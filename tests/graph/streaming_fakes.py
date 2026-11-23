"""A streaming fake chat model behind the real gateway, for offline token tests.

``FakeStreamingChatModel`` is a LangChain ``BaseChatModel`` that streams the JSON of
a preconfigured response in small fragments, chunk by chunk, from ``_astream``. It
supports both structured-output paths the real providers use:

* ``method=None`` -> LangChain's default tool calling: fragments arrive as
  ``tool_call_chunks[].args`` (the shape of function-calling providers);
* ``method="json_schema"`` -> fragments arrive as ``content`` (the default for
  ``ChatGoogleGenerativeAI`` and ``ChatOpenAI``/OpenRouter in this project).

``FakeStreamingGatewayLLM`` subclasses the production ``_PacedStructuredLLM``, so
every call goes through the gateway's real ``generate``: ``with_structured_output``,
``include_raw`` parsing, usage accounting and the run-metadata tag. Nothing is
fabricated after the fact: if the model is built with ``disable_streaming=True``,
LangChain calls ``_agenerate`` and no chunk exists.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Sequence
from operator import itemgetter
from typing import Any

from app.core.settings import Settings
from app.model_gateway.llm import _PacedStructuredLLM
from langchain_core.callbacks import AsyncCallbackManagerForLLMRun
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage
from langchain_core.output_parsers import PydanticOutputParser
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from langchain_core.runnables import Runnable, RunnableMap, RunnablePassthrough
from langchain_core.utils.function_calling import convert_to_openai_tool
from pydantic import BaseModel, ConfigDict, Field


class FakeStreamingChatModel(BaseChatModel):
    """Streams each schema's JSON in ``fragment_size`` pieces."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    payloads: dict[str, str] = Field(default_factory=dict)
    fragment_size: int = 9
    # Optional per-fragment delay, so parallel branches interleave.
    delay_seconds: float = 0.0
    # Prepended to content streams to prove thinking blocks are filtered.
    thinking_block: str | None = None
    streamed_calls: list[str] = Field(default_factory=list)
    generated_calls: list[str] = Field(default_factory=list)
    # Usage reported on the last chunk (streamed) or the message (not streamed), so
    # tests can show token accounting survives the streaming path.
    input_tokens: int = 11
    output_tokens: int = 7
    # Optional gate: after ``pause_after`` fragments of a response for ``pause_schema``
    # the stream sets ``paused`` and waits for ``resume`` (proves fragments leave
    # before the model has finished).
    pause_schema: str | None = None
    pause_after: int = 0
    paused: asyncio.Event = Field(default_factory=asyncio.Event, exclude=True)
    resume: asyncio.Event = Field(default_factory=asyncio.Event, exclude=True)

    @property
    def _llm_type(self) -> str:
        return "fake-streaming"

    def bind_tools(self, tools: Sequence[Any], **kwargs: Any) -> Runnable[Any, BaseMessage]:
        return self.bind(tools=[convert_to_openai_tool(tool) for tool in tools], **kwargs)

    def with_structured_output(  # type: ignore[override]
        self,
        schema: Any,
        *,
        method: str | None = None,
        include_raw: bool = False,
        **kwargs: Any,
    ) -> Runnable[Any, Any]:
        if method in (None, "function_calling"):
            return super().with_structured_output(schema, include_raw=include_raw, **kwargs)
        # The json_schema shape providers use: raw message, then a content parser.
        llm = self.bind(response_schema=schema.__name__)
        parser = PydanticOutputParser(pydantic_object=schema)
        if not include_raw:
            return llm | parser
        assign = RunnablePassthrough.assign(
            parsed=itemgetter("raw") | parser,
            parsing_error=lambda _: None,
        )
        fallback = RunnablePassthrough.assign(parsed=lambda _: None)
        return RunnableMap(raw=llm) | assign.with_fallbacks(
            [fallback], exception_key="parsing_error"
        )

    def _schema_name(self, kwargs: dict[str, Any]) -> str:
        if kwargs.get("tools"):
            return str(kwargs["tools"][0]["function"]["name"])
        return str(kwargs["response_schema"])

    def _generate(self, messages: list[BaseMessage], stop: Any = None, **kwargs: Any) -> ChatResult:
        raise NotImplementedError

    async def _agenerate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: AsyncCallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        """Non-streaming path (``disable_streaming=True``): one finished message."""
        name = self._schema_name(kwargs)
        payload = self.payloads[name]
        self.generated_calls.append(name)
        usage = self._usage()
        if kwargs.get("tools"):
            message = AIMessage(
                content="",
                tool_calls=[{"name": name, "args": json.loads(payload), "id": "call-1"}],
                usage_metadata=usage,
            )
        else:
            message = AIMessage(content=payload, usage_metadata=usage)
        return ChatResult(generations=[ChatGeneration(message=message)])

    def _usage(self) -> Any:
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.input_tokens + self.output_tokens,
        }

    async def _astream(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: AsyncCallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[ChatGenerationChunk]:
        name = self._schema_name(kwargs)
        payload = self.payloads[name]
        self.streamed_calls.append(name)
        as_tool = bool(kwargs.get("tools"))
        if not as_tool and self.thinking_block is not None:
            thought = AIMessageChunk(
                content=[{"type": "thinking", "thinking": self.thinking_block}]
            )
            yield await self._emit(ChatGenerationChunk(message=thought), run_manager)
        starts = list(range(0, len(payload), self.fragment_size))
        for number, start in enumerate(starts, start=1):
            piece = payload[start : start + self.fragment_size]
            last = number == len(starts)
            if as_tool:
                message = AIMessageChunk(
                    content="",
                    tool_call_chunks=[
                        {
                            "name": name if start == 0 else None,
                            "args": piece,
                            "id": "call-1" if start == 0 else None,
                            "index": 0,
                        },
                    ],
                    usage_metadata=self._usage() if last else None,
                )
            else:
                message = AIMessageChunk(
                    content=piece,
                    usage_metadata=self._usage() if last else None,
                )
            yield await self._emit(ChatGenerationChunk(message=message), run_manager)
            if name == self.pause_schema and number == self.pause_after:
                self.paused.set()
                await self.resume.wait()
            if self.delay_seconds:
                await asyncio.sleep(self.delay_seconds)

    async def _emit(
        self,
        chunk: ChatGenerationChunk,
        run_manager: AsyncCallbackManagerForLLMRun | None,
    ) -> ChatGenerationChunk:
        if run_manager is not None:
            await run_manager.on_llm_new_token(chunk.text, chunk=chunk)
        return chunk


def payloads_from(responses: dict[type[BaseModel], BaseModel]) -> dict[str, str]:
    return {schema.__name__: value.model_dump_json() for schema, value in responses.items()}


class FakeStreamingGatewayLLM(_PacedStructuredLLM):
    """The production gateway call path over ``FakeStreamingChatModel``."""

    def __init__(
        self,
        responses: dict[type[BaseModel], BaseModel],
        *,
        method: str | None = None,
        disable_streaming: bool = False,
        settings: Settings | None = None,
        **model_kwargs: Any,
    ) -> None:
        # Explicit settings: never read the developer's .env from a test double.
        super().__init__(settings or Settings(_env_file=None))  # type: ignore[call-arg]
        self.structured_method = method
        self.model = FakeStreamingChatModel(
            payloads=payloads_from(responses),
            disable_streaming=disable_streaming,
            **model_kwargs,
        )

    def _chat_model(self, *, model: str, temperature: float) -> BaseChatModel:
        _ = (model, temperature)
        return self.model

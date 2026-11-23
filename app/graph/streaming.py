"""Observe one graph execution through LangGraph's own stream (M5-B).

``stream_graph`` replaces ``ainvoke`` for callers that want progress. It runs the
same compiled graph once, with the same input, config and ``durability``, and returns
exactly what ``ainvoke`` (v1) returns: the latest state values, plus ``__interrupt__``
when the run paused. It is a re-implementation of ``Pregel.ainvoke``'s collection
loop (langgraph 1.2.11) with more stream modes attached, not a second execution.

Stream modes used (verified against the installed langgraph 1.2.11):

* ``values``: the state after each step; v2 parts carry ``interrupts``. Used only
  to build the return value, as ``ainvoke`` does.
* ``tasks``: one *start* part ``{id, name, input, triggers}`` when a node task is
  scheduled and one *result* part ``{id, name, error, result, interrupts}`` when it
  ends. ``Send`` branches of ``run_parallel_agent`` get distinct task ids. Node
  retries are not visible (one start, one result).
* ``messages`` (only when tokens are requested): ``(chunk, metadata)`` for every
  chat-model chunk produced inside a node. LangChain propagates callbacks through
  context variables, so the gateway's calls stream without being handed the node
  config. ``metadata["langgraph_checkpoint_ns"]`` is ``"<node>:<task id>"``.

Translation is conservative. Only names, task ids, agent ids and the *names* of
written state keys leave this module, never their values. Inputs, errors and
interrupt payloads are dropped. Model output is forwarded only when it is
(a) an ``AIMessageChunk``, i.e. really streamed (for a non-streaming model LangGraph
emits the finished ``AIMessage``, which is dropped rather than replayed as fake
tokens), and (b) from a run the gateway tagged as structured output. Reasoning or
thinking content blocks are never forwarded.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any, Literal, cast

from langchain_core.messages import AIMessageChunk
from langchain_core.runnables import RunnableConfig

from app.graph.typed_state import GraphState
from app.model_gateway.llm import STRUCTURED_OUTPUT_METADATA_KEY

INTERRUPT_KEY = "__interrupt__"

# Nodes whose LLM calls belong to one agent. ``run_parallel_agent`` takes the agent
# from its Send input instead.
_NODE_AGENTS: dict[str, str] = {
    "triage_planner": "triage_planner",
    "evidence_verifier": "evidence_verifier",
    "focused_retrieval_repair": "evidence_investigator",
}
_PARALLEL_NODE = "run_parallel_agent"
_STATE_KEYS = frozenset(GraphState.__annotations__)


@dataclass(frozen=True)
class NodeStarted:
    node: str
    task_id: str
    agent: str | None


@dataclass(frozen=True)
class NodeFinished:
    node: str
    task_id: str
    agent: str | None
    status: Literal["ok", "error", "interrupted"]
    updated_keys: tuple[str, ...]


@dataclass(frozen=True)
class OutputFragment:
    """A streamed piece of a structured-output response: provisional, unverified."""

    node: str
    task_id: str
    agent: str | None
    schema: str
    channel: Literal["tool_call_arguments", "content"]
    text: str
    # Stable for all chunks of one model response (LangChain sets it per run), so
    # two calls made by one node can be told apart. Internal: never sent to clients.
    message_id: str


GraphEvent = NodeStarted | NodeFinished | OutputFragment
GraphEventHandler = Callable[[GraphEvent], Awaitable[None]]


class GraphStreamTranslator:
    """Stateful: remembers each task's agent from its start part."""

    def __init__(self) -> None:
        self._agents: dict[str, str | None] = {}
        self._nodes: dict[str, str] = {}

    def translate(self, part: Mapping[str, Any]) -> list[GraphEvent]:
        kind = part.get("type")
        if part.get("ns"):
            return []  # no subgraphs in this graph; ignore rather than misattribute
        if kind == "tasks":
            return self._task(part.get("data") or {})
        if kind == "messages":
            return self._message(part.get("data"))
        return []

    def _task(self, data: Mapping[str, Any]) -> list[GraphEvent]:
        task_id = str(data.get("id") or "")
        node = str(data.get("name") or "")
        if not task_id or not node:
            return []
        if "input" in data:  # start part
            agent = _agent_for(node, data.get("input"))
            self._agents[task_id] = agent
            self._nodes[task_id] = node
            return [NodeStarted(node=node, task_id=task_id, agent=agent)]
        status: Literal["ok", "error", "interrupted"] = "ok"
        if data.get("error"):
            status = "error"
        elif data.get("interrupts"):
            status = "interrupted"
        result = data.get("result")
        written = result if isinstance(result, dict) else {}
        keys = sorted(key for key in written if key in _STATE_KEYS)
        return [
            NodeFinished(
                node=node,
                task_id=task_id,
                agent=self._agents.get(task_id, _agent_for(node, None)),
                status=status,
                updated_keys=tuple(keys),
            ),
        ]

    def _message(self, data: Any) -> list[GraphEvent]:
        if not isinstance(data, tuple) or len(data) != 2:
            return []
        chunk, metadata = data
        if not isinstance(chunk, AIMessageChunk) or not isinstance(metadata, Mapping):
            return []
        schema = metadata.get(STRUCTURED_OUTPUT_METADATA_KEY)
        if not isinstance(schema, str):
            return []  # not a gateway structured-output run: never forwarded
        namespace = str(metadata.get("langgraph_checkpoint_ns") or "")
        node, _, task_id = namespace.partition(":")
        node = node or str(metadata.get("langgraph_node") or "")
        agent = self._agents.get(task_id, _agent_for(node, None))
        message_id = str(chunk.id or "")
        events: list[GraphEvent] = []
        for piece in chunk.tool_call_chunks or []:
            args = piece.get("args")
            if isinstance(args, str) and args:
                events.append(
                    OutputFragment(
                        node=node,
                        task_id=task_id,
                        agent=agent,
                        schema=schema,
                        channel="tool_call_arguments",
                        text=args,
                        message_id=message_id,
                    ),
                )
        text = _visible_text(chunk.content)
        if text:
            events.append(
                OutputFragment(
                    node=node,
                    task_id=task_id,
                    agent=agent,
                    schema=schema,
                    channel="content",
                    text=text,
                    message_id=message_id,
                ),
            )
        return events


def _agent_for(node: str, task_input: Any) -> str | None:
    if node == _PARALLEL_NODE and isinstance(task_input, Mapping):
        agent = task_input.get("agent_name")
        return str(agent) if agent else None
    return _NODE_AGENTS.get(node)


def _visible_text(content: Any) -> str:
    """Answer text only: string content, or ``text`` blocks; thinking blocks dropped."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if isinstance(block, str):
            parts.append(block)
        elif isinstance(block, Mapping) and block.get("type") == "text":
            text = block.get("text")
            if isinstance(text, str):
                parts.append(text)
    return "".join(parts)


async def stream_graph(
    compiled: Any,
    graph_input: Any,
    config: RunnableConfig,
    *,
    on_event: GraphEventHandler,
    stream_tokens: bool,
) -> dict[str, Any]:
    """Run once, report progress, return what ``ainvoke`` would have returned."""
    modes = ["values", "tasks", *(["messages"] if stream_tokens else [])]
    translator = GraphStreamTranslator()
    latest: dict[str, Any] | None = None
    interrupts: list[Any] = []
    async for part in compiled.astream(
        graph_input,
        config,
        stream_mode=modes,
        durability="sync",
        version="v2",
    ):
        if part.get("type") == "values" and not part.get("ns"):
            latest = cast(dict[str, Any], part.get("data"))
            interrupts.extend(part.get("interrupts") or ())
            continue
        for event in translator.translate(part):
            await on_event(event)
    result: dict[str, Any] = dict(latest or {})
    if interrupts:
        result[INTERRUPT_KEY] = interrupts
    return result

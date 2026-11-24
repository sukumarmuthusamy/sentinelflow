"""Per-browser-session state for one analysis job, rebuilt only from API events.

Pure Python (no Streamlit), so it is unit-tested directly. Streamlit keeps one
``JobProgress`` per session in ``st.session_state``; every rerun reads it, and only
an explicit stream read changes it.

* **Durable events** (``job.*``, ``node.*``) are keyed by ``seq``. A replayed event
  (``seq`` at or below the cursor, or already stored) is ignored, so reconnecting
  never duplicates a row. ``cursor`` is the ``Last-Event-ID`` for the next read.
* **Live fragments** (``agent.token``) are keyed by ``(run, index)``; the index is
  gap-free per run as the server emits it, so a missing index is shown as a count of
  fragments not received. Nothing is filled in. ``stream.gap`` counts are added.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

TERMINAL_STATUSES = frozenset({"completed", "failed"})
ACTIVE_STATUSES = frozenset({"queued", "running"})


@dataclass
class NodeRun:
    run: int
    task_id: str
    node: str
    agent: str | None
    status: str = "running"  # running | ok | error | interrupted
    updated_keys: list[str] = field(default_factory=list)
    fragments_streamed: int | None = None
    fragments_dropped: int | None = None
    started_seq: int | None = None
    completed_seq: int | None = None

    @property
    def label(self) -> str:
        if self.agent and self.agent != self.node:
            return f"{self.node} · {self.agent}"
        return self.node


@dataclass
class FragmentStream:
    """Fragments of one model response, in index order."""

    run: int
    task_id: str
    node: str
    agent: str | None
    response: int
    schema_name: str
    channel: str
    pieces: dict[int, str] = field(default_factory=dict)

    @property
    def text(self) -> str:
        return "".join(self.pieces[index] for index in sorted(self.pieces))


@dataclass
class JobProgress:
    job_id: str
    cursor: int = 0
    durable: dict[int, dict[str, Any]] = field(default_factory=dict)
    nodes: dict[tuple[int, str], NodeRun] = field(default_factory=dict)
    fragments: dict[tuple[int, str, int, str], FragmentStream] = field(default_factory=dict)
    token_keys: set[tuple[int, int]] = field(default_factory=set)
    server_dropped: int = 0
    reads: int = 0
    reconnects: int = 0
    stream_ended: bool = False
    stream_failed: bool = False
    stream_note: str | None = None

    # -- applying events ------------------------------------------------------------

    def apply(self, events: list[dict[str, Any]]) -> int:
        """Apply a batch from the stream; return how many new rows it added."""
        added = 0
        for event in events:
            seq = event.get("seq")
            if isinstance(seq, int):
                if seq in self.durable:
                    continue  # replayed after a reconnect
                self.durable[seq] = event
                self.cursor = max(self.cursor, seq)
                self._apply_durable(event, seq)
                added += 1
            elif event.get("type") == "agent.token":
                added += self._apply_token(event.get("data") or {})
            elif event.get("type") == "stream.gap":
                dropped = (event.get("data") or {}).get("dropped_live_events")
                if isinstance(dropped, int) and dropped > 0:
                    self.server_dropped += dropped
                    added += 1
        return added

    def _apply_durable(self, event: dict[str, Any], seq: int) -> None:
        kind = event.get("type")
        data = event.get("data") or {}
        if kind not in ("node.started", "node.completed"):
            return
        run = data.get("run")
        task_id = data.get("task_id")
        node = data.get("node")
        if not isinstance(run, int) or not isinstance(task_id, str) or not isinstance(node, str):
            return
        key = (run, task_id)
        entry = self.nodes.get(key)
        if entry is None:
            entry = NodeRun(run=run, task_id=task_id, node=node, agent=data.get("agent"))
            self.nodes[key] = entry
        if kind == "node.started":
            entry.started_seq = seq
            return
        entry.completed_seq = seq
        entry.status = str(data.get("status") or "ok")
        keys = data.get("updated_keys")
        entry.updated_keys = [str(item) for item in keys] if isinstance(keys, list) else []
        entry.fragments_streamed = data.get("fragments_streamed")
        entry.fragments_dropped = data.get("fragments_dropped")

    def _apply_token(self, data: dict[str, Any]) -> int:
        run, index = data.get("run"), data.get("index")
        task_id, text = data.get("task_id"), data.get("text")
        if not (
            isinstance(run, int)
            and isinstance(index, int)
            and isinstance(task_id, str)
            and isinstance(text, str)
        ):
            return 0
        if (run, index) in self.token_keys:
            return 0
        self.token_keys.add((run, index))
        raw_response = data.get("response")
        response = raw_response if isinstance(raw_response, int) else 1
        channel = str(data.get("channel") or "content")
        key = (run, task_id, response, channel)
        stream = self.fragments.get(key)
        if stream is None:
            stream = FragmentStream(
                run=run,
                task_id=task_id,
                node=str(data.get("node") or ""),
                agent=data.get("agent"),
                response=response,
                schema_name=str(data.get("schema_name") or ""),
                channel=channel,
            )
            self.fragments[key] = stream
        stream.pieces[index] = text
        return 1

    # -- derived views --------------------------------------------------------------

    @property
    def lifecycle(self) -> list[dict[str, Any]]:
        return [
            self.durable[seq]
            for seq in sorted(self.durable)
            if str(self.durable[seq].get("type", "")).startswith("job.")
        ]

    @property
    def missing_fragments(self) -> int:
        """Fragments the server emitted that this session never received."""
        missing = 0
        for run in {run for run, _ in self.token_keys}:
            indices = [index for item_run, index in self.token_keys if item_run == run]
            missing += max(indices) - len(indices)
        return missing

    def running_nodes(self) -> list[NodeRun]:
        return [node for node in self.nodes.values() if node.status == "running"]

    def stage(self, status: str) -> str:
        if status == "completed":
            return "Finished"
        if status == "failed":
            return "Failed"
        if status == "waiting_for_approval":
            return "Paused for human approval"
        running = self.running_nodes()
        if running:
            return "Running: " + ", ".join(node.label for node in running)
        if status == "queued":
            return "Queued"
        done = [node for node in self.nodes.values() if node.completed_seq is not None]
        if done:
            last = max(done, key=lambda node: node.completed_seq or 0)
            return f"Running (last finished: {last.label})"
        return "Starting"


@dataclass
class SubmissionKeys:
    """Idempotency keys for create requests.

    One key per distinct request body: submitting the same body again (a double
    click, a retry after a timeout) reuses the key, so the server returns the
    job it already created. A different body, or an explicit "new submission",
    gets a new key.
    """

    key: str | None = None
    body: dict[str, Any] | None = None

    def key_for(self, body: dict[str, Any]) -> str:
        if self.key is None or self.body != body:
            self.key = f"ui-{uuid.uuid4().hex}"
            self.body = dict(body)
        return self.key

    def reset(self) -> None:
        self.key = None
        self.body = None

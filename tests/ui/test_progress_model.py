"""The harness's session model: dedupe, cursor, parallel agents, fragments, keys."""

from __future__ import annotations

from typing import Any

from ui.harness.progress import JobProgress, SubmissionKeys


def _durable(seq: int, kind: str, **data: Any) -> dict[str, Any]:
    return {"type": kind, "seq": seq, "after_seq": None, "at": None, "data": data}


def _node(seq: int, kind: str, task: str, node: str, agent: str | None = None, **extra: Any):
    return _durable(seq, kind, node=node, task_id=task, agent=agent, run=1, **extra)


def _token(index: int, task: str = "t-a", text: str = "x", run: int = 1) -> dict[str, Any]:
    return {
        "type": "agent.token",
        "seq": None,
        "after_seq": 3,
        "at": None,
        "data": {
            "node": "run_parallel_agent",
            "task_id": task,
            "agent": "impact",
            "run": run,
            "index": index,
            "response": 1,
            "schema_name": "ChangeImpact",
            "channel": "content",
            "text": text,
        },
    }


EVENTS = [
    _durable(1, "job.queued"),
    _durable(2, "job.running"),
    _node(3, "node.started", "t-a", "run_parallel_agent", "change_impact"),
    _node(4, "node.started", "t-b", "run_parallel_agent", "test_strategist"),
    _node(
        5,
        "node.completed",
        "t-a",
        "run_parallel_agent",
        "change_impact",
        status="ok",
        updated_keys=["agent_outputs"],
    ),
]


def test_replayed_events_never_duplicate_rows() -> None:
    progress = JobProgress(job_id="job-x")
    assert progress.apply(EVENTS[:3]) == 3
    assert progress.apply(EVENTS) == 2  # 1-3 replayed after a reconnect
    assert progress.apply(EVENTS) == 0
    assert progress.cursor == 5
    assert [event["seq"] for event in progress.lifecycle] == [1, 2]
    assert len(progress.nodes) == 2


def test_parallel_agents_are_separate_rows_with_their_own_status() -> None:
    progress = JobProgress(job_id="job-x")
    progress.apply(EVENTS)
    by_agent = {node.agent: node for node in progress.nodes.values()}
    assert by_agent["change_impact"].status == "ok"
    assert by_agent["change_impact"].updated_keys == ["agent_outputs"]
    assert by_agent["test_strategist"].status == "running"
    assert progress.stage("running") == "Running: run_parallel_agent · test_strategist"
    assert progress.stage("waiting_for_approval") == "Paused for human approval"
    assert progress.stage("completed") == "Finished"


def test_fragments_are_ordered_deduplicated_and_gaps_are_counted_not_filled() -> None:
    progress = JobProgress(job_id="job-x")
    tokens = [_token(2, text="b"), _token(1, text="a"), _token(2, text="b"), _token(5, text="e")]
    progress.apply(tokens)
    stream = next(iter(progress.fragments.values()))
    assert stream.text == "abe"  # indices 3 and 4 were never received
    assert progress.missing_fragments == 2
    progress.apply([{"type": "stream.gap", "seq": None, "data": {"dropped_live_events": 4}}])
    assert progress.server_dropped == 4


def test_tokens_never_move_the_durable_cursor() -> None:
    progress = JobProgress(job_id="job-x")
    progress.apply([*EVENTS, _token(1)])
    assert progress.cursor == 5


def test_submission_keys_are_stable_per_body() -> None:
    keys = SubmissionKeys()
    body = {"change_id": "CHG-H001", "domain_pack_id": "healthcare-claims"}
    first = keys.key_for(body)
    assert keys.key_for(dict(body)) == first  # double click / retry: same key
    changed = keys.key_for({**body, "change_id": "CHG-H002"})
    assert changed != first
    keys.reset()
    assert keys.key_for(body) not in {first, changed}  # explicit new submission

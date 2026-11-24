"""The Streamlit page, driven with ``AppTest`` against a real API on a loopback socket.

Each ``at.run()`` is one Streamlit rerun of ``ui/streamlit_app.py``; the progress
fragment runs inline, so one rerun is one bounded poll of the event stream. The
API process is real (uvicorn) with the offline test runtime.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("streamlit", reason="needs the ui extra")

from streamlit.testing.v1 import AppTest  # noqa: E402
from ui.harness.client import SentinelFlowClient

from tests.api.conftest import FakeRuntime
from tests.ui.conftest import GateLLM, LiveApiFactory, closed_port_url

APP = str(Path(__file__).resolve().parents[2] / "ui" / "streamlit_app.py")


@pytest.fixture(autouse=True)
def _fast_polling(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SENTINELFLOW_UI_READ_WINDOW_SECONDS", "0.3")
    monkeypatch.setenv("SENTINELFLOW_UI_POLL_SECONDS", "0.2")
    monkeypatch.delenv("SENTINELFLOW_API_KEY", raising=False)


def _app(monkeypatch: pytest.MonkeyPatch, base_url: str) -> AppTest:
    monkeypatch.setenv("SENTINELFLOW_API_URL", base_url)
    at = AppTest.from_file(APP, default_timeout=60)
    at.run()
    _no_exception(at)
    return at


def _no_exception(at: AppTest) -> None:
    assert not at.exception, [item.value for item in at.exception]


def _until(at: AppTest, done: Callable[[Any], bool], *, runs: int = 120) -> None:
    for _ in range(runs):
        if done(at.session_state):
            return
        at.run()
        _no_exception(at)
    raise AssertionError(f"condition not reached; job: {at.session_state['job']}")


def _status(state: Any) -> str | None:
    job = state["job"]
    return None if job is None else str(job["status"])


def _settled(status: str) -> Callable[[Any], bool]:
    """The job reached ``status`` and the page has rendered its final state."""

    def check(state: Any) -> bool:
        progress = state["progress"]
        return (
            _status(state) == status
            and progress is not None
            and bool(progress.lifecycle)
            and progress.lifecycle[-1]["type"] == f"job.{status}"
            and (status == "failed" or state["decision"] is not None)
            and (state["decision"] is None or state["decision"]["status"] == status)
        )

    return check


def _texts(at: AppTest) -> str:
    parts = [item.value for item in at.markdown]
    for group in (at.info, at.success, at.warning, at.error, at.caption):
        parts += [str(item.value) for item in group]
    parts += [f"{item.label}={item.value}" for item in at.metric]
    return "\n".join(parts)


def _button(at: AppTest, label: str) -> Any:
    return next(button for button in at.button if button.label == label)


def _launch(at: AppTest, *, record: bool = False, change: str = "CHG-H001") -> None:
    at.selectbox(key="form_change").set_value(change)
    if record:
        at.checkbox(key="form_record").check()
    at.button(key="launch").click().run()
    _no_exception(at)


def test_launch_streams_node_progress_and_shows_the_decision(
    live_api: LiveApiFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with live_api() as api:
        at = _app(monkeypatch, api.base_url)
        _launch(at)
        assert at.session_state["analysis_keys"].key is not None
        _until(at, _settled("completed"))
        progress = at.session_state["progress"]
        job_id = at.session_state["job_id"]
        stored = [event.seq for event in api.service.store.events(job_id)]
        assert sorted(progress.durable) == stored  # every durable event, once
        assert progress.nodes and all(node.status == "ok" for node in progress.nodes.values())
        assert api.job_count() == 1 and api.runtime.sessions == 1
        assert api.released(job_id)
    text = _texts(at)
    verdict = at.session_state["decision"]["result"]["risk_assessment"]["decision"]
    assert f"<div class='value'>{verdict}</div>" in text
    assert "The job status only says the run finished" in text
    assert "Deterministic gate" in text and "Supporting analysis" in text
    assert "badge[Completed]" in text


def test_reruns_and_repeat_clicks_never_create_a_second_job(
    live_api: LiveApiFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with live_api() as api:
        at = _app(monkeypatch, api.base_url)
        _launch(at)
        first = at.session_state["job_id"]
        key = at.session_state["analysis_keys"].key
        _launch(at)  # the same form submitted again: same key, replayed job
        assert "idempotent replay" in _texts(at)
        for _ in range(5):
            at.run()
        _until(at, _settled("completed"))
        assert at.session_state["job_id"] == first
        assert at.session_state["analysis_keys"].key == key
        assert api.job_count() == 1 and api.runtime.sessions == 1

        _button(at, "New submission").click().run()
        _launch(at)  # an explicit new submission is a new job
        assert at.session_state["job_id"] != first
        assert api.job_count() == 2


def test_approval_is_explicit_and_records_the_unchanged_decision(
    live_api: LiveApiFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with live_api() as api:
        at = _app(monkeypatch, api.base_url)
        _launch(at, record=True)
        _until(at, _settled("waiting_for_approval"))
        for _ in range(3):  # waiting is stable: no automatic approval, no new job
            at.run()
        assert _status(at.session_state) == "waiting_for_approval"
        before = at.session_state["decision"]["result"]["risk_assessment"]
        assert "Human approval" in _texts(at)

        _button(at, "Approve and record").click().run()  # no reviewer, not confirmed
        assert "Enter a reviewer" in _texts(at)
        assert _status(at.session_state) == "waiting_for_approval"

        at.text_input(key="approval_reviewer").input("release-manager")
        at.checkbox(key="approval_reviewed").check()
        _button(at, "Approve and record").click().run()
        _no_exception(at)
        _until(at, _settled("completed"))
        decision = at.session_state["decision"]["result"]
        assert decision["risk_assessment"] == before
        assert decision["approval"]["gate_decision_recorded"] is True
        assert api.runtime.sessions == 2  # first run + one resume
        assert len(api.audit_log_path.read_text(encoding="utf-8").splitlines()) == 1
    text = _texts(at)
    assert "gate decision recorded (approved by release-manager)" in text
    assert not any(button.label == "Approve and record" for button in at.button)


def test_rejection_writes_nothing_and_keeps_the_decision_visible(
    live_api: LiveApiFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with live_api() as api:
        at = _app(monkeypatch, api.base_url)
        _launch(at, record=True)
        _until(at, _settled("waiting_for_approval"))
        at.text_input(key="approval_reviewer").input("qa-lead")
        at.checkbox(key="approval_reviewed").check()
        _button(at, "Reject").click().run()
        _until(at, _settled("completed"))
        assert not api.audit_log_path.exists()
    text = _texts(at)
    assert "rejected by qa-lead; nothing was written to the audit log" in text
    assert "<div class='value'>" in text


def test_duplicate_approval_from_a_second_session_is_reported(
    live_api: LiveApiFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with live_api() as api:
        at = _app(monkeypatch, api.base_url)
        _launch(at, record=True)
        _until(at, _settled("waiting_for_approval"))
        with SentinelFlowClient(api.base_url) as other:  # another browser tab approves
            other.submit_approval(at.session_state["job_id"], approved=True, reviewer="other")
        at.text_input(key="approval_reviewer").input("release-manager")
        at.checkbox(key="approval_reviewed").check()
        _button(at, "Approve and record").click().run()
        _no_exception(at)
        _until(at, _settled("completed"))
        assert len(api.audit_log_path.read_text(encoding="utf-8").splitlines()) == 1
    assert at.session_state["job"]["approval"]["reviewer"] == "other"
    assert "already submitted for this job; your click was not applied" in _texts(at)


def test_failed_job_shows_the_sanitized_error(
    live_api: LiveApiFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    marker = "INTERNAL-DETAIL-MARKER /srv/secret"
    runtime = FakeRuntime(session_error=RuntimeError(f"429 RESOURCE_EXHAUSTED {marker}"))
    with live_api(runtime=runtime) as api:
        at = _app(monkeypatch, api.base_url)
        _launch(at)
        _until(at, _settled("failed"))
    text = _texts(at)
    assert "PROVIDER_RATE_LIMITED" in text and "badge[Failed]" in text
    assert marker not in text and "Traceback" not in text


def test_reconnect_after_the_api_drops_resumes_without_duplicates(
    live_api: LiveApiFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    llm = GateLLM()
    with live_api(runtime=FakeRuntime(llm)) as api:
        at = _app(monkeypatch, api.base_url)
        _launch(at)
        assert llm.entered.wait(20)
        at.run()
        seen = at.session_state["progress"].cursor
        assert seen >= 2 and _status(at.session_state) == "running"

        at.text_input(key="api_url").input(closed_port_url()).run()  # the API "goes away"
        _no_exception(at)
        assert "Cannot reach the SentinelFlow API" in at.session_state["progress"].stream_note

        at.text_input(key="api_url").input(api.base_url).run()
        llm.gate.set()
        _until(at, _settled("completed"))
        progress = at.session_state["progress"]
        stored = [event.seq for event in api.service.store.events(at.session_state["job_id"])]
        assert sorted(progress.durable) == stored
        assert progress.reconnects >= 1
        assert "Reconnected" in (progress.stream_note or "")
        assert api.job_count() == 1


def test_open_an_existing_job_without_rerunning_it(
    live_api: LiveApiFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with live_api() as api:
        with SentinelFlowClient(api.base_url) as client:
            job_id = client.create_analysis(
                {"change_id": "CHG-H003", "domain_pack_id": "healthcare-claims"},
                idempotency_key="existing",
            ).job["job_id"]
        api.wait_for(job_id, {"completed"})
        at = _app(monkeypatch, api.base_url)
        at.text_input(key="open_job_id").input(job_id)
        _button(at, "Open job").click().run()
        _until(at, _settled("completed"))
        assert api.runtime.sessions == 1 and api.job_count() == 1
        progress = at.session_state["progress"]
        assert sorted(progress.durable) == [e.seq for e in api.service.store.events(job_id)]

        at.text_input(key="open_job_id").input("not-a-job")
        _button(at, "Open job").click().run()
        assert "INVALID_JOB_ID" in _texts(at)


def test_evaluation_is_labelled_a_fixture_consistency_check(
    live_api: LiveApiFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with live_api() as api:
        at = _app(monkeypatch, api.base_url)
        _button(at, "Run evaluation").click().run()
        _until(at, lambda state: state["eval_result"] is not None)
        _button(at, "Run evaluation").click().run()  # same selection: replayed, not re-run
        assert api.job_count() == 1
    text = _texts(at)
    assert "Fixture consistency check, not model-quality accuracy" in text
    assert "Matched label=8 / 8" in text
    assert len(at.session_state["eval_result"]["result"]["rows"]) == 8


def test_an_unreachable_api_is_reported_not_raised(monkeypatch: pytest.MonkeyPatch) -> None:
    at = _app(monkeypatch, closed_port_url())
    _launch(at)
    assert "Cannot reach the SentinelFlow API" in _texts(at)
    assert at.session_state["job"] is None
    _button(at, "Check connection").click().run()
    _no_exception(at)
    assert "Cannot reach the SentinelFlow API" in _texts(at)

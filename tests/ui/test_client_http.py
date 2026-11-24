"""The harness's HTTP client against a real API on a loopback socket (M5-C)."""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from app.core.settings import Settings
from ui.harness.client import ApiError, ApiUnavailableError, SentinelFlowClient
from ui.harness.progress import JobProgress

from tests.api.conftest import FakeRuntime
from tests.graph.conftest import static_llm_responses
from tests.graph.fakes import FailingToolMcp
from tests.graph.streaming_fakes import FakeStreamingGatewayLLM
from tests.ui.conftest import GateLLM, LiveApiFactory, closed_port_url

PLAIN = {"change_id": "CHG-H001", "domain_pack_id": "healthcare-claims"}
GATED = {**PLAIN, "record_decision": True}


def _drain(client: SentinelFlowClient, progress: JobProgress, *, reads: int = 40) -> None:
    for _ in range(reads):
        batch = client.read_events(progress.job_id, after_seq=progress.cursor, window_seconds=0.5)
        progress.apply(batch.events)
        if batch.ended or batch.paused:
            return
    raise AssertionError("the stream did not end")


def test_create_is_idempotent_per_key(live_api: LiveApiFactory) -> None:
    with live_api() as api, SentinelFlowClient(api.base_url) as client:
        first = client.create_analysis(PLAIN, idempotency_key="ui-key-1")
        again = client.create_analysis(PLAIN, idempotency_key="ui-key-1")
        with pytest.raises(ApiError) as conflict:
            client.create_analysis({**PLAIN, "change_id": "CHG-H002"}, idempotency_key="ui-key-1")
        api.wait_for(first.job["job_id"], {"completed"})
        assert api.job_count() == 1
    assert first.replayed is False and again.replayed is True
    assert again.job["job_id"] == first.job["job_id"]
    assert conflict.value.status == 409
    assert conflict.value.code == "IDEMPOTENCY_KEY_CONFLICT"
    assert conflict.value.request_id


def test_replay_and_resume_from_the_cursor_without_duplicates(live_api: LiveApiFactory) -> None:
    with live_api() as api, SentinelFlowClient(api.base_url) as client:
        job_id = client.create_analysis(PLAIN, idempotency_key="ui-replay").job["job_id"]
        api.wait_for(job_id, {"completed"})
        stored = [event.seq for event in api.service.store.events(job_id)]

        full = client.read_events(job_id, after_seq=0, window_seconds=2)
        half = stored[len(stored) // 2]
        rest = client.read_events(job_id, after_seq=half, window_seconds=2)
        at_end = client.read_events(job_id, after_seq=stored[-1], window_seconds=2)

        progress = JobProgress(job_id=job_id)
        progress.apply(full.events[: len(full.events) // 2])
        progress.apply(full.events)  # overlapping replay
        progress.apply(rest.events)
        assert api.released(job_id)  # every read released its subscription
    assert full.ended and [event["seq"] for event in full.events] == stored
    assert [event["seq"] for event in rest.events] == [seq for seq in stored if seq > half]
    assert at_end.ended and at_end.events == []
    assert sorted(progress.durable) == stored and progress.cursor == stored[-1]
    assert progress.nodes and all(node.status == "ok" for node in progress.nodes.values())


def test_reconnect_mid_run_resumes_after_the_last_durable_event(
    live_api: LiveApiFactory,
) -> None:
    llm = GateLLM()
    with live_api(runtime=FakeRuntime(llm)) as api, SentinelFlowClient(api.base_url) as client:
        job_id = client.create_analysis(PLAIN, idempotency_key="ui-reconnect").job["job_id"]
        assert llm.entered.wait(20)
        progress = JobProgress(job_id=job_id)
        first = client.read_events(job_id, after_seq=0, window_seconds=0.5)
        progress.apply(first.events)
        assert not first.ended and progress.cursor >= 2  # queued, running, node events
        assert api.released(job_id)  # the window closed its connection
        llm.gate.set()
        _drain(client, progress)
        stored = [event.seq for event in api.service.store.events(job_id)]
    assert sorted(progress.durable) == stored
    assert progress.lifecycle[-1]["type"] == "job.completed"


def test_approval_and_duplicate_approval(live_api: LiveApiFactory) -> None:
    with live_api() as api, SentinelFlowClient(api.base_url) as client:
        job_id = client.create_analysis(GATED, idempotency_key="ui-gated").job["job_id"]
        progress = JobProgress(job_id=job_id)
        _drain(client, progress)
        assert progress.lifecycle[-1]["type"] == "job.waiting_for_approval"
        before = client.get_decision(job_id)
        accepted = client.submit_approval(job_id, approved=True, reviewer="release-manager")
        with pytest.raises(ApiError) as duplicate:
            client.submit_approval(job_id, approved=True, reviewer="release-manager")
        _drain(client, progress)
        after = client.get_decision(job_id)
        audit = api.audit_log_path.read_text(encoding="utf-8").splitlines()
    assert accepted["status"] == "queued" and accepted["approval"]["approved"] is True
    assert duplicate.value.status == 409 and duplicate.value.code == "APPROVAL_ALREADY_SUBMITTED"
    assert after["status"] == "completed"
    assert after["result"]["risk_assessment"] == before["result"]["risk_assessment"]
    assert after["result"]["approval"]["gate_decision_recorded"] is True
    assert len(audit) == 1


def test_rejection_completes_without_an_audit_write(live_api: LiveApiFactory) -> None:
    with live_api() as api, SentinelFlowClient(api.base_url) as client:
        job_id = client.create_analysis(GATED, idempotency_key="ui-reject").job["job_id"]
        api.wait_for(job_id, {"waiting_for_approval"})
        client.submit_approval(job_id, approved=False, reviewer="qa-lead", comment="not now")
        api.wait_for(job_id, {"completed"})
        decision = client.get_decision(job_id)
        assert not api.audit_log_path.exists()
    assert decision["result"]["approval"]["status"] == "rejected"


def test_failed_job_and_error_envelopes(live_api: LiveApiFactory) -> None:
    marker = "INTERNAL-DETAIL-MARKER"
    runtime = FakeRuntime(session_error=RuntimeError(f"429 RESOURCE_EXHAUSTED {marker}"))
    with live_api(runtime=runtime) as api, SentinelFlowClient(api.base_url) as client:
        job_id = client.create_analysis(PLAIN, idempotency_key="ui-failed").job["job_id"]
        progress = JobProgress(job_id=job_id)
        _drain(client, progress)
        job = client.get_analysis(job_id)
        with pytest.raises(ApiError) as no_decision:
            client.get_decision(job_id)
        with pytest.raises(ApiError) as invalid:
            client.create_analysis(
                {"change_id": "bad id!", "domain_pack_id": "x"}, idempotency_key="ui-invalid"
            )
        with pytest.raises(ApiError) as missing:
            client.get_analysis("job-" + "0" * 32)
    assert job["status"] == "failed" and job["error"]["code"] == "PROVIDER_RATE_LIMITED"
    assert marker not in job["error"]["message"]
    assert progress.lifecycle[-1]["type"] == "job.failed"
    assert no_decision.value.code == "RESULT_NOT_AVAILABLE"
    assert invalid.value.status == 422 and invalid.value.code == "INVALID_REQUEST"
    assert any(item["loc"][-1] == "change_id" for item in invalid.value.details["errors"])
    assert missing.value.status == 404 and missing.value.code == "JOB_NOT_FOUND"


def test_a_degraded_run_completes_as_review(live_api: LiveApiFactory) -> None:
    """A tool fault degrades the run: completed, conservative decision, reasons listed."""
    runtime = FakeRuntime(
        wrap_mcp=lambda mcp: FailingToolMcp(mcp, failing_tool="get_change_package"),
    )
    with live_api(runtime=runtime) as api, SentinelFlowClient(api.base_url) as client:
        job_id = client.create_analysis(PLAIN, idempotency_key="ui-degraded").job["job_id"]
        api.wait_for(job_id, {"completed", "failed"})
        decision = client.get_decision(job_id)
    result = decision["result"]
    assert decision["status"] == "completed"
    assert result["degraded"] is True and result["degradation"]
    assert result["risk_assessment"]["decision"] in {"REVIEW", "BLOCK"}


def test_live_fragments_arrive_labelled_provisional(
    live_api: LiveApiFactory,
    tmp_path: Any,
) -> None:
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        graph_checkpoint_path=tmp_path / "streaming-checkpoints.sqlite",
        gemini_api_key=None,
        openrouter_api_key=None,
    )
    llm = FakeStreamingGatewayLLM(static_llm_responses(), settings=settings)
    runtime = FakeRuntime(llm, stream_tokens=True)
    with live_api(runtime=runtime) as api, SentinelFlowClient(api.base_url) as client:
        job_id = client.create_analysis(PLAIN, idempotency_key="ui-tokens").job["job_id"]
        progress = JobProgress(job_id=job_id)
        tokens: list[dict[str, Any]] = []
        for _ in range(60):
            batch = client.read_events(job_id, after_seq=progress.cursor, window_seconds=0.5)
            tokens += [event for event in batch.events if event["type"] == "agent.token"]
            progress.apply(batch.events)
            if batch.ended:
                break
    assert tokens, "the streaming fake emitted no fragments"
    assert all(t["data"]["provisional"] is True and t["data"]["verified"] is False for t in tokens)
    assert all(t["seq"] is None and t["after_seq"] is not None for t in tokens)
    assert progress.fragments and progress.missing_fragments >= 0


def test_evaluation_over_http(live_api: LiveApiFactory) -> None:
    with live_api() as api, SentinelFlowClient(api.base_url) as client:
        created = client.create_evaluation(domain_pack_ids=None, idempotency_key="ui-eval")
        replay = client.create_evaluation(domain_pack_ids=None, idempotency_key="ui-eval")
        api.wait_for(created.job["job_id"], {"completed"})
        result = client.get_evaluation_result(created.job["job_id"])
    assert replay.replayed and replay.job["job_id"] == created.job["job_id"]
    summary = result["result"]["summary"]
    assert summary["fixtures"] == len(result["result"]["rows"]) == 8
    assert summary["matched"] == summary["labelled"]


def test_api_key_is_sent_only_as_a_bearer_header(live_api: LiveApiFactory) -> None:
    secret = "ui-test-key-" + "x" * 24
    with live_api(api_key=secret) as api:
        with SentinelFlowClient(api.base_url) as anonymous, pytest.raises(ApiError) as denied:
            anonymous.get_analysis("job-" + "0" * 32)
        with SentinelFlowClient(api.base_url, api_key=secret) as authorized:
            created = authorized.create_analysis(PLAIN, idempotency_key="ui-auth")
    assert denied.value.code == "UNAUTHORIZED"
    assert secret not in str(denied.value) and secret not in denied.value.message
    assert created.job["status"] in {"queued", "running", "completed"}


def test_unreachable_api_raises_a_clear_error() -> None:
    url = closed_port_url()
    with SentinelFlowClient(url, timeout_seconds=2) as client:
        with pytest.raises(ApiUnavailableError) as on_get:
            client.get_analysis("job-" + "0" * 32)
        with pytest.raises(ApiUnavailableError) as on_stream:
            client.read_events("job-" + "0" * 32, after_seq=0, window_seconds=0.5)
        with pytest.raises(ApiUnavailableError) as on_create:
            client.create_analysis(PLAIN, idempotency_key="ui-down")
    assert url in on_get.value.message and "app.api.serve" in on_get.value.message
    assert on_stream.value.message == on_get.value.message
    assert on_create.value.ambiguous is False  # refused before anything was sent


def test_a_non_sentinelflow_error_body_is_never_echoed() -> None:
    def proxy(request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, text="Traceback (most recent call last): secret-path")

    transport = httpx.MockTransport(proxy)
    with (
        SentinelFlowClient("http://127.0.0.1:9", transport=transport) as client,
        pytest.raises(ApiError) as info,
    ):
        client.get_analysis("job-" + "0" * 32)
    assert info.value.code == "HTTP_502"
    assert "Traceback" not in info.value.message and "secret-path" not in str(info.value)


def test_job_ids_are_validated_before_building_a_url() -> None:
    with SentinelFlowClient("http://127.0.0.1:9") as client, pytest.raises(ApiError) as info:
        client.get_analysis("../../health")
    assert info.value.code == "INVALID_JOB_ID"

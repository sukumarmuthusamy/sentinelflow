"""Analyses over HTTP: creation, status transitions, decisions and degradation."""

from __future__ import annotations

import pytest
from app.agents.contracts import ChangeImpact
from app.jobs.events import EMITTED_EVENT_TYPES, LIFECYCLE_EVENT_TYPES, JobEventType

from tests.api.conftest import BlockingLLM, FakeRuntime, HarnessFactory
from tests.graph.conftest import static_llm_responses
from tests.graph.fakes import FailingLLM, FailingToolMcp

PACK = "healthcare-claims"

# Graph-state fields that must never appear in an API response.
INTERNAL_STATE_KEYS = ("retrieved_chunks", "deterministic_signals", "parallel_completions")


async def test_analysis_runs_to_completion_over_http(harness: HarnessFactory) -> None:
    async with harness() as h:
        created = await h.create()
        assert created.status_code == 202
        job = created.json()
        assert job["status"] in {"queued", "running"}
        assert job["kind"] == "analysis"
        assert created.headers["Location"] == f"/v1/analyses/{job['job_id']}"
        assert created.headers["X-Request-ID"]
        assert job["links"]["approval"] is None  # record_decision was not requested

        await h.idle()
        status = (await h.client.get(created.headers["Location"])).json()
        assert status["status"] == "completed"
        assert status["result_available"] is True
        assert status["run_count"] == 1
        assert status["error"] is None
        assert status["started_at"] and status["finished_at"]

        decision = await h.client.get(f"/v1/analyses/{job['job_id']}/decision")
        assert decision.status_code == 200
        body = decision.json()
        result = body["result"]
        assert body["status"] == "completed"
        assert result["analysis_id"] == job["job_id"]
        assert result["risk_assessment"]["decision"] == "PASS"
        assert result["report"]["decision"] == "PASS"
        assert result["degraded"] is False
        assert result["degradation"] == []
        assert result["approval"] == {
            "required": False,
            "status": "not_requested",
            "gate_decision_recorded": False,
        }
        assert result["trace"]["llm_calls"] > 0
        assert result["plan"]["agents"]
        for key in INTERNAL_STATE_KEYS:
            assert key not in decision.text
    assert h.runtime.sessions == 1


async def test_request_defaults_are_echoed_validated(harness: HarnessFactory) -> None:
    async with harness() as h:
        job = (await h.create()).json()
    assert job["request"] == {
        "change_id": "CHG-H001",
        "domain_pack_id": "healthcare-claims",
        "objective": "Assess release risk and generate the minimum sufficient regression plan",
        "execute_tests": False,
        "record_decision": False,
        "source": "fixture",
    }


async def test_running_job_reports_running_and_no_decision_yet(harness: HarnessFactory) -> None:
    llm = BlockingLLM()
    async with harness(runtime=FakeRuntime(llm)) as h:
        job = (await h.create()).json()
        await llm.entered.wait()
        status = (await h.client.get(f"/v1/analyses/{job['job_id']}")).json()
        assert status["status"] == "running"
        early = await h.client.get(f"/v1/analyses/{job['job_id']}/decision")
        assert early.status_code == 409
        assert early.json()["error"]["code"] == "RESULT_NOT_AVAILABLE"
        assert early.json()["error"]["details"] == {"job_id": job["job_id"], "status": "running"}
        llm.release()
        await h.idle()
        done = (await h.client.get(f"/v1/analyses/{job['job_id']}")).json()
        assert done["status"] == "completed"


async def test_block_fixture_completes_with_block(harness: HarnessFactory) -> None:
    async with harness() as h:
        status = await h.run_to_rest({"change_id": "CHG-H003", "domain_pack_id": PACK})
        decision = (await h.client.get(status["links"]["result"])).json()
    assert status["status"] == "completed"
    assert decision["result"]["risk_assessment"]["decision"] == "BLOCK"


@pytest.mark.parametrize(("change_id", "expected"), [("CHG-H001", "REVIEW"), ("CHG-H003", "BLOCK")])
async def test_degraded_run_is_completed_not_failed(
    harness: HarnessFactory,
    change_id: str,
    expected: str,
) -> None:
    """A conservative REVIEW is the analysis result; degradation never weakens BLOCK."""
    llm = FailingLLM(static_llm_responses(), fail_on={ChangeImpact})
    async with harness(runtime=FakeRuntime(llm)) as h:
        status = await h.run_to_rest({"change_id": change_id, "domain_pack_id": PACK})
        decision = await h.client.get(status["links"]["result"])
    assert status["status"] == "completed"
    assert status["error"] is None
    result = decision.json()["result"]
    assert result["degraded"] is True
    assert result["risk_assessment"]["decision"] == expected
    assert {"code": "structured_output_invalid", "component": "change_analyst", "detail": None} in (
        result["degradation"]
    )
    # The graph's error string carries the exception message; the API does not.
    assert "simulated" not in decision.text
    assert "did not validate" not in decision.text


async def test_mcp_tool_failure_degrades_with_a_code_not_a_message(
    harness: HarnessFactory,
) -> None:
    runtime = FakeRuntime(
        wrap_mcp=lambda mcp: FailingToolMcp(mcp, failing_tool="run_security_scan"),
    )
    async with harness(runtime=runtime) as h:
        status = await h.run_to_rest()
        decision = await h.client.get(status["links"]["result"])
    result = decision.json()["result"]
    assert status["status"] == "completed"
    assert result["risk_assessment"]["decision"] == "REVIEW"
    assert {"code": "mcp_error", "component": "security_auditor", "detail": None} in (
        result["degradation"]
    )
    assert "simulated tool failure" not in decision.text


async def test_lifecycle_events_are_recorded_in_order(harness: HarnessFactory) -> None:
    async with harness() as h:
        status = await h.run_to_rest()
        events = h.service.store.events(status["job_id"])
    assert [event.type for event in events] == [
        JobEventType.JOB_QUEUED,
        JobEventType.JOB_RUNNING,
        JobEventType.JOB_COMPLETED,
    ]
    assert [event.seq for event in events] == [1, 2, 3]
    assert events[-1].data == {"decision": "PASS", "degraded": False}


def test_only_lifecycle_events_are_claimed_as_emitted() -> None:
    """Node and token events are M5-B contracts; nothing may claim they work yet."""
    assert EMITTED_EVENT_TYPES == LIFECYCLE_EVENT_TYPES
    assert JobEventType.AGENT_TOKEN not in EMITTED_EVENT_TYPES
    assert JobEventType.NODE_STARTED not in EMITTED_EVENT_TYPES

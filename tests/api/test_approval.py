"""Approval over HTTP: interrupt, strict resume, audit exactly once, decision unchanged."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from app.jobs.events import JobEventType
from app.model_gateway.llm import StaticStructuredLLM

from tests.api.conftest import BlockingLLM, FakeRuntime, HarnessFactory, api_settings
from tests.graph.conftest import static_llm_responses
from tests.graph.fakes import FailingToolMcp

GATED = {"change_id": "CHG-H001", "domain_pack_id": "healthcare-claims", "record_decision": True}
APPROVE = {"approved": True, "reviewer": "release-manager"}


async def _approve(h: Any, job_id: str, body: dict[str, Any] | None = None) -> Any:
    return await h.client.post(f"/v1/analyses/{job_id}/approval", json=body or APPROVE)


async def test_gated_analysis_waits_then_records_once(harness: HarnessFactory) -> None:
    llm = StaticStructuredLLM(static_llm_responses())
    async with harness(runtime=FakeRuntime(llm)) as h:
        waiting = await h.run_to_rest(GATED)
        job_id = waiting["job_id"]
        assert waiting["status"] == "waiting_for_approval"
        assert waiting["links"]["approval"] == f"/v1/analyses/{job_id}/approval"
        before = (await h.client.get(f"/v1/analyses/{job_id}/decision")).json()
        assert before["status"] == "waiting_for_approval"
        assert before["result"]["approval"] == {
            "required": True,
            "status": "pending",
            "gate_decision_recorded": False,
        }
        assert h.audit_lines() == []
        calls_before = len(llm.calls)

        accepted = await _approve(h, job_id, {**APPROVE, "comment": "ship it"})
        assert accepted.status_code == 202
        assert accepted.json()["status"] == "queued"
        assert accepted.json()["approval"]["reviewer"] == "release-manager"
        await h.idle()

        done = (await h.client.get(f"/v1/analyses/{job_id}")).json()
        after = (await h.client.get(f"/v1/analyses/{job_id}/decision")).json()
        events = [event.type for event in h.service.store.events(job_id)]

    assert done["status"] == "completed"
    assert done["run_count"] == 2
    assert after["result"]["approval"] == {
        "required": True,
        "status": "approved",
        "gate_decision_recorded": True,
    }
    # The decision shown for approval is the decision recorded after it.
    assert after["result"]["risk_assessment"] == before["result"]["risk_assessment"]
    assert after["result"]["report"]["decision"] == before["result"]["report"]["decision"]
    # Resume did not re-run the analysis, and restored the trace instead of zeroing it.
    assert len(llm.calls) == calls_before
    assert after["result"]["trace"]["llm_calls"] == before["result"]["trace"]["llm_calls"]
    assert after["result"]["trace"]["input_tokens"] == before["result"]["trace"]["input_tokens"]
    assert len(h.audit_lines()) == 1
    assert h.runtime.sessions == 2
    assert events == [
        JobEventType.JOB_QUEUED,
        JobEventType.JOB_RUNNING,
        JobEventType.JOB_WAITING_FOR_APPROVAL,
        JobEventType.JOB_APPROVAL_SUBMITTED,
        JobEventType.JOB_QUEUED,
        JobEventType.JOB_RUNNING,
        JobEventType.JOB_COMPLETED,
    ]


async def test_resume_uses_the_original_thread(
    harness: HarnessFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.jobs.runtime as runtime_module

    threads: dict[str, list[str]] = {"run": [], "resume": []}
    real_run, real_resume = runtime_module.run_analysis, runtime_module.resume_analysis

    async def spy_run(*args: Any, **kwargs: Any) -> Any:
        threads["run"].append(kwargs["thread_id"])
        return await real_run(*args, **kwargs)

    async def spy_resume(*args: Any, **kwargs: Any) -> Any:
        threads["resume"].append(kwargs["thread_id"])
        assert kwargs["resume_payload"] == {"approved": True}
        return await real_resume(*args, **kwargs)

    monkeypatch.setattr(runtime_module, "run_analysis", spy_run)
    monkeypatch.setattr(runtime_module, "resume_analysis", spy_resume)
    async with harness() as h:
        job_id = (await h.run_to_rest(GATED))["job_id"]
        await _approve(h, job_id)
        await h.idle()
    assert threads == {"run": [f"analysis-{job_id}"], "resume": [f"analysis-{job_id}"]}


async def test_duplicate_approval_is_rejected_and_writes_nothing(harness: HarnessFactory) -> None:
    async with harness() as h:
        job_id = (await h.run_to_rest(GATED))["job_id"]
        assert (await _approve(h, job_id)).status_code == 202
        await h.idle()
        again = await _approve(h, job_id)
        flipped = await _approve(h, job_id, {"approved": False, "reviewer": "someone-else"})
        await h.idle()
    for response in (again, flipped):
        assert response.status_code == 409
        error = response.json()["error"]
        assert error["code"] == "APPROVAL_ALREADY_SUBMITTED"
        assert error["details"] == {"job_id": job_id, "status": "completed", "approved": True}
    assert len(h.audit_lines()) == 1
    assert h.runtime.sessions == 2


async def test_concurrent_approvals_resume_exactly_once(harness: HarnessFactory) -> None:
    async with harness() as h:
        job_id = (await h.run_to_rest(GATED))["job_id"]
        responses = await asyncio.gather(*(_approve(h, job_id) for _ in range(8)))
        await h.idle()
    codes = sorted(response.status_code for response in responses)
    assert codes == [202] + [409] * 7
    for response in responses:
        if response.status_code == 409:
            assert response.json()["error"]["code"] in {
                "APPROVAL_ALREADY_SUBMITTED",
                "INVALID_JOB_TRANSITION",
            }
    assert len(h.audit_lines()) == 1
    assert h.runtime.sessions == 2


async def test_rejection_completes_without_an_audit_write(harness: HarnessFactory) -> None:
    async with harness() as h:
        job_id = (await h.run_to_rest(GATED))["job_id"]
        before = (await h.client.get(f"/v1/analyses/{job_id}/decision")).json()
        rejected = await _approve(h, job_id, {"approved": False, "reviewer": "qa-lead"})
        await h.idle()
        after = (await h.client.get(f"/v1/analyses/{job_id}/decision")).json()
    assert rejected.status_code == 202
    assert after["status"] == "completed"
    assert after["result"]["approval"]["status"] == "rejected"
    assert after["result"]["approval"]["gate_decision_recorded"] is False
    assert after["result"]["risk_assessment"] == before["result"]["risk_assessment"]
    assert h.audit_lines() == []


@pytest.mark.parametrize(
    "body",
    [
        {"approved": "true", "reviewer": "r"},
        {"approved": 1, "reviewer": "r"},
        {"approved": True},
        {"approved": True, "reviewer": ""},
        {"approved": True, "reviewer": "r\nINJECTED"},
        {"approved": True, "reviewer": "r", "decision": "PASS"},
        {"approved": True, "reviewer": "r", "risk_score": 0},
        {"approved": True, "reviewer": "r", "comment": "x" * 1001},
    ],
)
async def test_approval_payload_is_validated_strictly(
    harness: HarnessFactory,
    body: dict[str, Any],
) -> None:
    async with harness() as h:
        job_id = (await h.run_to_rest(GATED))["job_id"]
        response = await _approve(h, job_id, body)
        status = (await h.client.get(f"/v1/analyses/{job_id}")).json()
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "INVALID_REQUEST"
    assert "INJECTED" not in response.text
    assert status["status"] == "waiting_for_approval"
    assert status["approval"] is None
    assert h.audit_lines() == []


async def test_approval_without_a_gate_is_not_required(harness: HarnessFactory) -> None:
    async with harness() as h:
        job_id = (await h.run_to_rest())["job_id"]
        response = await _approve(h, job_id)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "APPROVAL_NOT_REQUIRED"
    assert h.runtime.sessions == 1


async def test_approval_while_running_is_an_invalid_transition(harness: HarnessFactory) -> None:
    llm = BlockingLLM()
    async with harness(runtime=FakeRuntime(llm)) as h:
        job_id = (await h.create(GATED)).json()["job_id"]
        await llm.entered.wait()
        early = await _approve(h, job_id)
        llm.release()
        await h.idle()
        status = (await h.client.get(f"/v1/analyses/{job_id}")).json()
    assert early.status_code == 409
    assert early.json()["error"]["code"] == "INVALID_JOB_TRANSITION"
    assert early.json()["error"]["details"] == {"job_id": job_id, "status": "running"}
    assert status["status"] == "waiting_for_approval"
    assert status["approval"] is None


async def test_approval_for_unknown_or_malformed_job(harness: HarnessFactory) -> None:
    async with harness() as h:
        missing = await _approve(h, "job-" + "0" * 32)
        malformed = await _approve(h, "../../etc")
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "JOB_NOT_FOUND"
    assert malformed.status_code in {404, 422}


async def test_approval_survives_a_restart(harness: HarnessFactory, tmp_path: Path) -> None:
    """Job metadata and the interrupt are durable: approve in a fresh app instance."""
    settings = api_settings(tmp_path)
    async with harness(settings=settings) as first:
        job_id = (await first.run_to_rest(GATED))["job_id"]
        before = (await first.client.get(f"/v1/analyses/{job_id}/decision")).json()
    async with harness(settings=settings) as second:
        status = (await second.client.get(f"/v1/analyses/{job_id}")).json()
        assert status["status"] == "waiting_for_approval"
        assert (await _approve(second, job_id)).status_code == 202
        await second.idle()
        after = (await second.client.get(f"/v1/analyses/{job_id}/decision")).json()
    assert after["status"] == "completed"
    assert after["result"]["approval"]["gate_decision_recorded"] is True
    assert after["result"]["risk_assessment"] == before["result"]["risk_assessment"]
    assert len(second.audit_lines()) == 1
    # The second instance only resumed; it never re-ran the analysis.
    assert second.runtime.sessions == 1


async def test_gate_write_failure_fails_the_job_but_keeps_the_decision(
    harness: HarnessFactory,
) -> None:
    runtime = FakeRuntime(
        wrap_mcp=lambda mcp: FailingToolMcp(mcp, failing_tool="record_gate_decision"),
    )
    async with harness(runtime=runtime) as h:
        job_id = (await h.run_to_rest(GATED))["job_id"]
        await _approve(h, job_id)
        await h.idle()
        status = (await h.client.get(f"/v1/analyses/{job_id}")).json()
        decision = await h.client.get(f"/v1/analyses/{job_id}/decision")
        again = await _approve(h, job_id)
    assert status["status"] == "failed"
    assert status["error"]["code"] == "MCP_ERROR"
    assert "simulated" not in str(status)
    # The decision computed before approval is still readable; nothing was recorded.
    assert decision.status_code == 200
    assert decision.json()["result"]["approval"]["gate_decision_recorded"] is False
    assert h.audit_lines() == []
    assert again.status_code == 409

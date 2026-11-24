"""Machine-readable errors, sanitization, input limits and local-only access."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest
from app.core.errors import ErrorCode, SentinelFlowError
from app.core.settings import Settings
from app.graph.state import AnalysisPlan
from app.jobs.models import JobStatus
from app.jobs.results import classify_degradation, classify_failure
from app.jobs.runtime import DefaultAnalysisRuntime, missing_provider_settings
from app.mcp_client.errors import ChangeSourceError, McpProtocolError
from app.model_gateway.errors import ProviderUnavailableError
from pydantic import SecretStr

from tests.api.conftest import FakeRuntime, HarnessFactory, api_settings
from tests.graph.conftest import static_llm_responses
from tests.graph.fakes import RaisingLLM

BODY = {"change_id": "CHG-H001", "domain_pack_id": "healthcare-claims"}
SECRETISH = "UNEXPECTED-FIELD-VALUE-MARKER"


def _error(response: httpx.Response) -> dict[str, Any]:
    payload: dict[str, Any] = response.json()
    assert set(payload) == {"error"}
    assert set(payload["error"]) == {"code", "message", "details", "request_id"}
    assert payload["error"]["request_id"] == response.headers["X-Request-ID"]
    error: dict[str, Any] = payload["error"]
    return error


@pytest.mark.parametrize(
    "body",
    [
        {**BODY, "unexpected": SECRETISH},
        {"domain_pack_id": "healthcare-claims"},
        {**BODY, "change_id": "../../etc/passwd"},
        {**BODY, "change_id": "x" * 129},
        {**BODY, "domain_pack_id": "pack/../other"},
        {**BODY, "record_decision": "true"},
        {**BODY, "execute_tests": 1},
        {**BODY, "source": "s3"},
        {**BODY, "objective": ""},
        {**BODY, "objective": "x" * 501},
    ],
)
async def test_invalid_analysis_requests_are_rejected_without_echo(
    harness: HarnessFactory,
    body: dict[str, Any],
) -> None:
    async with harness() as h:
        response = await h.client.post("/v1/analyses", json=body)
    assert response.status_code == 422
    error = _error(response)
    assert error["code"] == "INVALID_REQUEST"
    for problem in error["details"]["errors"]:
        assert set(problem) == {"loc", "type", "msg"}
    assert SECRETISH not in response.text
    assert "passwd" not in response.text
    assert h.runtime.sessions == 0
    assert h.runtime.ready_checks == []


async def test_malformed_json_is_invalid_request(harness: HarnessFactory) -> None:
    async with harness() as h:
        response = await h.client.post(
            "/v1/analyses",
            content=b'{"change_id": ',
            headers={"content-type": "application/json"},
        )
    assert response.status_code == 422
    assert _error(response)["code"] == "INVALID_REQUEST"


async def test_unknown_pack_and_change_are_distinct_not_found_errors(
    harness: HarnessFactory,
) -> None:
    async with harness() as h:
        pack = await h.create({"change_id": "CHG-H001", "domain_pack_id": "no-such-pack"})
        change = await h.create({"change_id": "CHG-H999", "domain_pack_id": "healthcare-claims"})
    assert pack.status_code == 404
    assert _error(pack) == {
        **_error(pack),
        "code": "DOMAIN_PACK_NOT_FOUND",
        "details": {"domain_pack_id": "no-such-pack"},
    }
    assert change.status_code == 404
    assert _error(change)["code"] == "CHANGE_NOT_FOUND"
    assert _error(change)["details"] == {
        "change_id": "CHG-H999",
        "domain_pack_id": "healthcare-claims",
    }
    # Loader details (filesystem paths) never leave the process.
    assert str(h.settings.domains_root) not in pack.text
    assert "domains" not in pack.text.replace("domain_pack", "")
    assert h.runtime.sessions == 0


async def test_github_source_requires_a_pr_change_id(harness: HarnessFactory) -> None:
    async with harness() as h:
        bad = await h.create({**BODY, "source": "github"})
    assert bad.status_code == 422
    assert _error(bad) == {
        **_error(bad),
        "code": "INVALID_REQUEST",
        "details": {"field": "change_id"},
    }


async def test_unknown_routes_and_methods_use_the_envelope(harness: HarnessFactory) -> None:
    async with harness() as h:
        route = await h.client.get("/v1/nothing-here")
        method = await h.client.delete("/v1/analyses")
        unknown_job = await h.client.get("/v1/analyses/job-" + "0" * 32)
        bad_id = await h.client.get("/v1/analyses/not-a-job-id")
    assert route.status_code == 404 and _error(route)["code"] == "NOT_FOUND"
    assert method.status_code == 405 and _error(method)["code"] == "METHOD_NOT_ALLOWED"
    assert unknown_job.status_code == 404 and _error(unknown_job)["code"] == "JOB_NOT_FOUND"
    assert bad_id.status_code == 422 and _error(bad_id)["code"] == "INVALID_REQUEST"


async def test_provider_not_configured_creates_no_job(harness: HarnessFactory) -> None:
    runtime = FakeRuntime(
        ready_error=SentinelFlowError(
            "Model provider configuration is incomplete for a live analysis",
            code=ErrorCode.PROVIDER_NOT_CONFIGURED,
            details={"missing_settings": ["GEMINI_API_KEY"]},
        ),
    )
    async with harness(runtime=runtime) as h:
        response = await h.create(BODY, key="k-unready")
        jobs = h.service.store.list_by_status(list(JobStatus))
        runtime.ready_error = None
        # The refused key was not consumed: the same request succeeds once configured.
        retry = await h.create(BODY, key="k-unready")
        await h.idle()
    assert response.status_code == 503
    assert _error(response)["details"] == {"missing_settings": ["GEMINI_API_KEY"]}
    assert jobs == []
    assert retry.status_code == 202


def test_default_runtime_names_missing_settings_without_values() -> None:
    bare = Settings(_env_file=None, gemini_api_key=None, openrouter_api_key=None)  # type: ignore[call-arg]
    assert missing_provider_settings(bare) == ["GEMINI_API_KEY"]
    openrouter_agents = Settings(  # type: ignore[call-arg]
        _env_file=None,
        gemini_api_key=SecretStr("test-gemini-value"),
        openrouter_api_key=None,
        agent_llm_model="vendor/model:free",
    )
    assert missing_provider_settings(openrouter_agents) == ["OPENROUTER_API_KEY"]
    with pytest.raises(SentinelFlowError) as caught:
        DefaultAnalysisRuntime(openrouter_agents).check_ready(None)  # type: ignore[arg-type]
    assert caught.value.code is ErrorCode.PROVIDER_NOT_CONFIGURED
    assert "test-gemini-value" not in repr(caught.value.details)


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (RuntimeError("429 RESOURCE_EXHAUSTED key=AIzaSyFAKEFAKEFAKE"), "PROVIDER_RATE_LIMITED"),
        (
            RuntimeError("401 UNAUTHENTICATED: API_KEY_INVALID AIzaSyFAKEFAKEFAKE"),
            "PROVIDER_AUTH_FAILED",
        ),
    ],
)
async def test_operational_provider_stop_fails_the_job_sanitized(
    harness: HarnessFactory,
    error: Exception,
    code: str,
) -> None:
    llm = RaisingLLM(static_llm_responses(), schema=AnalysisPlan, error=error)
    async with harness(runtime=FakeRuntime(llm)) as h:
        job_id = (await h.create(BODY)).json()["job_id"]
        await h.idle()
        status = await h.client.get(f"/v1/analyses/{job_id}")
        decision = await h.client.get(f"/v1/analyses/{job_id}/decision")
    assert status.json()["status"] == "failed"
    assert status.json()["error"]["code"] == code
    assert "AIzaSy" not in status.text
    assert decision.status_code == 409


async def test_provider_outage_degrades_instead_of_failing(harness: HarnessFactory) -> None:
    """Outage = degraded REVIEW (completed); quota/auth = failed. Distinguishable."""
    llm = RaisingLLM(
        static_llm_responses(),
        schema=AnalysisPlan,
        error=ProviderUnavailableError(
            "gemini unavailable: ServerError: 503 https://host?key=AIzaSyX"
        ),
    )
    async with harness(runtime=FakeRuntime(llm)) as h:
        status = await h.run_to_rest()
        decision = await h.client.get(status["links"]["result"])
    result = decision.json()["result"]
    assert status["status"] == "completed"
    assert result["risk_assessment"]["decision"] == "REVIEW"
    assert {"code": "provider_unavailable", "component": "triage_planner", "detail": None} in (
        result["degradation"]
    )
    assert "AIzaSy" not in decision.text


async def test_session_failure_fails_the_job_without_details(harness: HarnessFactory) -> None:
    runtime = FakeRuntime(session_error=RuntimeError("connect to db failed password=hunter2"))
    async with harness(runtime=runtime) as h:
        job_id = (await h.create(BODY)).json()["job_id"]
        await h.idle()
        status = await h.client.get(f"/v1/analyses/{job_id}")
    assert status.json()["error"]["code"] == "JOB_EXECUTION_FAILED"
    assert "hunter2" not in status.text


async def test_unexpected_errors_are_500_without_traces(
    harness: HarnessFactory,
    tmp_path: Path,
) -> None:
    runtime = FakeRuntime(ready_error=ValueError("Traceback secret-internal-detail"))
    settings = api_settings(tmp_path, debug=True)
    async with harness(runtime=runtime, settings=settings, raise_app_exceptions=False) as h:
        response = await h.create(BODY)
    assert response.status_code == 500
    assert _error(response)["code"] == "INTERNAL_ERROR"
    assert "secret-internal-detail" not in response.text
    assert "Traceback" not in response.text


async def test_oversized_bodies_are_refused_before_routing(harness: HarnessFactory) -> None:
    async def chunked() -> Any:
        for _ in range(4):
            yield b" " * 1024

    async with harness(api_max_body_bytes=2048) as h:
        declared = await h.client.post(
            "/v1/analyses",
            content=b"{" + b" " * 4096 + b"}",
            headers={"content-type": "application/json"},
        )
        streamed = await h.client.post(
            "/v1/analyses",
            content=chunked(),
            headers={"content-type": "application/json"},
        )
    for response in (declared, streamed):
        assert response.status_code == 413
        assert _error(response)["code"] == "REQUEST_TOO_LARGE"
        assert _error(response)["details"] == {"limit_bytes": 2048}
    assert h.runtime.sessions == 0


async def test_non_loopback_clients_are_refused_without_an_api_key(
    harness: HarnessFactory,
) -> None:
    async with harness(client=("203.0.113.7", 40000)) as h:
        create = await h.create(BODY)
        read = await h.client.get("/v1/analyses/job-" + "0" * 32)
        health = await h.client.get("/health")
    for response in (create, read):
        assert response.status_code == 403
        assert _error(response)["code"] == "NON_LOCAL_CLIENT"
    assert health.status_code == 200
    assert h.runtime.sessions == 0


async def test_api_key_is_required_from_every_client_when_configured(
    harness: HarnessFactory,
) -> None:
    key = "local-test-key-value"
    async with harness(client=("203.0.113.7", 40000), api_key=SecretStr(key)) as h:
        missing = await h.create(BODY)
        wrong = await h.client.post(
            "/v1/analyses",
            json=BODY,
            headers={"Authorization": "Bearer nope"},
        )
        right = await h.client.post(
            "/v1/analyses",
            json=BODY,
            headers={"Authorization": f"Bearer {key}"},
        )
        await h.idle()
    for response in (missing, wrong):
        assert response.status_code == 401
        assert _error(response)["code"] == "UNAUTHORIZED"
        assert key not in response.text
    assert right.status_code == 202


async def test_auth_is_checked_before_body_validation(harness: HarnessFactory) -> None:
    async with harness(client=("203.0.113.7", 40000)) as h:
        response = await h.client.post("/v1/analyses", json={"garbage": True})
    assert response.status_code == 403


def test_serve_refuses_public_bind_without_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.api.serve as serve

    calls: list[Any] = []
    monkeypatch.setattr(serve.uvicorn, "run", lambda *a, **k: calls.append((a, k)))
    monkeypatch.setattr(
        serve,
        "get_settings",
        lambda: Settings(_env_file=None, api_key=None),  # type: ignore[call-arg]
    )
    assert serve.main(["--host", "0.0.0.0"]) == 2
    assert calls == []
    assert serve.main([]) == 0
    assert calls[0][1]["host"] == "127.0.0.1"
    assert calls[0][1]["workers"] == 1


@pytest.mark.parametrize(
    ("text", "code", "component"),
    [
        ("incomplete_change_data: gitleaks unavailable", "incomplete_change_data", None),
        ("max_llm_calls exceeded", "budget_exhausted", None),
        ("security_auditor: max_input_tokens exceeded", "budget_exhausted", "security_auditor"),
        (
            "change_analyst failed: StructuredOutputError: x",
            "structured_output_invalid",
            "change_analyst",
        ),
        (
            "triage_planner failed: ProviderUnavailableError: y",
            "provider_unavailable",
            "triage_planner",
        ),
        ("evidence_verifier failed: TimeoutError: ", "timeout", "evidence_verifier"),
        ("repair failed: KeyError: 'z'", "component_failed", "repair"),
        ("repair: retrieval unavailable", "retrieval_unavailable", "repair"),
        ("triage plan invalid, clamped to default: boom", "triage_plan_clamped", "triage_planner"),
        ("record_gate_decision failed", "gate_decision_write_failed", "record_gate"),
        ("something new", "other", None),
    ],
)
def test_degradation_codes(text: str, code: str, component: str | None) -> None:
    reason = classify_degradation(text)
    assert reason.code == code
    assert reason.component == component
    if code != "incomplete_change_data":
        assert reason.detail is None


def test_failure_classification_unwraps_groups_and_respects_mcp_codes() -> None:
    grouped = ExceptionGroup("task group", [RuntimeError("429 RESOURCE_EXHAUSTED")])
    assert classify_failure(grouped).code is ErrorCode.PROVIDER_RATE_LIMITED
    # A JSON-RPC code on an MCP error is not an HTTP status.
    assert classify_failure(McpProtocolError("denied", code=403)).code is ErrorCode.MCP_ERROR
    assert classify_failure(ChangeSourceError("404 PR")).code is ErrorCode.CHANGE_SOURCE_UNAVAILABLE
    leaked = classify_failure(KeyError("internal-key-name"))
    assert leaked.code is ErrorCode.JOB_EXECUTION_FAILED
    assert "internal-key-name" not in leaked.message

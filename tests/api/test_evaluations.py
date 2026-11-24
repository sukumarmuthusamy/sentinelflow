"""Evaluation triggering over HTTP (deterministic fixture suite, offline)."""

from __future__ import annotations

import threading
from collections.abc import Sequence
from pathlib import Path

from app.jobs.evaluation import (
    EvaluationResult,
    EvaluationSuite,
    run_deterministic_fixture_suite,
)

from tests.api.conftest import HarnessFactory


async def test_evaluation_runs_every_pack(harness: HarnessFactory) -> None:
    async with harness() as h:
        created = await h.client.post("/v1/evaluations", json={})
        assert created.status_code == 202
        job_id = created.json()["job_id"]
        assert created.headers["Location"] == f"/v1/evaluations/{job_id}"
        await h.idle()
        status = (await h.client.get(f"/v1/evaluations/{job_id}")).json()
        result = (await h.client.get(f"/v1/evaluations/{job_id}/result")).json()["result"]
    assert status["status"] == "completed"
    assert status["kind"] == "evaluation"
    assert result["suite"] == "deterministic_fixtures"
    assert {row["domain_pack_id"] for row in result["rows"]} == {
        "ecommerce-payments",
        "healthcare-claims",
    }
    summary = result["summary"]
    assert summary["fixtures"] == len(result["rows"]) >= 8
    labelled = [row for row in result["rows"] if row["matches_expected"] is not None]
    assert summary["labelled"] == len(labelled)
    assert summary["matched"] == sum(1 for row in labelled if row["matches_expected"])
    assert summary["decision_accuracy"] == summary["matched"] / summary["labelled"]
    # No LLM or graph run was involved.
    assert h.runtime.sessions == 0


async def test_evaluation_of_selected_pack(harness: HarnessFactory) -> None:
    async with harness() as h:
        created = await h.client.post(
            "/v1/evaluations",
            json={"domain_pack_ids": ["healthcare-claims"]},
        )
        await h.idle()
        result = (await h.client.get(f"{created.headers['Location']}/result")).json()["result"]
    assert result["domain_pack_ids"] == ["healthcare-claims"]
    assert {row["domain_pack_id"] for row in result["rows"]} == {"healthcare-claims"}


async def test_evaluation_request_validation(harness: HarnessFactory) -> None:
    async with harness() as h:
        unknown_pack = await h.client.post(
            "/v1/evaluations",
            json={"domain_pack_ids": ["no-such-pack"]},
        )
        unknown_suite = await h.client.post("/v1/evaluations", json={"suite": "graph_sample"})
        empty = await h.client.post("/v1/evaluations", json={"domain_pack_ids": []})
        extra = await h.client.post("/v1/evaluations", json={"suite_args": {"x": 1}})
    assert unknown_pack.status_code == 404
    assert unknown_pack.json()["error"]["code"] == "DOMAIN_PACK_NOT_FOUND"
    assert unknown_pack.json()["error"]["details"] == {"domain_pack_id": "no-such-pack"}
    for response in (unknown_suite, empty, extra):
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "INVALID_REQUEST"


async def test_evaluation_result_before_completion(harness: HarnessFactory) -> None:
    started = threading.Event()
    release = threading.Event()

    def slow_runner(
        suite: EvaluationSuite,
        pack_ids: Sequence[str],
        domains_root: Path,
    ) -> EvaluationResult:
        started.set()
        release.wait(timeout=10)
        return run_deterministic_fixture_suite(suite, pack_ids, domains_root)

    async with harness(evaluation_runner=slow_runner) as h:
        job_id = (await h.client.post("/v1/evaluations", json={})).json()["job_id"]
        while not started.is_set():
            await h.client.get("/health")
        early = await h.client.get(f"/v1/evaluations/{job_id}/result")
        release.set()
        await h.idle()
        late = await h.client.get(f"/v1/evaluations/{job_id}/result")
    assert early.status_code == 409
    assert early.json()["error"]["code"] == "RESULT_NOT_AVAILABLE"
    assert late.status_code == 200


async def test_evaluation_failure_is_sanitized(harness: HarnessFactory) -> None:
    def broken_runner(
        suite: EvaluationSuite,
        pack_ids: Sequence[str],
        domains_root: Path,
    ) -> EvaluationResult:
        msg = f"cannot read {domains_root} token=ghp_SECRETSECRET"
        raise OSError(msg)

    async with harness(evaluation_runner=broken_runner) as h:
        job_id = (await h.client.post("/v1/evaluations", json={})).json()["job_id"]
        await h.idle()
        status = await h.client.get(f"/v1/evaluations/{job_id}")
    body = status.json()
    assert body["status"] == "failed"
    assert body["error"]["code"] == "JOB_EXECUTION_FAILED"
    assert "ghp_SECRET" not in status.text
    assert "cannot read" not in status.text


async def test_evaluation_ids_do_not_resolve_as_analyses(harness: HarnessFactory) -> None:
    async with harness() as h:
        job_id = (await h.client.post("/v1/evaluations", json={})).json()["job_id"]
        await h.idle()
        as_analysis = await h.client.get(f"/v1/analyses/{job_id}")
    assert as_analysis.status_code == 404
    assert as_analysis.json()["error"]["code"] == "JOB_NOT_FOUND"

"""Idempotency-Key semantics, concurrent creates and the active-job cap."""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path

from app.graph.state import AnalysisPlan
from app.jobs.models import JobKind, JobRecord, JobStatus
from app.model_gateway.llm import StaticStructuredLLM
from app.persistence.job_store import CreateOutcome, SqliteJobStore, utc_now

from tests.api.conftest import BlockingLLM, FakeRuntime, HarnessFactory
from tests.graph.conftest import static_llm_responses
from tests.graph.fakes import RaisingLLM

BODY = {"change_id": "CHG-H001", "domain_pack_id": "healthcare-claims"}


async def test_same_key_same_request_resolves_to_same_job(harness: HarnessFactory) -> None:
    llm = StaticStructuredLLM(static_llm_responses())
    async with harness(runtime=FakeRuntime(llm)) as h:
        first = await h.create(BODY, key="release-42")
        await h.idle()
        calls_after_first = len(llm.calls)
        again = await h.create(BODY, key="release-42")
        await h.idle()
    assert first.status_code == 202
    assert "Idempotent-Replayed" not in first.headers
    assert again.status_code == 200
    assert again.headers["Idempotent-Replayed"] == "true"
    assert again.json()["job_id"] == first.json()["job_id"]
    assert again.json()["status"] == "completed"
    # The replay launched nothing: one graph execution, no further LLM calls.
    assert h.runtime.sessions == 1
    assert len(llm.calls) == calls_after_first


async def test_spelled_out_defaults_are_the_same_request(harness: HarnessFactory) -> None:
    explicit = {
        **BODY,
        "objective": "Assess release risk and generate the minimum sufficient regression plan",
        "execute_tests": False,
        "record_decision": False,
        "source": "fixture",
    }
    async with harness() as h:
        first = await h.create(BODY, key="k-defaults")
        again = await h.create(explicit, key="k-defaults")
        await h.idle()
    assert again.status_code == 200
    assert again.json()["job_id"] == first.json()["job_id"]


async def test_same_key_different_request_is_a_conflict(harness: HarnessFactory) -> None:
    async with harness() as h:
        first = await h.create(BODY, key="k-conflict")
        other = await h.create({**BODY, "change_id": "CHG-H003"}, key="k-conflict")
        await h.idle()
    assert first.status_code == 202
    assert other.status_code == 409
    error = other.json()["error"]
    assert error["code"] == "IDEMPOTENCY_KEY_CONFLICT"
    assert error["details"] == {}
    assert error["request_id"] == other.headers["X-Request-ID"]
    assert h.runtime.sessions == 1


async def test_concurrent_requests_with_one_key_launch_one_analysis(
    harness: HarnessFactory,
) -> None:
    llm = BlockingLLM()
    async with harness(runtime=FakeRuntime(llm), api_max_active_jobs=20) as h:
        responses = await asyncio.gather(*(h.create(BODY, key="k-race") for _ in range(12)))
        llm.release()
        await h.idle()
    codes = sorted(response.status_code for response in responses)
    assert codes == [200] * 11 + [202]
    assert len({response.json()["job_id"] for response in responses}) == 1
    assert h.runtime.sessions == 1
    assert len(h.service.store.list_by_status(list(JobStatus))) == 1


async def test_concurrent_mixed_requests_with_one_key(harness: HarnessFactory) -> None:
    """Racing different bodies on one key: one job, the rest replay or conflict."""
    llm = BlockingLLM()
    bodies = [BODY, {**BODY, "change_id": "CHG-H003"}] * 4
    async with harness(runtime=FakeRuntime(llm), api_max_active_jobs=20) as h:
        responses = await asyncio.gather(*(h.create(body, key="k-mixed") for body in bodies))
        llm.release()
        await h.idle()
    created = [response for response in responses if response.status_code == 202]
    assert len(created) == 1
    winner = created[0].json()
    for response in responses:
        if response.status_code == 200:
            assert response.json()["job_id"] == winner["job_id"]
        elif response.status_code == 409:
            assert response.json()["error"]["code"] == "IDEMPOTENCY_KEY_CONFLICT"
    assert h.runtime.sessions == 1


async def test_retry_of_a_failed_job_does_not_rerun_it(harness: HarnessFactory) -> None:
    """A retry after a timeout must not silently repeat expensive work."""
    llm = RaisingLLM(
        static_llm_responses(),
        schema=AnalysisPlan,
        error=RuntimeError("429 RESOURCE_EXHAUSTED"),
    )
    async with harness(runtime=FakeRuntime(llm)) as h:
        first = await h.create(BODY, key="k-failed")
        await h.idle()
        retry = await h.create(BODY, key="k-failed")
        await h.idle()
    assert retry.status_code == 200
    assert retry.json()["status"] == "failed"
    assert retry.json()["error"]["code"] == "PROVIDER_RATE_LIMITED"
    assert retry.json()["job_id"] == first.json()["job_id"]
    assert h.runtime.sessions == 1


async def test_requests_without_a_key_are_independent(harness: HarnessFactory) -> None:
    async with harness() as h:
        first = await h.create(BODY)
        second = await h.create(BODY)
        await h.idle()
    assert first.status_code == second.status_code == 202
    assert first.json()["job_id"] != second.json()["job_id"]
    assert h.runtime.sessions == 2


async def test_keys_are_scoped_per_job_kind(harness: HarnessFactory) -> None:
    async with harness() as h:
        analysis = await h.create(BODY, key="shared-key")
        evaluation = await h.client.post(
            "/v1/evaluations",
            json={},
            headers={"Idempotency-Key": "shared-key"},
        )
        await h.idle()
    assert analysis.status_code == 202
    assert evaluation.status_code == 202
    assert evaluation.json()["job_id"] != analysis.json()["job_id"]


async def test_malformed_key_is_rejected(harness: HarnessFactory) -> None:
    async with harness() as h:
        for bad in ("", "has space", "x" * 129, "-leading-dash"):
            response = await h.create(BODY, key=bad)
            assert response.status_code == 422, bad
            assert response.json()["error"]["details"] == {"field": "Idempotency-Key"}
    assert h.runtime.sessions == 0


async def test_active_job_cap_refuses_new_work_but_not_replays(harness: HarnessFactory) -> None:
    llm = BlockingLLM()
    async with harness(runtime=FakeRuntime(llm), api_max_active_jobs=1) as h:
        first = await h.create(BODY, key="k-cap")
        refused = await h.create({**BODY, "change_id": "CHG-H003"}, key="k-other")
        replay = await h.create(BODY, key="k-cap")
        llm.release()
        await h.idle()
        # The refused key was never stored: once capacity frees up it creates a job.
        later = await h.create({**BODY, "change_id": "CHG-H003"}, key="k-other")
        await h.idle()
    assert first.status_code == 202
    assert refused.status_code == 429
    assert refused.json()["error"]["code"] == "TOO_MANY_ACTIVE_JOBS"
    assert refused.json()["error"]["details"] == {"max_active_jobs": 1}
    assert replay.status_code == 200
    assert later.status_code == 202


def _record(job_id: str) -> JobRecord:
    now = utc_now()
    return JobRecord(
        job_id=job_id,
        kind=JobKind.ANALYSIS,
        status=JobStatus.QUEUED,
        request={"change_id": "CHG-H001"},
        request_fingerprint="f" * 64,
        created_at=now,
        updated_at=now,
    )


def test_store_reservation_is_atomic_across_connections(tmp_path: Path) -> None:
    """Threads with their own connections race one key: exactly one insert wins."""
    store = SqliteJobStore(tmp_path / "jobs.sqlite")
    store.initialize()
    barrier = threading.Barrier(8)
    outcomes: list[CreateOutcome] = []
    lock = threading.Lock()

    def worker(index: int) -> None:
        barrier.wait()
        outcome = store.create(
            _record(f"job-{index:032x}"),
            idempotency_scope="analysis",
            idempotency_key_hash="a" * 64,
            max_active=100,
        )
        with lock:
            outcomes.append(outcome)

    threads = [threading.Thread(target=worker, args=(index,)) for index in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sum(outcome.created for outcome in outcomes) == 1
    assert len({outcome.record.job_id for outcome in outcomes}) == 1
    assert len(store.list_by_status([JobStatus.QUEUED])) == 1
    winner = next(outcome.record.job_id for outcome in outcomes if outcome.created)
    assert [event.seq for event in store.events(winner)] == [1]


def test_raw_idempotency_key_is_not_persisted(tmp_path: Path) -> None:
    import sqlite3

    from app.jobs.service import idempotency_key_hash

    store = SqliteJobStore(tmp_path / "jobs.sqlite")
    store.initialize()
    store.create(
        _record("job-" + "1" * 32),
        idempotency_scope="analysis",
        idempotency_key_hash=idempotency_key_hash("customer-visible-key"),
        max_active=10,
    )
    dump = "\n".join(sqlite3.connect(tmp_path / "jobs.sqlite").iterdump())
    assert "customer-visible-key" not in dump

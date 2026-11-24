"""Restart and shutdown semantics: nothing is silently re-run, queued work resumes."""

from __future__ import annotations

from pathlib import Path

import pytest
from app.jobs.events import JobEventType
from app.jobs.models import (
    ALLOWED_TRANSITIONS,
    ApprovalRecord,
    JobKind,
    JobRecord,
    JobStatus,
    is_allowed_transition,
)
from app.jobs.service import request_fingerprint
from app.persistence.job_store import SqliteJobStore, utc_now

from tests.api.conftest import BlockingLLM, FakeRuntime, HarnessFactory, api_settings

REQUEST = {
    "change_id": "CHG-H001",
    "domain_pack_id": "healthcare-claims",
    "objective": "Assess release risk and generate the minimum sufficient regression plan",
    "execute_tests": False,
    "record_decision": False,
    "source": "fixture",
}


def _seed(store: SqliteJobStore, job_id: str) -> None:
    now = utc_now()
    store.initialize()
    store.create(
        JobRecord(
            job_id=job_id,
            kind=JobKind.ANALYSIS,
            status=JobStatus.QUEUED,
            request=REQUEST,
            request_fingerprint=request_fingerprint(JobKind.ANALYSIS, REQUEST),
            thread_id=f"analysis-{job_id}",
            created_at=now,
            updated_at=now,
        ),
        max_active=10,
    )


async def test_running_job_found_at_startup_is_interrupted_not_rerun(
    harness: HarnessFactory,
    tmp_path: Path,
) -> None:
    settings = api_settings(tmp_path)
    store = SqliteJobStore(settings.api_job_db_path)
    job_id = "job-" + "a" * 32
    _seed(store, job_id)
    store.transition(
        job_id,
        expected=JobStatus.QUEUED,
        target=JobStatus.RUNNING,
        events=[(JobEventType.JOB_RUNNING, {"phase": "start"})],
    )
    async with harness(settings=settings) as h:
        await h.idle()
        status = (await h.client.get(f"/v1/analyses/{job_id}")).json()
    assert status["status"] == "failed"
    assert status["error"]["code"] == "JOB_INTERRUPTED"
    assert h.runtime.sessions == 0


async def test_queued_job_found_at_startup_is_relaunched(
    harness: HarnessFactory,
    tmp_path: Path,
) -> None:
    settings = api_settings(tmp_path)
    job_id = "job-" + "b" * 32
    _seed(SqliteJobStore(settings.api_job_db_path), job_id)
    async with harness(settings=settings) as h:
        await h.idle()
        status = (await h.client.get(f"/v1/analyses/{job_id}")).json()
    assert status["status"] == "completed"
    assert h.runtime.sessions == 1


async def test_recovery_can_be_disabled(harness: HarnessFactory, tmp_path: Path) -> None:
    settings = api_settings(tmp_path, api_recover_jobs_on_startup=False)
    job_id = "job-" + "c" * 32
    _seed(SqliteJobStore(settings.api_job_db_path), job_id)
    async with harness(settings=settings) as h:
        await h.idle()
        status = (await h.client.get(f"/v1/analyses/{job_id}")).json()
    assert status["status"] == "queued"
    assert h.runtime.sessions == 0


async def test_accepted_approval_resumes_after_a_restart(
    harness: HarnessFactory,
    tmp_path: Path,
) -> None:
    """Crash after the approval was accepted, before its resume started."""
    settings = api_settings(tmp_path)
    gated = {**REQUEST, "record_decision": True}
    async with harness(settings=settings) as first:
        created = await first.create(gated)
        await first.idle()
        job_id = created.json()["job_id"]
    # Simulate the accepted approval whose resume task never ran.
    SqliteJobStore(settings.api_job_db_path).transition(
        job_id,
        expected=JobStatus.WAITING_FOR_APPROVAL,
        target=JobStatus.QUEUED,
        events=[(JobEventType.JOB_APPROVAL_SUBMITTED, {"approved": True})],
        approval=ApprovalRecord(approved=True, reviewer="ops", submitted_at=utc_now()),
    )
    async with harness(settings=settings) as second:
        await second.idle()
        decision = (await second.client.get(f"/v1/analyses/{job_id}/decision")).json()
    assert decision["status"] == "completed"
    assert decision["result"]["approval"]["gate_decision_recorded"] is True
    assert len(second.audit_lines()) == 1
    assert second.runtime.sessions == 1


async def test_shutdown_marks_running_job_interrupted(
    harness: HarnessFactory, tmp_path: Path
) -> None:
    settings = api_settings(tmp_path)
    llm = BlockingLLM()
    async with harness(settings=settings, runtime=FakeRuntime(llm)) as h:
        job_id = (await h.create()).json()["job_id"]
        await llm.entered.wait()
    record = SqliteJobStore(settings.api_job_db_path).get(job_id)
    assert record is not None
    assert record.status is JobStatus.FAILED
    assert record.error is not None and record.error.code.value == "JOB_INTERRUPTED"


def test_transition_table_is_closed() -> None:
    for terminal in (JobStatus.COMPLETED, JobStatus.FAILED):
        assert ALLOWED_TRANSITIONS[terminal] == frozenset()
    assert not is_allowed_transition(JobStatus.WAITING_FOR_APPROVAL, JobStatus.COMPLETED)
    assert not is_allowed_transition(JobStatus.WAITING_FOR_APPROVAL, JobStatus.RUNNING)
    assert not is_allowed_transition(JobStatus.QUEUED, JobStatus.COMPLETED)
    assert set(ALLOWED_TRANSITIONS) == set(JobStatus)


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (JobStatus.COMPLETED, JobStatus.RUNNING),
        (JobStatus.FAILED, JobStatus.QUEUED),
        (JobStatus.QUEUED, JobStatus.WAITING_FOR_APPROVAL),
        (JobStatus.WAITING_FOR_APPROVAL, JobStatus.COMPLETED),
    ],
)
def test_store_refuses_illegal_transitions(
    tmp_path: Path,
    current: JobStatus,
    target: JobStatus,
) -> None:
    store = SqliteJobStore(tmp_path / "jobs.sqlite")
    with pytest.raises(ValueError, match="illegal job transition"):
        store.transition("job-x", expected=current, target=target, events=[])


def test_compare_and_set_loses_cleanly(tmp_path: Path) -> None:
    store = SqliteJobStore(tmp_path / "jobs.sqlite")
    job_id = "job-" + "d" * 32
    _seed(store, job_id)
    acquire = {
        "expected": JobStatus.QUEUED,
        "target": JobStatus.RUNNING,
        "events": [(JobEventType.JOB_RUNNING, {})],
    }
    assert store.transition(job_id, **acquire) is not None  # type: ignore[arg-type]
    assert store.transition(job_id, **acquire) is None  # type: ignore[arg-type]
    assert [event.seq for event in store.events(job_id)] == [1, 2]

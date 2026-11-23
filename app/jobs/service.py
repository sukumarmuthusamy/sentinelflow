"""Job service: idempotent creation, execution, approval and recovery (ADR-0003).

Layering. Routes (``app.api``) translate HTTP to calls here and errors back to HTTP.
This service owns lifecycle rules and launches work; the store owns atomicity; the
``GraphExecutor`` is the only thing that touches LangGraph. M5-B adds streaming by
subscribing to the store's events and to executor callbacks, not by changing routes.

Execution. Each job runs as an asyncio task in this process, gated by a semaphore of
``api_max_concurrent_runs``. A job is acquired by a ``queued -> running``
compare-and-set immediately before execution, so two launchers (an in-process task and
startup recovery, say) can never both run it.

Guarantees and limits are listed in ADR-0003. In short: creation with an
``Idempotency-Key`` is at-most-once per key. Execution is at-most-once per job, and
nothing is retried automatically. Approval is at-most-once per job.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any

from app.core.domain_pack import list_domain_pack_ids, load_domain_pack
from app.core.errors import ErrorCode, SentinelFlowError
from app.core.settings import Settings
from app.deterministic.evaluate import resolve_fixture_directory
from app.graph.state import AnalysisRequest
from app.jobs.evaluation import (
    EvaluationRunner,
    EvaluationSuite,
    run_deterministic_fixture_suite,
)
from app.jobs.events import JobEventType
from app.jobs.models import ApprovalRecord, JobError, JobKind, JobRecord, JobStatus
from app.jobs.results import (
    AnalysisResult,
    build_analysis_result,
    classify_failure,
    job_error,
    root_exception,
)
from app.jobs.runtime import AnalysisRuntime, GraphExecutor, GraphOutcome
from app.persistence.job_store import CreateOutcome, SqliteJobStore, utc_now

logger = logging.getLogger(__name__)

# Same shape the GitHub change source accepts (mcp_server github_source).
_GITHUB_CHANGE_ID_PREFIX = "PR-"


def request_fingerprint(kind: JobKind, payload: dict[str, Any]) -> str:
    """Hash of the *validated* request, defaults filled in.

    Two bodies that differ only by spelling out a default are the same request.
    """
    canonical = json.dumps(
        {"kind": kind.value, "request": payload},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def idempotency_key_hash(key: str) -> str:
    """Keys are stored hashed: the raw client value is never persisted or logged."""
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class RecoveryReport:
    interrupted: list[str]
    relaunched: list[str]


class JobService:
    def __init__(
        self,
        *,
        settings: Settings,
        store: SqliteJobStore,
        runtime: AnalysisRuntime,
        evaluation_runner: EvaluationRunner = run_deterministic_fixture_suite,
    ) -> None:
        self._settings = settings
        self._store = store
        self._runtime = runtime
        self._executor = GraphExecutor(runtime, settings)
        self._evaluation_runner = evaluation_runner
        self._slots = asyncio.Semaphore(settings.api_max_concurrent_runs)
        self._tasks: set[asyncio.Task[None]] = set()

    @property
    def store(self) -> SqliteJobStore:
        return self._store

    # -- lifecycle of the service itself --------------------------------------

    async def start(self) -> RecoveryReport | None:
        await asyncio.to_thread(self._store.initialize)
        if self._settings.api_recover_jobs_on_startup:
            return await self.recover()
        return None

    async def shutdown(self) -> None:
        """Cancel in-flight work; each cancelled job is marked ``JOB_INTERRUPTED``."""
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def wait_idle(self) -> None:
        """Wait until no job task is running (tests and orderly shutdown)."""
        while pending := [task for task in self._tasks if not task.done()]:
            await asyncio.gather(*pending, return_exceptions=True)

    async def recover(self) -> RecoveryReport:
        """Restart semantics for one API process per job database.

        * ``running``: the process died mid-execution. The job is marked failed
          (``JOB_INTERRUPTED``) and is **not** re-run: re-running would repeat
          provider calls silently. The LangGraph checkpoint is left in place.
        * ``queued``: no graph work had started (the move to ``running`` happens
          right before execution), so the job is relaunched. That includes approved
          resumes whose execution had not begun.
        * ``waiting_for_approval``: untouched. The interrupt lives in the checkpoint
          database, so an approval after a restart resumes the original thread.
        """
        interrupted: list[str] = []
        for job in await asyncio.to_thread(self._store.list_by_status, [JobStatus.RUNNING]):
            if self._fail_sync(job.job_id, JobStatus.RUNNING, job_error(ErrorCode.JOB_INTERRUPTED)):
                interrupted.append(job.job_id)
        relaunched: list[str] = []
        for job in await asyncio.to_thread(self._store.list_by_status, [JobStatus.QUEUED]):
            self._launch(job.job_id)
            relaunched.append(job.job_id)
        if interrupted or relaunched:
            logger.info(
                "job recovery: %d interrupted, %d relaunched",
                len(interrupted),
                len(relaunched),
            )
        return RecoveryReport(interrupted=interrupted, relaunched=relaunched)

    # -- queries ---------------------------------------------------------------

    async def get_job(self, job_id: str, *, kind: JobKind) -> JobRecord:
        job = await asyncio.to_thread(self._store.get, job_id)
        if job is None or job.kind is not kind:
            raise SentinelFlowError(
                "Job not found",
                code=ErrorCode.JOB_NOT_FOUND,
                details={"job_id": job_id},
            )
        return job

    # -- creation --------------------------------------------------------------

    async def create_analysis(
        self,
        request: AnalysisRequest,
        *,
        idempotency_key: str | None,
    ) -> CreateOutcome:
        payload = request.model_dump(mode="json")
        fingerprint = request_fingerprint(JobKind.ANALYSIS, payload)

        async def prepare() -> None:
            await asyncio.to_thread(self._validate_analysis_request, request)
            self._runtime.check_ready(request)

        job_id = f"job-{uuid.uuid4().hex}"
        return await self._create(
            kind=JobKind.ANALYSIS,
            job_id=job_id,
            payload=payload,
            fingerprint=fingerprint,
            idempotency_key=idempotency_key,
            prepare=prepare,
            # One LangGraph thread per job: reusing one would merge reducer state.
            thread_id=f"analysis-{job_id}",
        )

    async def create_evaluation(
        self,
        suite: EvaluationSuite,
        domain_pack_ids: Sequence[str] | None,
        *,
        idempotency_key: str | None,
    ) -> CreateOutcome:
        payload: dict[str, Any] = {
            "suite": suite.value,
            "domain_pack_ids": list(domain_pack_ids) if domain_pack_ids is not None else None,
        }
        fingerprint = request_fingerprint(JobKind.EVALUATION, payload)

        async def prepare() -> None:
            await asyncio.to_thread(self._validate_pack_ids, domain_pack_ids)

        return await self._create(
            kind=JobKind.EVALUATION,
            job_id=f"job-{uuid.uuid4().hex}",
            payload=payload,
            fingerprint=fingerprint,
            idempotency_key=idempotency_key,
            prepare=prepare,
            thread_id=None,
        )

    async def _create(
        self,
        *,
        kind: JobKind,
        job_id: str,
        payload: dict[str, Any],
        fingerprint: str,
        idempotency_key: str | None,
        prepare: Callable[[], Awaitable[None]],
        thread_id: str | None,
    ) -> CreateOutcome:
        key_hash = idempotency_key_hash(idempotency_key) if idempotency_key else None
        scope = kind.value
        # A replay is answered from the store before validation, so a retry of an
        # accepted request still resolves after configuration changes.
        if key_hash is not None:
            existing = await asyncio.to_thread(self._store.find_by_idempotency, scope, key_hash)
            if existing is not None:
                return CreateOutcome(record=_replay(existing, fingerprint), created=False)
        await prepare()
        now = utc_now()
        record = JobRecord(
            job_id=job_id,
            kind=kind,
            status=JobStatus.QUEUED,
            request=payload,
            request_fingerprint=fingerprint,
            thread_id=thread_id,
            created_at=now,
            updated_at=now,
        )
        outcome = await asyncio.to_thread(
            self._store.create,
            record,
            idempotency_scope=scope,
            idempotency_key_hash=key_hash,
            max_active=self._settings.api_max_active_jobs,
        )
        if not outcome.created:
            # Lost the race to a concurrent request with the same key: nothing launched.
            return CreateOutcome(record=_replay(outcome.record, fingerprint), created=False)
        logger.info("job %s queued (%s)", job_id, kind.value)
        self._launch(job_id)
        return outcome

    def _validate_pack_ids(self, pack_ids: Sequence[str] | None) -> None:
        known = set(list_domain_pack_ids(domains_root=self._settings.domains_root))
        for pack_id in pack_ids or []:
            if pack_id not in known:
                raise _pack_not_found(pack_id)
            self._load_pack(pack_id)

    def _load_pack(self, pack_id: str) -> None:
        try:
            load_domain_pack(pack_id, domains_root=self._settings.domains_root)
        except SentinelFlowError as exc:
            # The loader's details carry filesystem paths and parser text: drop them.
            if exc.code is ErrorCode.DOMAIN_PACK_NOT_FOUND:
                raise _pack_not_found(pack_id) from None
            raise SentinelFlowError(
                "Domain pack failed validation",
                code=ErrorCode.DOMAIN_PACK_INVALID,
                details={"domain_pack_id": pack_id},
            ) from None

    def _validate_analysis_request(self, request: AnalysisRequest) -> None:
        self._validate_pack_ids([request.domain_pack_id])
        root = self._settings.domains_root
        if request.source == "fixture":
            try:
                resolve_fixture_directory(
                    request.change_id,
                    pack_id=request.domain_pack_id,
                    domains_root=root,
                )
            except FileNotFoundError:
                raise SentinelFlowError(
                    "Change fixture not found in this domain pack",
                    code=ErrorCode.CHANGE_NOT_FOUND,
                    details={
                        "change_id": request.change_id,
                        "domain_pack_id": request.domain_pack_id,
                    },
                ) from None
            return
        if not (root / request.domain_pack_id / "live_source.yaml").is_file():
            raise SentinelFlowError(
                "This domain pack has no live change source configured",
                code=ErrorCode.CHANGE_SOURCE_NOT_CONFIGURED,
                details={"domain_pack_id": request.domain_pack_id, "source": request.source},
            )
        # Whether the pull request exists is only known at run time (a failed job
        # with CHANGE_SOURCE_UNAVAILABLE); the shape is checked here.
        if not request.change_id.startswith(_GITHUB_CHANGE_ID_PREFIX):
            raise SentinelFlowError(
                "GitHub change_id must look like 'PR-<number>'",
                code=ErrorCode.INVALID_REQUEST,
                details={"field": "change_id"},
            )

    # -- approval --------------------------------------------------------------

    async def submit_approval(
        self,
        job_id: str,
        *,
        approved: bool,
        reviewer: str,
        comment: str | None,
    ) -> JobRecord:
        job = await self.get_job(job_id, kind=JobKind.ANALYSIS)
        _check_approvable(job)
        approval = ApprovalRecord(
            approved=approved,
            reviewer=reviewer,
            comment=comment,
            submitted_at=utc_now(),
        )
        updated = await asyncio.to_thread(
            self._store.transition,
            job_id,
            expected=JobStatus.WAITING_FOR_APPROVAL,
            target=JobStatus.QUEUED,
            events=[
                (JobEventType.JOB_APPROVAL_SUBMITTED, {"approved": approved}),
                (JobEventType.JOB_QUEUED, {"phase": "resume"}),
            ],
            approval=approval,
        )
        if updated is None:
            # Another approval won the compare-and-set between the read and the write.
            current = await self.get_job(job_id, kind=JobKind.ANALYSIS)
            _check_approvable(current)
            raise _invalid_transition(current)
        logger.info("job %s approval submitted (approved=%s)", job_id, approved)
        self._launch(job_id)
        return updated

    # -- execution -------------------------------------------------------------

    def _launch(self, job_id: str) -> None:
        task = asyncio.create_task(self._run_job(job_id), name=f"sentinelflow-{job_id}")
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _run_job(self, job_id: str) -> None:
        async with self._slots:
            job = await asyncio.to_thread(self._store.get, job_id)
            if job is None or job.status is not JobStatus.QUEUED:
                return
            resume = job.is_resume_pending
            acquired = await asyncio.to_thread(self._acquire, job_id, resume=resume)
            if acquired is None:
                return  # another launcher acquired it
            try:
                if acquired.kind is JobKind.EVALUATION:
                    await self._execute_evaluation(acquired)
                elif resume:
                    await self._execute_resume(acquired)
                else:
                    await self._execute_start(acquired)
            except asyncio.CancelledError:
                # Synchronous write: the task is being cancelled, so avoid awaiting.
                self._fail_sync(job_id, JobStatus.RUNNING, job_error(ErrorCode.JOB_INTERRUPTED))
                raise
            except Exception as exc:  # noqa: BLE001 — every failure becomes a job state
                error = classify_failure(exc)
                # Exception type only: messages can carry provider or tool internals.
                logger.warning(
                    "job %s failed: %s (%s)",
                    job_id,
                    error.code.value,
                    type(root_exception(exc)).__name__,
                )
                await asyncio.to_thread(self._fail_sync, job_id, JobStatus.RUNNING, error)

    def _acquire(self, job_id: str, *, resume: bool) -> JobRecord | None:
        """``queued -> running`` compare-and-set; ``None`` if another launcher won."""
        events = [(JobEventType.JOB_RUNNING, {"phase": "resume" if resume else "start"})]
        if resume:
            # started_at keeps the first run's time; run_count tells the runs apart.
            return self._store.transition(
                job_id,
                expected=JobStatus.QUEUED,
                target=JobStatus.RUNNING,
                events=events,
                increment_run_count=True,
            )
        return self._store.transition(
            job_id,
            expected=JobStatus.QUEUED,
            target=JobStatus.RUNNING,
            events=events,
            increment_run_count=True,
            started_at=utc_now(),
        )

    async def _execute_start(self, job: JobRecord) -> None:
        request = AnalysisRequest.model_validate(job.request)
        assert job.thread_id is not None
        outcome = await self._executor.start(
            request,
            thread_id=job.thread_id,
            analysis_id=job.job_id,
        )
        await self._settle_analysis(job, outcome, prior=None)

    async def _execute_resume(self, job: JobRecord) -> None:
        assert job.thread_id is not None and job.approval is not None and job.result is not None
        request = AnalysisRequest.model_validate(job.request)
        prior = AnalysisResult.model_validate(job.result)
        outcome = await self._executor.resume(
            request,
            thread_id=job.thread_id,
            approved=job.approval.approved,
        )
        await self._settle_analysis(job, outcome, prior=prior)

    async def _settle_analysis(
        self,
        job: JobRecord,
        outcome: GraphOutcome,
        *,
        prior: AnalysisResult | None,
    ) -> None:
        result = build_analysis_result(outcome.state, analysis_id=job.job_id)
        if prior is not None:
            # Approval gates the audit write; it must never move the decision.
            before = (prior.risk_assessment.decision, prior.risk_assessment.risk_score)
            after = (result.risk_assessment.decision, result.risk_assessment.risk_score)
            if before != after or outcome.interrupted:
                logger.error("job %s: decision changed or re-interrupted on resume", job.job_id)
                await asyncio.to_thread(
                    self._fail_sync,
                    job.job_id,
                    JobStatus.RUNNING,
                    job_error(ErrorCode.DECISION_INVARIANT_VIOLATED),
                )
                return
        summary = {
            "decision": result.risk_assessment.decision.value,
            "degraded": result.degraded,
        }
        if outcome.interrupted:
            await asyncio.to_thread(
                self._store.transition,
                job.job_id,
                expected=JobStatus.RUNNING,
                target=JobStatus.WAITING_FOR_APPROVAL,
                events=[(JobEventType.JOB_WAITING_FOR_APPROVAL, summary)],
                result=result.model_dump(mode="json"),
            )
            logger.info("job %s waiting for approval", job.job_id)
            return
        await asyncio.to_thread(
            self._store.transition,
            job.job_id,
            expected=JobStatus.RUNNING,
            target=JobStatus.COMPLETED,
            events=[(JobEventType.JOB_COMPLETED, summary)],
            result=result.model_dump(mode="json"),
            finished_at=utc_now(),
        )
        logger.info("job %s completed", job.job_id)

    async def _execute_evaluation(self, job: JobRecord) -> None:
        suite = EvaluationSuite(job.request["suite"])
        requested = job.request.get("domain_pack_ids")
        pack_ids = (
            list(requested)
            if requested is not None
            else list_domain_pack_ids(domains_root=self._settings.domains_root)
        )
        result = await asyncio.to_thread(
            self._evaluation_runner,
            suite,
            pack_ids,
            self._settings.domains_root,
        )
        await asyncio.to_thread(
            self._store.transition,
            job.job_id,
            expected=JobStatus.RUNNING,
            target=JobStatus.COMPLETED,
            events=[(JobEventType.JOB_COMPLETED, {"fixtures": result.summary.fixtures})],
            result=result.model_dump(mode="json"),
            finished_at=utc_now(),
        )

    def _fail_sync(self, job_id: str, expected: JobStatus, error: JobError) -> bool:
        updated = self._store.transition(
            job_id,
            expected=expected,
            target=JobStatus.FAILED,
            events=[(JobEventType.JOB_FAILED, {"code": error.code.value})],
            error=error,
            finished_at=utc_now(),
        )
        return updated is not None


def _replay(existing: JobRecord, fingerprint: str) -> JobRecord:
    if existing.request_fingerprint != fingerprint:
        raise SentinelFlowError(
            "Idempotency-Key was already used with a different request",
            code=ErrorCode.IDEMPOTENCY_KEY_CONFLICT,
        )
    return existing


def _pack_not_found(pack_id: str) -> SentinelFlowError:
    return SentinelFlowError(
        "Domain pack not found",
        code=ErrorCode.DOMAIN_PACK_NOT_FOUND,
        details={"domain_pack_id": pack_id},
    )


def _invalid_transition(job: JobRecord) -> SentinelFlowError:
    return SentinelFlowError(
        "The job is not waiting for approval",
        code=ErrorCode.INVALID_JOB_TRANSITION,
        details={"job_id": job.job_id, "status": job.status.value},
    )


def _check_approvable(job: JobRecord) -> None:
    """Approval is legal exactly once, and only while the job waits for it."""
    if job.approval is not None:
        raise SentinelFlowError(
            "An approval was already submitted for this job",
            code=ErrorCode.APPROVAL_ALREADY_SUBMITTED,
            details={
                "job_id": job.job_id,
                "status": job.status.value,
                "approved": job.approval.approved,
            },
        )
    if not job.request.get("record_decision"):
        raise SentinelFlowError(
            "This analysis did not request a recorded gate decision",
            code=ErrorCode.APPROVAL_NOT_REQUIRED,
            details={"job_id": job.job_id, "status": job.status.value},
        )
    if job.status is not JobStatus.WAITING_FOR_APPROVAL:
        raise _invalid_transition(job)

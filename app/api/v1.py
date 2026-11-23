"""``/v1`` routes: analyses, approvals, decisions and evaluations.

Routes only translate HTTP to ``JobService`` calls. Creation returns ``202`` with a
``Location`` header. A replay of an idempotent create returns ``200`` with
``Idempotent-Replayed: true`` and the original job, whatever its status now.
"""

from __future__ import annotations

import re
from typing import Annotated

from fastapi import APIRouter, Depends, Header, Path, Request, Response, status

from app.api.errors import ErrorResponse
from app.api.schemas import (
    IDEMPOTENCY_KEY_PATTERN,
    ApprovalRequest,
    CreateAnalysisRequest,
    CreateEvaluationRequest,
    DecisionView,
    EvaluationResultView,
    JobView,
)
from app.api.security import require_api_access
from app.core.errors import ErrorCode, SentinelFlowError
from app.jobs.evaluation import EvaluationResult
from app.jobs.models import JobKind
from app.jobs.results import AnalysisResult
from app.jobs.service import JobService
from app.persistence.job_store import CreateOutcome

JOB_ID_PATTERN = r"^job-[0-9a-f]{32}$"
_KEY = re.compile(IDEMPOTENCY_KEY_PATTERN)

_ERRORS: dict[int | str, dict[str, object]] = {
    code: {"model": ErrorResponse} for code in (401, 403, 404, 409, 413, 422, 429, 503)
}

router = APIRouter(prefix="/v1", dependencies=[Depends(require_api_access)], responses=_ERRORS)

JobIdPath = Annotated[str, Path(pattern=JOB_ID_PATTERN)]
IdempotencyKey = Annotated[
    str | None,
    Header(
        alias="Idempotency-Key",
        description="1-128 chars of [A-Za-z0-9._:-]; same key + same body = same job",
    ),
]


def get_service(request: Request) -> JobService:
    service: JobService = request.app.state.job_service
    return service


Service = Annotated[JobService, Depends(get_service)]


def _validated_key(raw: str | None) -> str | None:
    if raw is None:
        return None
    if not _KEY.match(raw):
        raise SentinelFlowError(
            "Idempotency-Key must be 1-128 characters of [A-Za-z0-9._:-]",
            code=ErrorCode.INVALID_REQUEST,
            details={"field": "Idempotency-Key"},
        )
    return raw


def _created(outcome: CreateOutcome, response: Response) -> JobView:
    view = JobView.from_record(outcome.record)
    response.headers["Location"] = view.links.self
    if outcome.created:
        response.status_code = status.HTTP_202_ACCEPTED
    else:
        response.status_code = status.HTTP_200_OK
        response.headers["Idempotent-Replayed"] = "true"
    return view


@router.post("/analyses", response_model=JobView, status_code=status.HTTP_202_ACCEPTED)
async def create_analysis(
    body: CreateAnalysisRequest,
    response: Response,
    service: Service,
    idempotency_key: IdempotencyKey = None,
) -> JobView:
    outcome = await service.create_analysis(
        body.to_graph_request(),
        idempotency_key=_validated_key(idempotency_key),
    )
    return _created(outcome, response)


@router.get("/analyses/{job_id}", response_model=JobView)
async def get_analysis(job_id: JobIdPath, service: Service) -> JobView:
    return JobView.from_record(await service.get_job(job_id, kind=JobKind.ANALYSIS))


@router.get("/analyses/{job_id}/decision", response_model=DecisionView)
async def get_decision(job_id: JobIdPath, service: Service) -> DecisionView:
    job = await service.get_job(job_id, kind=JobKind.ANALYSIS)
    if job.result is None:
        raise SentinelFlowError(
            "No decision is available for this job yet",
            code=ErrorCode.RESULT_NOT_AVAILABLE,
            details={"job_id": job.job_id, "status": job.status.value},
        )
    return DecisionView(
        job_id=job.job_id,
        status=job.status,
        result=AnalysisResult.model_validate(job.result),
    )


@router.post(
    "/analyses/{job_id}/approval",
    response_model=JobView,
    status_code=status.HTTP_202_ACCEPTED,
)
async def submit_approval(job_id: JobIdPath, body: ApprovalRequest, service: Service) -> JobView:
    job = await service.submit_approval(
        job_id,
        approved=body.approved,
        reviewer=body.reviewer,
        comment=body.comment,
    )
    return JobView.from_record(job)


@router.post("/evaluations", response_model=JobView, status_code=status.HTTP_202_ACCEPTED)
async def create_evaluation(
    body: CreateEvaluationRequest,
    response: Response,
    service: Service,
    idempotency_key: IdempotencyKey = None,
) -> JobView:
    outcome = await service.create_evaluation(
        body.suite,
        body.domain_pack_ids,
        idempotency_key=_validated_key(idempotency_key),
    )
    return _created(outcome, response)


@router.get("/evaluations/{job_id}", response_model=JobView)
async def get_evaluation(job_id: JobIdPath, service: Service) -> JobView:
    return JobView.from_record(await service.get_job(job_id, kind=JobKind.EVALUATION))


@router.get("/evaluations/{job_id}/result", response_model=EvaluationResultView)
async def get_evaluation_result(job_id: JobIdPath, service: Service) -> EvaluationResultView:
    job = await service.get_job(job_id, kind=JobKind.EVALUATION)
    if job.result is None:
        raise SentinelFlowError(
            "No result is available for this job yet",
            code=ErrorCode.RESULT_NOT_AVAILABLE,
            details={"job_id": job.job_id, "status": job.status.value},
        )
    return EvaluationResultView(
        job_id=job.job_id,
        status=job.status,
        result=EvaluationResult.model_validate(job.result),
    )

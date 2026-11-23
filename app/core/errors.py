"""Structured application errors."""

from enum import StrEnum


class ErrorCode(StrEnum):
    """Machine-readable error codes returned by the API."""

    INPUT_ERROR = "INPUT_ERROR"
    DOMAIN_PACK_NOT_FOUND = "DOMAIN_PACK_NOT_FOUND"
    DOMAIN_PACK_INVALID = "DOMAIN_PACK_INVALID"
    INTERNAL_ERROR = "INTERNAL_ERROR"

    # M5 HTTP boundary: request shape and access.
    INVALID_REQUEST = "INVALID_REQUEST"
    REQUEST_TOO_LARGE = "REQUEST_TOO_LARGE"
    UNAUTHORIZED = "UNAUTHORIZED"
    NON_LOCAL_CLIENT = "NON_LOCAL_CLIENT"
    NOT_FOUND = "NOT_FOUND"
    METHOD_NOT_ALLOWED = "METHOD_NOT_ALLOWED"

    # M5 request semantics, checked before a job is created.
    CHANGE_NOT_FOUND = "CHANGE_NOT_FOUND"
    CHANGE_SOURCE_NOT_CONFIGURED = "CHANGE_SOURCE_NOT_CONFIGURED"
    PROVIDER_NOT_CONFIGURED = "PROVIDER_NOT_CONFIGURED"
    IDEMPOTENCY_KEY_CONFLICT = "IDEMPOTENCY_KEY_CONFLICT"
    TOO_MANY_ACTIVE_JOBS = "TOO_MANY_ACTIVE_JOBS"

    # M5 job lifecycle.
    JOB_NOT_FOUND = "JOB_NOT_FOUND"
    INVALID_JOB_TRANSITION = "INVALID_JOB_TRANSITION"
    APPROVAL_NOT_REQUIRED = "APPROVAL_NOT_REQUIRED"
    APPROVAL_ALREADY_SUBMITTED = "APPROVAL_ALREADY_SUBMITTED"
    RESULT_NOT_AVAILABLE = "RESULT_NOT_AVAILABLE"
    TOO_MANY_SUBSCRIBERS = "TOO_MANY_SUBSCRIBERS"

    # M5 job failure codes (stored on a failed job, never raised to the client as 5xx).
    PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"
    PROVIDER_RATE_LIMITED = "PROVIDER_RATE_LIMITED"
    PROVIDER_AUTH_FAILED = "PROVIDER_AUTH_FAILED"
    MCP_ERROR = "MCP_ERROR"
    CHANGE_SOURCE_UNAVAILABLE = "CHANGE_SOURCE_UNAVAILABLE"
    JOB_EXECUTION_FAILED = "JOB_EXECUTION_FAILED"
    JOB_INTERRUPTED = "JOB_INTERRUPTED"
    DECISION_INVARIANT_VIOLATED = "DECISION_INVARIANT_VIOLATED"
    GRAPH_INCOMPLETE = "GRAPH_INCOMPLETE"
    GATE_DECISION_NOT_RECORDED = "GATE_DECISION_NOT_RECORDED"


class SentinelFlowError(Exception):
    """Base exception for SentinelFlow domain errors."""

    def __init__(
        self,
        message: str,
        *,
        code: ErrorCode = ErrorCode.INTERNAL_ERROR,
        details: dict[str, object] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.details = details or {}

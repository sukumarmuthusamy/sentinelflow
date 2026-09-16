"""Structured application errors."""

from enum import StrEnum


class ErrorCode(StrEnum):
    """Machine-readable error codes returned by the API."""

    INPUT_ERROR = "INPUT_ERROR"
    DOMAIN_PACK_NOT_FOUND = "DOMAIN_PACK_NOT_FOUND"
    DOMAIN_PACK_INVALID = "DOMAIN_PACK_INVALID"
    INTERNAL_ERROR = "INTERNAL_ERROR"


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

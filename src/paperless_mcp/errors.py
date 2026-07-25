"""Typed application errors with safe, actionable messages."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any


class PaperlessMCPError(Exception):
    """Base class for errors safe to translate at CLI and MCP boundaries."""

    code = "paperless_mcp_error"

    def __init__(self, message: str, *, details: Mapping[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = dict(details or {})


class ConfigurationError(PaperlessMCPError):
    code = "configuration_error"


class AuthenticationError(PaperlessMCPError):
    code = "authentication_failed"


class PaperlessConnectionError(PaperlessMCPError):
    code = "paperless_unreachable"


class TLSVerificationError(PaperlessConnectionError):
    code = "tls_verification_failed"


class RateLimitError(PaperlessMCPError):
    code = "rate_limited"

    def __init__(self, message: str, *, retry_after_seconds: float | None = None) -> None:
        details = (
            {"retry_after_seconds": retry_after_seconds}
            if retry_after_seconds is not None
            else None
        )
        super().__init__(message, details=details)
        self.retry_after_seconds = retry_after_seconds


class PaperlessResponseError(PaperlessMCPError):
    code = "invalid_paperless_response"


class UnsupportedPaperlessBehaviorError(PaperlessMCPError):
    code = "unsupported_paperless_behavior"


class NotFoundError(PaperlessMCPError):
    code = "not_found"


class InvalidTaxonomyError(PaperlessMCPError):
    code = "invalid_taxonomy_id"


class WritesDisabledError(PaperlessMCPError):
    code = "writes_disabled"


class DeletesDisabledError(PaperlessMCPError):
    code = "deletes_disabled"


class TaxonomyCreationDisabledError(PaperlessMCPError):
    code = "taxonomy_creation_disabled"


class BatchTooLargeError(PaperlessMCPError):
    code = "batch_too_large"

    def __init__(self, requested: int, maximum: int) -> None:
        super().__init__(
            f"Batch contains {requested} changes; the server limit is {maximum}.",
            details={"requested": requested, "maximum": maximum},
        )


class MalformedProposalError(PaperlessMCPError):
    code = "malformed_proposal"


class StaleProposalError(PaperlessMCPError):
    code = "stale_proposal"

    def __init__(self, document_id: int, conflicting_fields: Sequence[str]) -> None:
        fields = tuple(conflicting_fields)
        super().__init__(
            f"Document {document_id} changed after the proposal was created.",
            details={"document_id": document_id, "conflicting_fields": fields},
        )
        self.document_id = document_id
        self.conflicting_fields = fields


class PartialBatchError(PaperlessMCPError):
    code = "partial_batch_failure"


class RollbackConflictError(PaperlessMCPError):
    code = "rollback_conflict"


class AuditDirectoryError(PaperlessMCPError):
    code = "audit_directory_failure"

"""Small, stderr-only structured logging with fail-closed redaction."""

from __future__ import annotations

import json
import logging
import sys
from typing import Any

_LOGGER_NAME = "paperless_mcp"
_SAFE_FIELDS = (
    "operation",
    "run_id",
    "document_id",
    "duration_ms",
    "status_code",
    "retry_count",
    "dry_run",
    "mutation_count",
    "applied_count",
    "conflict_count",
    "failure_count",
)
_secrets: tuple[str, ...] = ()


def configure_logging(level: str, *, secrets: tuple[str, ...] = ()) -> None:
    """Configure the project logger without ever touching protocol stdout."""
    global _secrets
    _secrets = tuple(secret for secret in secrets if secret)
    logger = logging.getLogger(_LOGGER_NAME)
    logger.handlers.clear()
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(_JsonFormatter())
    logger.addHandler(handler)
    logger.setLevel(level)
    logger.propagate = False


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"{_LOGGER_NAME}.{name}")


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "level": record.levelname,
            "event": _redact(record.getMessage()),
        }
        for field in _SAFE_FIELDS:
            value = getattr(record, field, None)
            if value is not None:
                payload[field] = value
        return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _redact(value: str) -> str:
    rendered = value
    for secret in _secrets:
        rendered = rendered.replace(secret, "[REDACTED]")
    return rendered

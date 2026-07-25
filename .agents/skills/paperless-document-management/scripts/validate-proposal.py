#!/usr/bin/env python3
"""Validate Paperless MCP proposal JSON locally without network access or mutation."""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections.abc import Mapping, Sequence
from datetime import date, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

EXIT_VALID = 0
EXIT_INVALID = 1
EXIT_INPUT_ERROR = 2
EXIT_INTERNAL_ERROR = 3
MAX_BYTES = 5 * 1024 * 1024


class ProposalValidationError(ValueError):
    """One or more proposal values violate the standalone schema."""


def _object(value: Any, path: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise ProposalValidationError(f"{path} must be an object")
    return value


def _array(value: Any, path: str) -> Sequence[Any]:
    if not isinstance(value, list):
        raise ProposalValidationError(f"{path} must be an array")
    return value


def _keys(
    value: Mapping[str, Any],
    path: str,
    *,
    required: set[str],
    allowed: set[str],
) -> None:
    missing = sorted(required - value.keys())
    extra = sorted(value.keys() - allowed)
    if missing:
        raise ProposalValidationError(f"{path} is missing required field(s): {', '.join(missing)}")
    if extra:
        raise ProposalValidationError(f"{path} has unknown field(s): {', '.join(extra)}")


def _text(value: Any, path: str, *, maximum: int | None = None) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ProposalValidationError(f"{path} must be a non-empty string")
    if maximum is not None and len(value.strip()) > maximum:
        raise ProposalValidationError(f"{path} exceeds {maximum} characters")


def _positive_id(value: Any, path: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ProposalValidationError(f"{path} must be a positive integer")


def _ids(value: Any, path: str) -> list[int]:
    values = _array(value, path)
    result: list[int] = []
    for index, item in enumerate(values):
        _positive_id(item, f"{path}[{index}]")
        result.append(item)
    if len(result) != len(set(result)):
        raise ProposalValidationError(f"{path} must not contain duplicate IDs")
    return result


def _iso_date(value: Any, path: str) -> None:
    if value is None:
        return
    if not isinstance(value, str):
        raise ProposalValidationError(f"{path} must be an ISO date or null")
    try:
        date.fromisoformat(value)
    except ValueError as exc:
        raise ProposalValidationError(f"{path} must be an ISO date") from exc


def _aware_datetime(value: Any, path: str) -> None:
    if value is None:
        return
    if not isinstance(value, str):
        raise ProposalValidationError(f"{path} must be an ISO timestamp or null")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ProposalValidationError(f"{path} must be an ISO timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ProposalValidationError(f"{path} must include a timezone")


def _nullable_id(value: Any, path: str) -> None:
    if value is not None:
        _positive_id(value, path)


def _custom_fields(value: Any, path: str) -> None:
    fields = _object(value, path)
    for field_id in fields:
        if not isinstance(field_id, str) or not field_id.isdecimal() or int(field_id) <= 0:
            raise ProposalValidationError(f"{path} keys must be positive integer IDs")


EXPECTED_FIELDS = {
    "title",
    "created",
    "correspondent_id",
    "document_type_id",
    "storage_path_id",
    "tag_ids",
    "custom_fields",
    "archive_serial_number",
    "modified",
}
CHANGE_FIELDS = {
    "title",
    "created",
    "correspondent_id",
    "document_type_id",
    "storage_path_id",
    "add_tag_ids",
    "remove_tag_ids",
    "replace_tag_ids",
    "custom_fields",
    "replace_custom_fields",
}


def _validate_expected(value: Any, path: str) -> None:
    expected = _object(value, path)
    _keys(expected, path, required={"title", "tag_ids"}, allowed=EXPECTED_FIELDS)
    if not isinstance(expected["title"], str):
        raise ProposalValidationError(f"{path}.title must be a string")
    _ids(expected["tag_ids"], f"{path}.tag_ids")
    if "created" in expected:
        _iso_date(expected["created"], f"{path}.created")
    for field in ("correspondent_id", "document_type_id", "storage_path_id"):
        if field in expected:
            _nullable_id(expected[field], f"{path}.{field}")
    if "custom_fields" in expected:
        _custom_fields(expected["custom_fields"], f"{path}.custom_fields")
    if "archive_serial_number" in expected:
        number = expected["archive_serial_number"]
        if number is not None and (
            isinstance(number, bool) or not isinstance(number, int) or number < 0
        ):
            raise ProposalValidationError(
                f"{path}.archive_serial_number must be non-negative or null"
            )
    if "modified" in expected:
        _aware_datetime(expected["modified"], f"{path}.modified")


def _validate_changes(value: Any, path: str) -> None:
    changes = _object(value, path)
    _keys(changes, path, required=set(), allowed=CHANGE_FIELDS)
    if not changes:
        raise ProposalValidationError(f"{path} must contain at least one change")
    if "title" in changes:
        _text(changes["title"], f"{path}.title", maximum=255)
    if "created" in changes:
        _iso_date(changes["created"], f"{path}.created")
    for field in ("correspondent_id", "document_type_id", "storage_path_id"):
        if field in changes:
            _nullable_id(changes[field], f"{path}.{field}")
    add_ids = _ids(changes.get("add_tag_ids", []), f"{path}.add_tag_ids")
    remove_ids = _ids(changes.get("remove_tag_ids", []), f"{path}.remove_tag_ids")
    if set(add_ids) & set(remove_ids):
        raise ProposalValidationError(f"{path} cannot add and remove the same tag")
    if "replace_tag_ids" in changes:
        if changes["replace_tag_ids"] is not None:
            _ids(changes["replace_tag_ids"], f"{path}.replace_tag_ids")
        if add_ids or remove_ids:
            raise ProposalValidationError(
                f"{path}.replace_tag_ids cannot be combined with add/remove"
            )
    if "custom_fields" in changes and changes["custom_fields"] is not None:
        _custom_fields(changes["custom_fields"], f"{path}.custom_fields")
    if "replace_custom_fields" in changes and changes["replace_custom_fields"] is not None:
        _custom_fields(changes["replace_custom_fields"], f"{path}.replace_custom_fields")
    if "custom_fields" in changes and "replace_custom_fields" in changes:
        raise ProposalValidationError(
            f"{path} cannot combine custom_fields and replace_custom_fields"
        )
    meaningful = any(
        field in changes
        and (
            field
            in {
                "created",
                "correspondent_id",
                "document_type_id",
                "storage_path_id",
                "replace_tag_ids",
                "custom_fields",
                "replace_custom_fields",
            }
            or bool(changes[field])
        )
        for field in CHANGE_FIELDS
    )
    if not meaningful:
        raise ProposalValidationError(f"{path} must contain at least one meaningful change")


def validate_standalone(value: Any) -> None:
    """Conservatively validate the public proposal schema using only the standard library."""
    proposal = _object(value, "$")
    _keys(
        proposal,
        "$",
        required={"description", "changes"},
        allowed={"proposal_id", "description", "changes", "created_at"},
    )
    _text(proposal["description"], "$.description", maximum=2_000)
    if "proposal_id" in proposal:
        try:
            UUID(str(proposal["proposal_id"]))
        except (ValueError, TypeError, AttributeError) as exc:
            raise ProposalValidationError("$.proposal_id must be a UUID") from exc
    if "created_at" in proposal:
        _aware_datetime(proposal["created_at"], "$.created_at")
    changes = _array(proposal["changes"], "$.changes")
    if not changes:
        raise ProposalValidationError("$.changes must contain at least one proposal")
    document_ids: list[int] = []
    allowed = {
        "document_id",
        "expected_current_state",
        "changes",
        "confidence",
        "reason",
        "allow_protected_tag_removal",
    }
    for index, item in enumerate(changes):
        path = f"$.changes[{index}]"
        change = _object(item, path)
        _keys(
            change,
            path,
            required={
                "document_id",
                "expected_current_state",
                "changes",
                "confidence",
                "reason",
            },
            allowed=allowed,
        )
        _positive_id(change["document_id"], f"{path}.document_id")
        document_ids.append(change["document_id"])
        _validate_expected(change["expected_current_state"], f"{path}.expected_current_state")
        _validate_changes(change["changes"], f"{path}.changes")
        confidence = change["confidence"]
        if (
            isinstance(confidence, bool)
            or not isinstance(confidence, (int, float))
            or not math.isfinite(float(confidence))
            or not 0.0 <= float(confidence) <= 1.0
        ):
            raise ProposalValidationError(
                f"{path}.confidence must be a finite number from 0 through 1"
            )
        _text(change["reason"], f"{path}.reason", maximum=2_000)
        approvals = change.get("allow_protected_tag_removal", [])
        approval_values = _array(approvals, f"{path}.allow_protected_tag_removal")
        for approval_index, approval in enumerate(approval_values):
            _text(approval, f"{path}.allow_protected_tag_removal[{approval_index}]")
    if len(document_ids) != len(set(document_ids)):
        raise ProposalValidationError("$.changes may contain only one change per document")


def validate_with_project(value: Any) -> bool:
    """Use the installed project's canonical Pydantic model when available."""
    try:
        from paperless_mcp.models import BatchProposal
    except ImportError:
        return False
    BatchProposal.model_validate(value)
    return True


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Validate proposal JSON locally. This command never contacts Paperless."
    )
    parser.add_argument("proposal", type=Path, help="Path to a proposal JSON file")
    parser.add_argument(
        "--standalone",
        action="store_true",
        help="Use the bundled standard-library validator even if paperless-mcp is installed.",
    )
    return parser


def main() -> int:
    args = _parser().parse_args()
    path: Path = args.proposal
    try:
        if not path.is_file():
            print("input error: proposal path is not a regular file", file=sys.stderr)
            return EXIT_INPUT_ERROR
        if path.stat().st_size > MAX_BYTES:
            print("input error: proposal file exceeds 5 MiB", file=sys.stderr)
            return EXIT_INPUT_ERROR
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        print(f"input error: {exc}", file=sys.stderr)
        return EXIT_INPUT_ERROR

    try:
        used_project_model = False if args.standalone else validate_with_project(value)
        if not used_project_model:
            validate_standalone(value)
    except (ProposalValidationError, ValueError) as exc:
        # Pydantic's ValidationError is a ValueError. It contains schema locations but no secrets.
        print(f"invalid proposal: {exc}", file=sys.stderr)
        return EXIT_INVALID
    except Exception:
        print("validator internal error", file=sys.stderr)
        return EXIT_INTERNAL_ERROR

    validator = "paperless-mcp model" if used_project_model else "standalone schema"
    print(f"valid proposal ({validator}); no network request or mutation performed")
    return EXIT_VALID


if __name__ == "__main__":
    raise SystemExit(main())

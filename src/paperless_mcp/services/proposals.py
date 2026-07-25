"""Proposal loading, operator policy, and server-side semantic validation."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml  # type: ignore[import-untyped]
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from paperless_mcp.client import PaperlessClient, TaxonomyEndpoint
from paperless_mcp.config import Settings
from paperless_mcp.errors import MalformedProposalError
from paperless_mcp.models import (
    BatchProposal,
    IssueSeverity,
    ProposalValidationResult,
    ProposedDocumentChange,
    TaxonomyKind,
    ValidationIssue,
)

MAX_PROPOSAL_BYTES = 5 * 1024 * 1024


class _PolicyModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class TitlePolicy(_PolicyModel):
    separator: str = " \N{EN DASH} "
    expose_account_numbers: bool = False


class ClassificationPolicy(_PolicyModel):
    allow_new_tags: bool = False
    preserve_existing_tags_by_default: bool = True
    auto_apply_confidence: float = Field(default=0.95, ge=0.0, le=1.0)


class TagRules(_PolicyModel):
    prefer: tuple[str, ...] = ()
    discouraged: tuple[str, ...] = ()


class TaxonomyPolicy(_PolicyModel):
    """Operator policy can restrict behavior but cannot enable a capability."""

    protected_tags: tuple[str, ...] = ()
    review_tag: str | None = None
    title: TitlePolicy = Field(default_factory=TitlePolicy)
    classification: ClassificationPolicy = Field(default_factory=ClassificationPolicy)
    tag_rules: TagRules = Field(default_factory=TagRules)
    correspondent_aliases: dict[str, str] = Field(default_factory=dict)

    def safe_summary(self, settings: Settings) -> dict[str, Any]:
        return {
            **self.model_dump(mode="json"),
            "effective_protected_tags": list(effective_protected_tags(settings, self)),
            "capabilities": {
                "writes": settings.write_enabled,
                "deletes": settings.delete_enabled,
                "taxonomy_creation": (
                    settings.allow_taxonomy_creation and self.classification.allow_new_tags
                ),
            },
        }


def load_taxonomy_policy(settings: Settings) -> TaxonomyPolicy:
    path = settings.taxonomy_policy_file
    if path is None:
        return TaxonomyPolicy(
            protected_tags=settings.protected_tags,
            review_tag=settings.default_review_tag,
        )
    try:
        if not path.is_file():
            raise MalformedProposalError("The taxonomy policy path is not a regular file.")
        if path.stat().st_size > MAX_PROPOSAL_BYTES:
            raise MalformedProposalError("The taxonomy policy file is too large.")
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        return TaxonomyPolicy.model_validate({} if raw is None else raw)
    except MalformedProposalError:
        raise
    except (OSError, UnicodeError, yaml.YAMLError, ValidationError) as exc:
        raise MalformedProposalError("The taxonomy policy file is invalid.") from exc


def effective_protected_tags(
    settings: Settings,
    policy: TaxonomyPolicy,
) -> tuple[str, ...]:
    """Return the case-insensitive union; policy can only add protection."""
    result: list[str] = []
    seen: set[str] = set()
    for name in (*settings.protected_tags, *policy.protected_tags):
        normalized = name.casefold()
        if normalized not in seen:
            seen.add(normalized)
            result.append(name)
    return tuple(result)


def load_proposal_file(path: Path) -> BatchProposal:
    try:
        if not path.is_file():
            raise MalformedProposalError("The proposal path is not a regular file.")
        if path.stat().st_size > MAX_PROPOSAL_BYTES:
            raise MalformedProposalError("The proposal file is too large.")
        value = json.loads(path.read_text(encoding="utf-8"))
        return BatchProposal.model_validate(value)
    except MalformedProposalError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError, ValidationError) as exc:
        raise MalformedProposalError("The proposal JSON is invalid.") from exc


class ProposalService:
    """Validate proposals independently from CLI and MCP input parsing."""

    def __init__(
        self,
        client: PaperlessClient,
        settings: Settings,
        policy: TaxonomyPolicy,
    ) -> None:
        self._client = client
        self._settings = settings
        self.policy = policy

    async def validate(self, proposal: BatchProposal) -> ProposalValidationResult:
        issues: list[ValidationIssue] = []
        if len(proposal.changes) > self._settings.max_batch_size:
            issues.append(
                ValidationIssue(
                    severity=IssueSeverity.ERROR,
                    code="batch_too_large",
                    message=(
                        f"Batch contains {len(proposal.changes)} changes; "
                        f"the server limit is {self._settings.max_batch_size}."
                    ),
                )
            )

        references = _taxonomy_references(proposal)
        existence: dict[tuple[TaxonomyEndpoint, int], bool] = {}
        for endpoint, item_id in sorted(references):
            existence[(endpoint, item_id)] = await self._client.taxonomy_item_exists(
                endpoint,
                item_id,
            )

        for change in proposal.changes:
            issues.extend(_expected_state_issues(change))
            for endpoint, item_id, field in _change_taxonomy_references(change):
                if not existence[(endpoint, item_id)]:
                    issues.append(
                        ValidationIssue(
                            severity=IssueSeverity.ERROR,
                            code="invalid_taxonomy_id",
                            message=f"Referenced {endpoint} ID {item_id} does not exist.",
                            document_id=change.document_id,
                            field=field,
                        )
                    )

        valid = not any(issue.severity is IssueSeverity.ERROR for issue in issues)
        return ProposalValidationResult(
            valid=valid,
            proposal_id=proposal.proposal_id,
            requested_count=len(proposal.changes),
            issues=tuple(issues),
            summary=(
                f"Proposal is valid for {len(proposal.changes)} document(s)."
                if valid
                else f"Proposal was rejected with {len(issues)} validation issue(s)."
            ),
        )

    async def tag_names(self, tag_ids: set[int]) -> dict[int, str]:
        names: dict[int, str] = {}
        for tag_id in sorted(tag_ids):
            item = await self._client.get_taxonomy_item("tags", tag_id)
            names[tag_id] = item.name
        return names

    def active_policy(self) -> dict[str, Any]:
        return self.policy.safe_summary(self._settings)


def _expected_state_issues(change: ProposedDocumentChange) -> list[ValidationIssue]:
    """Require snapshots for fields whose mutation could overwrite human changes."""
    expected_fields = change.expected_current_state.model_fields_set
    required = {"title", "tag_ids"}
    relevant = {
        "custom_fields" if field == "replace_custom_fields" else field
        for field in (
            "created",
            "correspondent_id",
            "document_type_id",
            "storage_path_id",
            "custom_fields",
            "replace_custom_fields",
            "archive_serial_number",
            "modified",
        )
        if field in change.changes.model_fields_set
    }
    missing = sorted((required | relevant) - expected_fields)
    return [
        ValidationIssue(
            severity=IssueSeverity.ERROR,
            code="incomplete_expected_state",
            message=f"Expected current state must explicitly include {field}.",
            document_id=change.document_id,
            field=f"expected_current_state.{field}",
        )
        for field in missing
    ]


def _taxonomy_references(proposal: BatchProposal) -> set[tuple[TaxonomyEndpoint, int]]:
    return {
        (endpoint, item_id)
        for change in proposal.changes
        for endpoint, item_id, _ in _change_taxonomy_references(change)
    }


def _change_taxonomy_references(
    change: ProposedDocumentChange,
) -> tuple[tuple[TaxonomyEndpoint, int, str], ...]:
    changes = change.changes
    references: list[tuple[TaxonomyEndpoint, int, str]] = []
    for tag_id in (*changes.add_tag_ids, *changes.remove_tag_ids, *(changes.replace_tag_ids or ())):
        references.append(("tags", tag_id, "changes.tag_ids"))
    if changes.correspondent_id is not None:
        references.append(("correspondents", changes.correspondent_id, "changes.correspondent_id"))
    if changes.document_type_id is not None:
        references.append(("document_types", changes.document_type_id, "changes.document_type_id"))
    if changes.storage_path_id is not None:
        references.append(("storage_paths", changes.storage_path_id, "changes.storage_path_id"))
    changed_custom_fields = (
        changes.replace_custom_fields
        if changes.replace_custom_fields is not None
        else changes.custom_fields
    )
    if changed_custom_fields is not None:
        references.extend(
            ("custom_fields", field_id, "changes.custom_fields")
            for field_id in changed_custom_fields
        )
    return tuple(references)


def taxonomy_kind_for_endpoint(endpoint: TaxonomyEndpoint) -> TaxonomyKind:
    return {
        "tags": TaxonomyKind.TAG,
        "correspondents": TaxonomyKind.CORRESPONDENT,
        "document_types": TaxonomyKind.DOCUMENT_TYPE,
        "storage_paths": TaxonomyKind.STORAGE_PATH,
        "custom_fields": TaxonomyKind.CUSTOM_FIELD,
    }[endpoint]

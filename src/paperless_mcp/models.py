"""Shared validated domain models used by all transports and services."""

from __future__ import annotations

from datetime import UTC, date, datetime
from enum import StrEnum
from typing import Annotated, Self
from uuid import UUID, uuid4

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    RootModel,
    StrictFloat,
    StrictInt,
    field_validator,
    model_validator,
)

PositiveId = Annotated[StrictInt, Field(gt=0)]


def _sorted_unique_ids(values: list[int] | tuple[int, ...] | set[int]) -> tuple[int, ...]:
    if not isinstance(values, (list, tuple, set)):
        raise ValueError("IDs must be a list, tuple, or set")
    if any(not isinstance(value, int) or isinstance(value, bool) for value in values):
        raise ValueError("IDs must be integers")
    result = tuple(sorted(set(values)))
    if any(value <= 0 for value in result):
        raise ValueError("IDs must be positive integers")
    return result


def utc_now() -> datetime:
    return datetime.now(UTC)


class DomainModel(BaseModel):
    """Strict base model for stable interface schemas."""

    model_config = ConfigDict(
        extra="forbid",
        validate_assignment=True,
        str_strip_whitespace=True,
    )


class ConfidenceScore(RootModel[Annotated[StrictFloat, Field(ge=0.0, le=1.0)]]):
    """A normalized confidence score from zero through one."""

    @property
    def value(self) -> float:
        return self.root


class TaxonomyKind(StrEnum):
    TAG = "tag"
    CORRESPONDENT = "correspondent"
    DOCUMENT_TYPE = "document_type"
    STORAGE_PATH = "storage_path"
    CUSTOM_FIELD = "custom_field"


class TaxonomyItem(DomainModel):
    id: PositiveId
    kind: TaxonomyKind
    name: str = Field(min_length=1, max_length=255)
    slug: str | None = None
    document_count: int | None = Field(default=None, ge=0)
    matching_algorithm: int | None = None
    is_insensitive: bool | None = None


class CustomFieldDefinition(DomainModel):
    id: PositiveId
    name: str = Field(min_length=1, max_length=255)
    data_type: str | None = None
    extra_data: JsonValue = None
    document_count: int | None = Field(default=None, ge=0)


class DuplicateTaxonomyCandidate(DomainModel):
    """A non-mutating indication that taxonomy names may be duplicates."""

    normalized_name: str = Field(min_length=1)
    item_ids: tuple[PositiveId, ...] = Field(min_length=2)
    names: tuple[str, ...] = Field(min_length=2)

    _normalize_item_ids = field_validator("item_ids", mode="before")(_sorted_unique_ids)


class DocumentOrdering(StrEnum):
    ADDED_ASC = "added"
    ADDED_DESC = "-added"
    CREATED_ASC = "created"
    CREATED_DESC = "-created"
    MODIFIED_ASC = "modified"
    MODIFIED_DESC = "-modified"
    TITLE_ASC = "title"
    TITLE_DESC = "-title"


class MissingMetadataField(StrEnum):
    CORRESPONDENT = "correspondent"
    DOCUMENT_TYPE = "document_type"
    STORAGE_PATH = "storage_path"
    TAGS = "tags"


class DocumentFilters(DomainModel):
    """Allowlisted Paperless document filters.

    No raw query expression is accepted. Search text remains inert data sent only
    as a value for Paperless's documented simple-search parameters.
    """

    text: str | None = Field(default=None, min_length=1, max_length=1_000)
    title: str | None = Field(default=None, min_length=1, max_length=1_000)
    correspondent_id: PositiveId | None = None
    document_type_id: PositiveId | None = None
    storage_path_id: PositiveId | None = None
    tag_ids: tuple[PositiveId, ...] = ()
    tag_names: tuple[Annotated[str, Field(min_length=1, max_length=255)], ...] = ()
    created_after: date | None = None
    created_before: date | None = None
    added_after: date | None = None
    added_before: date | None = None
    archive_serial_number: int | None = Field(default=None, ge=0)
    original_filename: str | None = Field(default=None, min_length=1, max_length=255)
    untagged: bool | None = None
    missing: MissingMetadataField | None = None
    ordering: DocumentOrdering = DocumentOrdering.ADDED_DESC

    _normalize_tag_ids = field_validator("tag_ids", mode="before")(_sorted_unique_ids)

    @field_validator("tag_names", mode="before")
    @classmethod
    def normalize_tag_names(cls, value: object) -> object:
        if not isinstance(value, (list, tuple, set)):
            raise ValueError("tag names must be a list, tuple, or set")
        names = tuple(dict.fromkeys(str(item).strip() for item in value if str(item).strip()))
        return names

    @model_validator(mode="after")
    def validate_ranges(self) -> Self:
        if (
            self.created_after is not None
            and self.created_before is not None
            and self.created_after > self.created_before
        ):
            raise ValueError("created_after must not be later than created_before")
        if (
            self.added_after is not None
            and self.added_before is not None
            and self.added_after > self.added_before
        ):
            raise ValueError("added_after must not be later than added_before")
        if self.untagged is True and (self.tag_ids or self.tag_names):
            raise ValueError("untagged cannot be combined with tag filters")
        return self


class CurrentDocumentMetadata(DomainModel):
    """Canonical state used for stale-proposal comparisons."""

    title: str
    created: date | None = None
    correspondent_id: PositiveId | None = None
    document_type_id: PositiveId | None = None
    storage_path_id: PositiveId | None = None
    tag_ids: tuple[PositiveId, ...] = ()
    custom_fields: dict[int, JsonValue] = Field(default_factory=dict)
    archive_serial_number: int | None = Field(default=None, ge=0)
    modified: AwareDatetime | None = None

    _normalize_tag_ids = field_validator("tag_ids", mode="before")(_sorted_unique_ids)

    @field_validator("custom_fields")
    @classmethod
    def validate_custom_field_ids(cls, value: dict[int, JsonValue]) -> dict[int, JsonValue]:
        if any(field_id <= 0 for field_id in value):
            raise ValueError("custom field IDs must be positive integers")
        return dict(sorted(value.items()))


class DocumentSummary(DomainModel):
    id: PositiveId
    title: str
    created: date | None = None
    added: AwareDatetime | None = None
    modified: AwareDatetime | None = None
    correspondent_id: PositiveId | None = None
    document_type_id: PositiveId | None = None
    storage_path_id: PositiveId | None = None
    tag_ids: tuple[PositiveId, ...] = ()
    archive_serial_number: int | None = Field(default=None, ge=0)
    original_filename: str | None = None

    _normalize_tag_ids = field_validator("tag_ids", mode="before")(_sorted_unique_ids)


class DocumentDetail(DocumentSummary):
    """Detailed metadata; OCR content remains opt-in."""

    content: str | None = Field(default=None, repr=False)
    content_length: int | None = Field(default=None, ge=0)
    content_truncated: bool = False
    custom_fields: dict[int, JsonValue] = Field(default_factory=dict)
    notes: tuple[str, ...] = ()
    notes_total_count: int = Field(default=0, ge=0)
    notes_truncated: bool = False

    def current_metadata(self) -> CurrentDocumentMetadata:
        return CurrentDocumentMetadata(
            title=self.title,
            created=self.created,
            correspondent_id=self.correspondent_id,
            document_type_id=self.document_type_id,
            storage_path_id=self.storage_path_id,
            tag_ids=self.tag_ids,
            custom_fields=self.custom_fields,
            archive_serial_number=self.archive_serial_number,
            modified=self.modified,
        )


class ExpectedCurrentState(CurrentDocumentMetadata):
    """Expected state required by every proposed change."""


class ProposedDocumentChanges(DomainModel):
    title: str | None = Field(default=None, min_length=1, max_length=255)
    created: date | None = None
    correspondent_id: PositiveId | None = None
    document_type_id: PositiveId | None = None
    storage_path_id: PositiveId | None = None
    add_tag_ids: tuple[PositiveId, ...] = ()
    remove_tag_ids: tuple[PositiveId, ...] = ()
    replace_tag_ids: tuple[PositiveId, ...] | None = None
    custom_fields: dict[int, JsonValue] | None = None
    replace_custom_fields: dict[int, JsonValue] | None = None

    _normalize_add_tag_ids = field_validator("add_tag_ids", mode="before")(_sorted_unique_ids)
    _normalize_remove_tag_ids = field_validator("remove_tag_ids", mode="before")(_sorted_unique_ids)

    @field_validator("replace_tag_ids", mode="before")
    @classmethod
    def normalize_replace_tag_ids(cls, value: object) -> object:
        return None if value is None else _sorted_unique_ids(value)  # type: ignore[arg-type]

    @field_validator("custom_fields", "replace_custom_fields")
    @classmethod
    def validate_changed_custom_field_ids(
        cls, value: dict[int, JsonValue] | None
    ) -> dict[int, JsonValue] | None:
        if value is not None and any(field_id <= 0 for field_id in value):
            raise ValueError("custom field IDs must be positive integers")
        return value

    @model_validator(mode="after")
    def validate_tag_operations(self) -> Self:
        overlap = set(self.add_tag_ids).intersection(self.remove_tag_ids)
        if overlap:
            raise ValueError(f"tag IDs cannot be both added and removed: {sorted(overlap)}")
        if self.replace_tag_ids is not None and (self.add_tag_ids or self.remove_tag_ids):
            raise ValueError("replace_tag_ids cannot be combined with add/remove tag operations")
        if self.replace_custom_fields is not None and self.custom_fields is not None:
            raise ValueError("replace_custom_fields cannot be combined with custom_fields")

        mutable_fields = {
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
        explicitly_set = self.model_fields_set.intersection(mutable_fields)
        meaningful = any(
            field in explicitly_set
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
                or bool(getattr(self, field))
            )
            for field in mutable_fields
        )
        if not meaningful:
            raise ValueError("at least one document change must be provided")
        return self


class ProposedDocumentChange(DomainModel):
    document_id: PositiveId
    expected_current_state: ExpectedCurrentState
    changes: ProposedDocumentChanges
    confidence: ConfidenceScore
    reason: str = Field(min_length=1, max_length=2_000)
    allow_protected_tag_removal: tuple[str, ...] = ()


class BatchProposal(DomainModel):
    proposal_id: UUID = Field(default_factory=uuid4)
    description: str = Field(min_length=1, max_length=2_000)
    changes: tuple[ProposedDocumentChange, ...] = Field(min_length=1)
    created_at: AwareDatetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def require_unique_document_ids(self) -> Self:
        document_ids = [change.document_id for change in self.changes]
        if len(document_ids) != len(set(document_ids)):
            raise ValueError("a batch may contain only one change per document")
        return self


class IssueSeverity(StrEnum):
    ERROR = "error"
    WARNING = "warning"
    INFO = "info"


class ValidationIssue(DomainModel):
    severity: IssueSeverity
    code: str = Field(min_length=1)
    message: str = Field(min_length=1)
    document_id: PositiveId | None = None
    field: str | None = None


class ProposalValidationResult(DomainModel):
    """Complete, transport-safe validation result for one batch proposal."""

    valid: bool
    proposal_id: UUID
    requested_count: int = Field(ge=1)
    issues: tuple[ValidationIssue, ...] = ()
    summary: str

    @model_validator(mode="after")
    def validate_issue_status(self) -> Self:
        has_error = any(issue.severity is IssueSeverity.ERROR for issue in self.issues)
        if self.valid == has_error:
            raise ValueError("valid must be false exactly when validation has errors")
        return self


class MutationStatus(StrEnum):
    DRY_RUN = "dry_run"
    NO_OP = "no_op"
    APPLIED = "applied"
    PARTIAL = "partial"
    INDETERMINATE = "indeterminate"
    REJECTED = "rejected"


class InitiatingInterface(StrEnum):
    MCP = "mcp"
    CLI = "cli"
    ROLLBACK = "rollback"


class DocumentMutation(DomainModel):
    document_id: PositiveId
    before: CurrentDocumentMetadata
    after: CurrentDocumentMetadata | None = None
    changed_fields: tuple[str, ...] = ()
    conflicting_fields: tuple[str, ...] = ()
    error_code: str | None = None
    error_message: str | None = None


class DryRunRollbackOperation(DomainModel):
    """A non-persistent rollback plan for one proposed dry-run mutation."""

    document_id: PositiveId
    expected_current_state: CurrentDocumentMetadata
    restore_state: CurrentDocumentMetadata


class DryRunAuditPreview(DomainModel):
    """Complete audit-shaped preview returned without writing local state."""

    operation: str = Field(min_length=1)
    interface: InitiatingInterface
    proposal_id: UUID
    force: bool = False
    document_ids: tuple[PositiveId, ...]
    rollback_operations: tuple[DryRunRollbackOperation, ...] = ()


class MutationResult(DomainModel):
    status: MutationStatus
    dry_run: bool
    requested_count: int = Field(ge=0)
    applied_count: int = Field(default=0, ge=0)
    noop_count: int = Field(default=0, ge=0)
    conflict_count: int = Field(default=0, ge=0)
    failure_count: int = Field(default=0, ge=0)
    mutations: tuple[DocumentMutation, ...] = ()
    summary: str
    run_id: str | None = None
    rollback_path: str | None = None
    audit_preview: DryRunAuditPreview | None = None

    @model_validator(mode="after")
    def validate_counts(self) -> Self:
        if (
            self.applied_count + self.noop_count + self.conflict_count + self.failure_count
            > self.requested_count
        ):
            raise ValueError("result counts cannot exceed the requested mutation count")
        if self.dry_run and self.applied_count:
            raise ValueError("a dry-run cannot report applied mutations")
        return self


class AuditRecord(DomainModel):
    run_id: str = Field(min_length=1)
    timestamp: AwareDatetime = Field(default_factory=utc_now)
    operation: str = Field(min_length=1)
    interface: InitiatingInterface
    app_version: str
    paperless_url: str
    paperless_version: str | None = None
    proposal_id: UUID | None = None
    document_ids: tuple[PositiveId, ...]
    dry_run: bool
    force: bool = False
    result: MutationResult
    metadata_hashes: dict[int, str] = Field(default_factory=dict)


class RollbackOperationVerification(StrEnum):
    VERIFIED = "verified"
    INDETERMINATE = "indeterminate"


class RollbackOperation(DomainModel):
    document_id: PositiveId
    expected_current_state: CurrentDocumentMetadata
    restore_state: CurrentDocumentMetadata
    verification: RollbackOperationVerification = RollbackOperationVerification.VERIFIED


class RollbackRecord(DomainModel):
    rollback_id: UUID = Field(default_factory=uuid4)
    source_run_id: str = Field(min_length=1)
    created_at: AwareDatetime = Field(default_factory=utc_now)
    operations: tuple[RollbackOperation, ...] = ()
    app_version: str
    paperless_url: str


class PaginationInfo(DomainModel):
    page: int = Field(ge=1)
    page_size: int = Field(ge=1)
    total_count: int = Field(ge=0)
    has_next: bool
    has_previous: bool


class DocumentPage(DomainModel):
    items: tuple[DocumentSummary, ...]
    pagination: PaginationInfo


class HealthStatus(DomainModel):
    reachable: bool
    authenticated: bool
    api_version: str | None = None
    server_version: str | None = None
    status: str | None = None


class ContentChunk(DomainModel):
    document_id: PositiveId
    content: str = Field(repr=False)
    offset: int = Field(ge=0)
    returned_characters: int = Field(ge=0)
    total_characters: int = Field(ge=0)
    truncated: bool

    @model_validator(mode="after")
    def validate_character_counts(self) -> Self:
        if self.returned_characters != len(self.content):
            raise ValueError("returned_characters must equal the content length")
        if self.offset + self.returned_characters > self.total_characters:
            raise ValueError("content range cannot exceed total_characters")
        return self


class TaxonomySnapshot(DomainModel):
    """Bounded read-only snapshot of all supported taxonomy definitions."""

    tags: tuple[TaxonomyItem, ...]
    correspondents: tuple[TaxonomyItem, ...]
    document_types: tuple[TaxonomyItem, ...]
    storage_paths: tuple[TaxonomyItem, ...]
    custom_fields: tuple[CustomFieldDefinition, ...]


class TagUsage(DomainModel):
    """Document usage count reported by Paperless for one tag."""

    tag_id: PositiveId
    document_count: int | None = Field(default=None, ge=0)

"""Read-only document workflows shared by CLI and MCP transports."""

from __future__ import annotations

from pydantic import JsonValue

from paperless_mcp.client import (
    CustomFieldValuePayload,
    DocumentPayload,
    NotePayload,
    PaperlessClient,
)
from paperless_mcp.config import Settings
from paperless_mcp.models import (
    ContentChunk,
    DocumentDetail,
    DocumentFilters,
    DocumentPage,
    DocumentSummary,
    HealthStatus,
    MissingMetadataField,
    PaginationInfo,
)


class DocumentService:
    """Bounded, read-only document workflows with no transport assumptions."""

    def __init__(self, client: PaperlessClient, settings: Settings) -> None:
        self._client = client
        self._max_page_size = settings.max_page_size
        self._max_content_characters = settings.max_content_characters
        self._max_notes = settings.max_notes
        self._max_note_characters = settings.max_note_characters

    async def health(self) -> HealthStatus:
        payload, versions = await self._client.health()
        return HealthStatus(
            reachable=True,
            authenticated=True,
            api_version=versions.api_version,
            server_version=versions.server_version,
            status=payload.status,
        )

    async def list_documents(
        self,
        *,
        filters: DocumentFilters | None = None,
        page: int = 1,
        page_size: int | None = None,
    ) -> DocumentPage:
        effective_page_size = min(25, self._max_page_size) if page_size is None else page_size
        _validate_page(page, effective_page_size, self._max_page_size)
        active_filters = filters or DocumentFilters()
        tag_ids = set(active_filters.tag_ids)
        for tag_name in active_filters.tag_names:
            tag_ids.add(await self._client.resolve_tag_id(tag_name))
        params = _filter_params(
            active_filters.model_copy(update={"tag_ids": tuple(sorted(tag_ids)), "tag_names": ()})
        )
        params.update({"page": page, "page_size": effective_page_size})
        payload = await self._client.list_documents(params)
        return DocumentPage(
            items=tuple(_summary(item) for item in payload.results),
            pagination=PaginationInfo(
                page=page,
                page_size=effective_page_size,
                total_count=payload.count,
                has_next=payload.next is not None,
                has_previous=payload.previous is not None,
            ),
        )

    async def get_document(
        self,
        document_id: int,
        *,
        include_notes: bool = True,
        include_custom_fields: bool = True,
    ) -> DocumentDetail:
        _validate_document_id(document_id)
        payload = await self._client.get_document(document_id)
        notes = await self._client.list_document_notes(document_id) if include_notes else ()
        bounded_notes = notes[: self._max_notes]
        return _detail(
            payload,
            notes=bounded_notes,
            notes_total_count=len(notes),
            notes_truncated=(
                len(notes) > len(bounded_notes)
                or any(len(note.note) > self._max_note_characters for note in bounded_notes)
            ),
            max_note_characters=self._max_note_characters,
            include_custom_fields=include_custom_fields,
        )

    async def get_notes(self, document_id: int) -> tuple[str, ...]:
        _validate_document_id(document_id)
        notes = await self._client.list_document_notes(document_id)
        return tuple(note.note[: self._max_note_characters] for note in notes[: self._max_notes])

    async def get_content(
        self,
        document_id: int,
        *,
        offset: int = 0,
        limit: int | None = None,
    ) -> ContentChunk:
        """Return an explicitly requested OCR text range.

        OCR is treated as an inert string. It is not parsed for commands and does
        not influence endpoint, filter, or policy selection.
        """
        _validate_document_id(document_id)
        if offset < 0:
            raise ValueError("offset must be non-negative")
        effective_limit = self._max_content_characters if limit is None else limit
        if effective_limit < 1 or effective_limit > self._max_content_characters:
            raise ValueError(
                f"limit must be between 1 and {self._max_content_characters} characters"
            )

        payload = await self._client.get_document(document_id)
        content = payload.content or ""
        total = len(content)
        if offset > total:
            raise ValueError("offset cannot exceed the document content length")
        chunk = content[offset : offset + effective_limit]
        return ContentChunk(
            document_id=document_id,
            content=chunk,
            offset=offset,
            returned_characters=len(chunk),
            total_characters=total,
            truncated=offset + len(chunk) < total,
        )

    async def list_missing_metadata(
        self,
        field: MissingMetadataField,
        *,
        page: int = 1,
        page_size: int | None = None,
    ) -> DocumentPage:
        return await self.list_documents(
            filters=DocumentFilters(missing=field),
            page=page,
            page_size=page_size,
        )


def _validate_document_id(document_id: int) -> None:
    if isinstance(document_id, bool) or not isinstance(document_id, int) or document_id < 1:
        raise ValueError("document_id must be a positive integer")


def _validate_page(page: int, page_size: int, maximum: int) -> None:
    if isinstance(page, bool) or not isinstance(page, int) or page < 1:
        raise ValueError("page must be a positive integer")
    if isinstance(page_size, bool) or not isinstance(page_size, int):
        raise ValueError("page_size must be an integer")
    if page_size < 1 or page_size > maximum:
        raise ValueError(f"page_size must be between 1 and {maximum}")


def _filter_params(filters: DocumentFilters) -> dict[str, str | int]:
    params: dict[str, str | int] = {"ordering": filters.ordering.value}
    if filters.text is not None:
        params["text"] = filters.text
    if filters.title is not None:
        params["title_search"] = filters.title
    if filters.correspondent_id is not None:
        params["correspondent__id"] = filters.correspondent_id
    if filters.document_type_id is not None:
        params["document_type__id"] = filters.document_type_id
    if filters.storage_path_id is not None:
        params["storage_path__id"] = filters.storage_path_id
    if filters.tag_ids:
        params["tags__id__all"] = ",".join(str(tag_id) for tag_id in filters.tag_ids)
    if filters.created_after is not None:
        params["created__date__gte"] = filters.created_after.isoformat()
    if filters.created_before is not None:
        params["created__date__lte"] = filters.created_before.isoformat()
    if filters.added_after is not None:
        params["added__date__gte"] = filters.added_after.isoformat()
    if filters.added_before is not None:
        params["added__date__lte"] = filters.added_before.isoformat()
    if filters.archive_serial_number is not None:
        params["archive_serial_number"] = filters.archive_serial_number
    if filters.original_filename is not None:
        params["original_filename__icontains"] = filters.original_filename
    if filters.untagged is not None:
        params["is_tagged"] = "false" if filters.untagged else "true"
    if filters.missing is not None:
        params[f"{filters.missing.value}__isnull"] = "true"
    return params


def _summary(payload: DocumentPayload) -> DocumentSummary:
    return DocumentSummary(
        id=payload.id,
        title=payload.title,
        created=payload.created,
        added=payload.added,
        modified=payload.modified,
        correspondent_id=payload.correspondent,
        document_type_id=payload.document_type,
        storage_path_id=payload.storage_path,
        tag_ids=payload.tags,
        archive_serial_number=payload.archive_serial_number,
        original_filename=payload.original_file_name,
    )


def _detail(
    payload: DocumentPayload,
    *,
    notes: tuple[NotePayload, ...],
    notes_total_count: int,
    notes_truncated: bool,
    max_note_characters: int,
    include_custom_fields: bool,
) -> DocumentDetail:
    custom_fields: dict[int, JsonValue] = (
        {field.field: field.value for field in payload.custom_fields}
        if include_custom_fields
        else {}
    )
    summary = _summary(payload)
    return DocumentDetail(
        **summary.model_dump(),
        content=None,
        content_length=len(payload.content) if payload.content is not None else None,
        content_truncated=payload.content is not None,
        custom_fields=custom_fields,
        notes=tuple(note.note[:max_note_characters] for note in notes),
        notes_total_count=notes_total_count,
        notes_truncated=notes_truncated,
    )


def custom_field_values(
    values: list[CustomFieldValuePayload],
) -> dict[int, JsonValue]:
    """Convert Paperless's field/value list to the canonical keyed representation."""
    return {item.field: item.value for item in values}

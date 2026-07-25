"""Read-only taxonomy workflows and conservative duplicate hints."""

from __future__ import annotations

import re
import unicodedata
from typing import cast

from paperless_mcp.client import (
    CustomFieldPayload,
    PaperlessClient,
    TaxonomyEndpoint,
    TaxonomyPayload,
)
from paperless_mcp.config import Settings
from paperless_mcp.models import (
    CustomFieldDefinition,
    DuplicateTaxonomyCandidate,
    TagUsage,
    TaxonomyItem,
    TaxonomyKind,
    TaxonomySnapshot,
)

_ENDPOINTS: dict[TaxonomyKind, TaxonomyEndpoint] = {
    TaxonomyKind.TAG: "tags",
    TaxonomyKind.CORRESPONDENT: "correspondents",
    TaxonomyKind.DOCUMENT_TYPE: "document_types",
    TaxonomyKind.STORAGE_PATH: "storage_paths",
}


class TaxonomyService:
    """Typed taxonomy reads; no creation or mutation is possible here."""

    def __init__(self, client: PaperlessClient, settings: Settings) -> None:
        self._client = client
        self._max_items = settings.max_page_size

    async def list_items(
        self,
        kind: TaxonomyKind,
        *,
        limit: int | None = None,
    ) -> tuple[TaxonomyItem, ...]:
        if kind is TaxonomyKind.CUSTOM_FIELD:
            raise ValueError("use list_custom_fields for custom field definitions")
        effective_limit = self._validated_limit(limit)
        endpoint = _ENDPOINTS[kind]
        payloads = await self._client.list_taxonomy(endpoint, max_items=effective_limit)
        taxonomy_payloads = cast(tuple[TaxonomyPayload, ...], payloads)
        return tuple(
            TaxonomyItem(
                id=payload.id,
                kind=kind,
                name=payload.name,
                slug=payload.slug,
                document_count=payload.document_count,
                matching_algorithm=payload.matching_algorithm,
                is_insensitive=payload.is_insensitive,
            )
            for payload in taxonomy_payloads
        )

    async def list_custom_fields(
        self,
        *,
        limit: int | None = None,
        include_extra_data: bool = True,
    ) -> tuple[CustomFieldDefinition, ...]:
        effective_limit = self._validated_limit(limit)
        payloads = await self._client.list_taxonomy(
            "custom_fields",
            max_items=effective_limit,
        )
        custom_field_payloads = cast(tuple[CustomFieldPayload, ...], payloads)
        return tuple(
            CustomFieldDefinition(
                id=payload.id,
                name=payload.name,
                data_type=payload.data_type,
                extra_data=payload.extra_data if include_extra_data else None,
                document_count=payload.document_count,
            )
            for payload in custom_field_payloads
        )

    async def tag_usage_counts(
        self,
        *,
        limit: int | None = None,
    ) -> dict[int, int | None]:
        tags = await self.list_items(TaxonomyKind.TAG, limit=limit)
        return {tag.id: tag.document_count for tag in tags}

    async def tag_usage(
        self,
        *,
        limit: int | None = None,
    ) -> tuple[TagUsage, ...]:
        """Return typed tag usage records suitable for transport schemas."""
        counts = await self.tag_usage_counts(limit=limit)
        return tuple(
            TagUsage(tag_id=tag_id, document_count=document_count)
            for tag_id, document_count in counts.items()
        )

    async def get_snapshot(
        self,
        *,
        limit: int | None = None,
        include_custom_field_extra_data: bool = True,
    ) -> TaxonomySnapshot:
        """Return a bounded snapshot of every supported taxonomy kind."""
        return TaxonomySnapshot(
            tags=await self.list_items(TaxonomyKind.TAG, limit=limit),
            correspondents=await self.list_items(TaxonomyKind.CORRESPONDENT, limit=limit),
            document_types=await self.list_items(TaxonomyKind.DOCUMENT_TYPE, limit=limit),
            storage_paths=await self.list_items(TaxonomyKind.STORAGE_PATH, limit=limit),
            custom_fields=await self.list_custom_fields(
                limit=limit,
                include_extra_data=include_custom_field_extra_data,
            ),
        )

    async def probable_duplicate_tags(
        self,
        *,
        limit: int | None = None,
    ) -> tuple[DuplicateTaxonomyCandidate, ...]:
        tags = await self.list_items(TaxonomyKind.TAG, limit=limit)
        groups: dict[str, list[TaxonomyItem]] = {}
        for tag in tags:
            normalized = _normalize_name(tag.name)
            if normalized:
                groups.setdefault(normalized, []).append(tag)

        return tuple(
            DuplicateTaxonomyCandidate(
                normalized_name=normalized,
                item_ids=tuple(item.id for item in items),
                names=tuple(item.name for item in items),
            )
            for normalized, items in sorted(groups.items())
            if len(items) > 1
        )

    def _validated_limit(self, limit: int | None) -> int:
        effective = self._max_items if limit is None else limit
        if isinstance(effective, bool) or not isinstance(effective, int):
            raise ValueError("limit must be an integer")
        if effective < 1 or effective > self._max_items:
            raise ValueError(f"limit must be between 1 and {self._max_items}")
        return effective


def _normalize_name(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold().strip()
    return re.sub(r"\s+", " ", normalized)

from __future__ import annotations

import pytest
from pydantic import ValidationError

from paperless_mcp.models import (
    DocumentFilters,
    DocumentOrdering,
    DuplicateTaxonomyCandidate,
    MissingMetadataField,
)


def test_document_filters_are_allowlisted_and_strict() -> None:
    filters = DocumentFilters(
        text="invoice",
        title="Electric Ireland",
        tag_ids=[5, 2, 5],
        missing=MissingMetadataField.CORRESPONDENT,
        ordering=DocumentOrdering.CREATED_DESC,
    )

    assert filters.tag_ids == (2, 5)
    assert filters.ordering is DocumentOrdering.CREATED_DESC


def test_document_filters_reject_unknown_query_parameters() -> None:
    with pytest.raises(ValidationError, match="Extra inputs"):
        DocumentFilters(advanced_query="owner:admin")  # type: ignore[call-arg]


def test_duplicate_candidate_requires_at_least_two_ids() -> None:
    with pytest.raises(ValidationError):
        DuplicateTaxonomyCandidate(
            normalized_name="invoice",
            item_ids=[1],
            names=["Invoice", "invoice"],
        )

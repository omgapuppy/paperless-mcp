from datetime import date

import pytest
from pydantic import ValidationError

from paperless_mcp.models import (
    BatchProposal,
    ConfidenceScore,
    ContentChunk,
    ExpectedCurrentState,
    ProposedDocumentChange,
    ProposedDocumentChanges,
)


def expected_state() -> ExpectedCurrentState:
    return ExpectedCurrentState(
        title="scan_001",
        created=date(2026, 6, 1),
        tag_ids=[4, 1, 4],
        correspondent_id=None,
        document_type_id=None,
    )


@pytest.mark.parametrize("value", [-0.01, 1.01])
def test_confidence_rejects_out_of_range_values(value: float) -> None:
    with pytest.raises(ValidationError):
        ConfidenceScore(value)


def test_confidence_accepts_range_boundaries() -> None:
    assert ConfidenceScore(0).value == 0
    assert ConfidenceScore(1).value == 1


def test_current_state_canonicalizes_tag_ids() -> None:
    assert expected_state().tag_ids == (1, 4)


@pytest.mark.parametrize("tag_ids", [[0], [-1], [1, 0]])
def test_current_state_rejects_non_positive_tag_ids(tag_ids: list[int]) -> None:
    with pytest.raises(ValidationError, match="positive"):
        ExpectedCurrentState(title="scan_001", tag_ids=tag_ids)


@pytest.mark.parametrize("tag_ids", [[True], ["2"], [1, "2"]])
def test_current_state_rejects_non_integer_tag_ids(tag_ids: list[object]) -> None:
    with pytest.raises(ValidationError, match="integers"):
        ExpectedCurrentState(title="scan_001", tag_ids=tag_ids)


@pytest.mark.parametrize("document_id", [True, "123", 1.5])
def test_document_id_does_not_coerce_untrusted_values(document_id: object) -> None:
    with pytest.raises(ValidationError):
        ProposedDocumentChange(
            document_id=document_id,
            expected_current_state=expected_state(),
            changes=ProposedDocumentChanges(title="Useful title"),
            confidence=0.9,
            reason="Evidence is clear.",
        )


def test_confidence_does_not_coerce_strings() -> None:
    with pytest.raises(ValidationError):
        ConfidenceScore("0.97")  # type: ignore[arg-type]


def test_change_rejects_overlapping_tag_operations() -> None:
    with pytest.raises(ValidationError, match="both added and removed"):
        ProposedDocumentChanges(add_tag_ids=[1, 2], remove_tag_ids=[2, 3])


def test_change_rejects_replace_combined_with_delta() -> None:
    with pytest.raises(ValidationError, match="cannot be combined"):
        ProposedDocumentChanges(replace_tag_ids=[1, 2], add_tag_ids=[3])


def test_change_requires_a_meaningful_operation() -> None:
    with pytest.raises(ValidationError, match="at least one"):
        ProposedDocumentChanges()


def test_nullable_taxonomy_change_is_meaningful() -> None:
    change = ProposedDocumentChanges(correspondent_id=None)
    assert "correspondent_id" in change.model_fields_set


def test_batch_rejects_duplicate_document_ids() -> None:
    proposed = ProposedDocumentChange(
        document_id=123,
        expected_current_state=expected_state(),
        changes=ProposedDocumentChanges(
            title="Electric Ireland – Electricity Bill – June 2026",
            add_tag_ids=[12],
            remove_tag_ids=[1],
            correspondent_id=8,
            document_type_id=3,
        ),
        confidence=0.97,
        reason="Issuer and billing period are explicit in the document.",
    )

    with pytest.raises(ValidationError, match="one change per document"):
        BatchProposal(description="Classify Inbox documents", changes=[proposed, proposed])


def test_content_chunk_counts_must_match() -> None:
    with pytest.raises(ValidationError, match="content length"):
        ContentChunk(
            document_id=1,
            content="data only",
            offset=0,
            returned_characters=4,
            total_characters=9,
            truncated=False,
        )

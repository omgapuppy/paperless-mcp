"""Guard behavior for taxonomy create, rename and delete."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import respx

from paperless_mcp.client import PaperlessClient
from paperless_mcp.config import Settings
from paperless_mcp.models import (
    MatchingAlgorithm,
    MatchingRule,
    MutationStatus,
    ProposedTaxonomyChange,
    TaxonomyAction,
    TaxonomyKind,
)
from paperless_mcp.services.taxonomy_mutations import TaxonomyMutationService

BASE_URL = "https://paperless.example.test"
TOKEN = "never-show-this-token"


def settings(tmp_path: object = None, **overrides: object) -> Settings:
    values: dict[str, object] = {
        "PAPERLESS_URL": BASE_URL,
        "PAPERLESS_API_TOKEN": TOKEN,
        "PAPERLESS_MCP_RETRY_ATTEMPTS": 0,
        "PAPERLESS_MCP_PROTECTED_TAGS": "needs-review",
        "PAPERLESS_MCP_WRITE_ENABLED": True,
        "PAPERLESS_MCP_DELETE_ENABLED": True,
        "PAPERLESS_MCP_ALLOW_TAXONOMY_CREATION": True,
    }
    if tmp_path is not None:
        values["PAPERLESS_MCP_AUDIT_DIR"] = str(tmp_path)
    values.update(overrides)
    return Settings.model_validate(values)


def tag_item(
    item_id: int,
    name: str,
    *,
    count: int = 0,
    children: list[object] | None = None,
) -> dict[str, object]:
    return {
        "id": item_id,
        "name": name,
        "slug": name.lower(),
        "document_count": count,
        "children": children or [],
    }


def rename(item_id: int, expected: str, new: str) -> ProposedTaxonomyChange:
    return ProposedTaxonomyChange(
        action=TaxonomyAction.RENAME,
        kind=TaxonomyKind.TAG,
        item_id=item_id,
        expected_current_name=expected,
        name=new,
        reason="consolidating duplicate tags",
    )


def delete(item_id: int, expected: str) -> ProposedTaxonomyChange:
    return ProposedTaxonomyChange(
        action=TaxonomyAction.DELETE,
        kind=TaxonomyKind.TAG,
        item_id=item_id,
        expected_current_name=expected,
        reason="retiring an unused tag",
    )


@pytest.mark.integration
@respx.mock
async def test_delete_refuses_while_documents_still_reference_the_tag() -> None:
    respx.get(f"{BASE_URL}/api/tags/7/").mock(
        return_value=httpx.Response(200, json=tag_item(7, "finance", count=124))
    )
    destroy = respx.delete(f"{BASE_URL}/api/tags/7/").mock(return_value=httpx.Response(204))

    async with PaperlessClient(settings()) as client:
        result = await TaxonomyMutationService(client, settings()).execute(
            [delete(7, "finance")], apply=True
        )

    assert result.status is MutationStatus.REJECTED
    assert result.applied_count == 0
    assert not destroy.called
    assert "still on 124 document" in (result.mutations[0].error_message or "")


@pytest.mark.integration
@respx.mock
async def test_delete_refuses_while_child_tags_remain() -> None:
    respx.get(f"{BASE_URL}/api/tags/8/").mock(
        return_value=httpx.Response(
            200, json=tag_item(8, "area", count=0, children=[tag_item(9, "area/home")])
        )
    )
    destroy = respx.delete(f"{BASE_URL}/api/tags/8/").mock(return_value=httpx.Response(204))

    async with PaperlessClient(settings()) as client:
        result = await TaxonomyMutationService(client, settings()).execute(
            [delete(8, "area")], apply=True
        )

    assert result.status is MutationStatus.REJECTED
    assert not destroy.called
    assert "child tag" in (result.mutations[0].error_message or "")


@pytest.mark.integration
@respx.mock
async def test_protected_tag_cannot_be_renamed() -> None:
    respx.get(f"{BASE_URL}/api/tags/12/").mock(
        return_value=httpx.Response(200, json=tag_item(12, "needs-review", count=129))
    )
    patch = respx.patch(f"{BASE_URL}/api/tags/12/").mock(return_value=httpx.Response(200))

    async with PaperlessClient(settings()) as client:
        result = await TaxonomyMutationService(client, settings()).execute(
            [rename(12, "needs-review", "review")], apply=True
        )

    assert result.status is MutationStatus.REJECTED
    assert not patch.called
    assert "protected" in (result.mutations[0].error_message or "")


@pytest.mark.integration
@respx.mock
async def test_rename_refuses_when_the_current_name_has_drifted() -> None:
    respx.get(f"{BASE_URL}/api/tags/5/").mock(
        return_value=httpx.Response(200, json=tag_item(5, "renamed-elsewhere", count=3))
    )
    patch = respx.patch(f"{BASE_URL}/api/tags/5/").mock(return_value=httpx.Response(200))

    async with PaperlessClient(settings()) as client:
        result = await TaxonomyMutationService(client, settings()).execute(
            [rename(5, "finance", "area/banking")], apply=True
        )

    assert result.status is MutationStatus.REJECTED
    assert not patch.called
    assert "Stale proposal" in (result.mutations[0].error_message or "")


@pytest.mark.integration
@respx.mock
async def test_one_rejection_blocks_every_write_in_the_batch() -> None:
    respx.get(f"{BASE_URL}/api/tags/5/").mock(
        return_value=httpx.Response(200, json=tag_item(5, "finance", count=3))
    )
    respx.get(f"{BASE_URL}/api/tags/12/").mock(
        return_value=httpx.Response(200, json=tag_item(12, "needs-review", count=129))
    )
    patch = respx.patch(f"{BASE_URL}/api/tags/5/").mock(return_value=httpx.Response(200))

    async with PaperlessClient(settings()) as client:
        result = await TaxonomyMutationService(client, settings()).execute(
            [rename(5, "finance", "area/banking"), rename(12, "needs-review", "review")],
            apply=True,
        )

    assert result.status is MutationStatus.REJECTED
    assert result.applied_count == 0
    assert not patch.called, "a valid change must not be written when a sibling is rejected"


@pytest.mark.integration
@respx.mock
async def test_preview_never_writes() -> None:
    respx.get(f"{BASE_URL}/api/tags/5/").mock(
        return_value=httpx.Response(200, json=tag_item(5, "finance", count=3))
    )
    patch = respx.patch(f"{BASE_URL}/api/tags/5/").mock(return_value=httpx.Response(200))

    async with PaperlessClient(settings()) as client:
        result = await TaxonomyMutationService(client, settings()).execute(
            [rename(5, "finance", "area/banking")], apply=False
        )

    assert result.status is MutationStatus.DRY_RUN
    assert result.dry_run is True
    assert not patch.called
    assert result.mutations[0].inverse == "rename back to 'finance'"


@pytest.mark.integration
@respx.mock
async def test_creation_requires_its_own_enablement() -> None:
    create = respx.post(f"{BASE_URL}/api/tags/").mock(return_value=httpx.Response(201))
    disabled = settings(PAPERLESS_MCP_ALLOW_TAXONOMY_CREATION=False)
    change = ProposedTaxonomyChange(
        action=TaxonomyAction.CREATE,
        kind=TaxonomyKind.TAG,
        name="area/banking",
        reason="new grouped vocabulary",
    )

    async with PaperlessClient(disabled) as client:
        result = await TaxonomyMutationService(client, disabled).execute([change], apply=True)

    assert result.status is MutationStatus.REJECTED
    assert not create.called
    assert result.mutations[0].error_code == "TaxonomyCreationDisabledError"


@pytest.mark.integration
@respx.mock
async def test_deletion_requires_its_own_enablement() -> None:
    respx.get(f"{BASE_URL}/api/tags/5/").mock(
        return_value=httpx.Response(200, json=tag_item(5, "junk", count=0))
    )
    destroy = respx.delete(f"{BASE_URL}/api/tags/5/").mock(return_value=httpx.Response(204))
    disabled = settings(PAPERLESS_MCP_DELETE_ENABLED=False)

    async with PaperlessClient(disabled) as client:
        result = await TaxonomyMutationService(client, disabled).execute(
            [delete(5, "junk")], apply=True
        )

    assert result.status is MutationStatus.REJECTED
    assert not destroy.called
    assert result.mutations[0].error_code == "DeletesDisabledError"


@pytest.mark.integration
@respx.mock
async def test_writes_disabled_blocks_every_action() -> None:
    disabled = settings(PAPERLESS_MCP_WRITE_ENABLED=False)
    change = ProposedTaxonomyChange(
        action=TaxonomyAction.CREATE,
        kind=TaxonomyKind.TAG,
        name="area/home",
        reason="new grouped vocabulary",
    )

    async with PaperlessClient(disabled) as client:
        result = await TaxonomyMutationService(client, disabled).execute([change], apply=True)

    assert result.status is MutationStatus.REJECTED
    assert result.mutations[0].error_code == "WritesDisabledError"


@pytest.mark.integration
@respx.mock
async def test_applied_rename_records_an_audit_run(tmp_path: object) -> None:
    respx.get(f"{BASE_URL}/api/tags/5/").mock(
        return_value=httpx.Response(200, json=tag_item(5, "finance", count=3))
    )
    respx.patch(f"{BASE_URL}/api/tags/5/").mock(
        return_value=httpx.Response(200, json=tag_item(5, "area/banking", count=3))
    )
    configured = settings(tmp_path)

    async with PaperlessClient(configured) as client:
        result = await TaxonomyMutationService(client, configured).execute(
            [rename(5, "finance", "area/banking")], apply=True
        )

    assert result.status is MutationStatus.APPLIED
    assert result.applied_count == 1
    assert result.mutations[0].after_name == "area/banking"
    assert result.run_id is not None


@pytest.mark.integration
async def test_delete_proposal_rejects_a_name_at_the_model_boundary() -> None:
    with pytest.raises(ValueError, match="must not carry name"):
        ProposedTaxonomyChange(
            action=TaxonomyAction.DELETE,
            kind=TaxonomyKind.TAG,
            item_id=5,
            expected_current_name="junk",
            name="something",
            reason="retiring",
        )


@pytest.mark.integration
async def test_rename_proposal_requires_an_expected_current_name() -> None:
    with pytest.raises(ValueError, match="requires expected_current_name"):
        ProposedTaxonomyChange(
            action=TaxonomyAction.RENAME,
            kind=TaxonomyKind.TAG,
            item_id=5,
            name="area/banking",
            reason="consolidating",
        )


def correspondent_item(
    item_id: int,
    name: str,
    *,
    algorithm: int = 1,
    match: str = "",
    insensitive: bool = True,
) -> dict[str, object]:
    return {
        "id": item_id,
        "name": name,
        "slug": name.lower(),
        "document_count": 69,
        "matching_algorithm": algorithm,
        "match": match,
        "is_insensitive": insensitive,
    }


def set_matching(
    item_id: int,
    expected_name: str,
    expected: MatchingRule,
    new: MatchingRule,
    kind: TaxonomyKind = TaxonomyKind.CORRESPONDENT,
) -> ProposedTaxonomyChange:
    return ProposedTaxonomyChange(
        action=TaxonomyAction.SET_MATCHING,
        kind=kind,
        item_id=item_id,
        expected_current_name=expected_name,
        expected_current_matching=expected,
        matching=new,
        reason="empty any-word rule never matches",
    )


EMPTY_ANY = MatchingRule(algorithm=MatchingAlgorithm.ANY, match="")
IONITY_LITERAL = MatchingRule(algorithm=MatchingAlgorithm.LITERAL, match="IONITY")


@pytest.mark.integration
@respx.mock
async def test_set_matching_preview_reports_inverse_without_writing() -> None:
    respx.get(f"{BASE_URL}/api/correspondents/5/").mock(
        return_value=httpx.Response(200, json=correspondent_item(5, "IONITY"))
    )
    patch = respx.patch(f"{BASE_URL}/api/correspondents/5/").mock(return_value=httpx.Response(200))

    async with PaperlessClient(settings()) as client:
        result = await TaxonomyMutationService(client, settings()).execute(
            [set_matching(5, "IONITY", EMPTY_ANY, IONITY_LITERAL)], apply=False
        )

    assert result.status is MutationStatus.DRY_RUN
    assert not patch.called
    mutation = result.mutations[0]
    assert mutation.before_matching == EMPTY_ANY
    assert mutation.after_matching == IONITY_LITERAL
    assert mutation.reversible is True
    assert "restore matching to any ''" in (mutation.inverse or "")


@pytest.mark.integration
@respx.mock
async def test_set_matching_applies_wire_values_and_records_restore_plan(
    tmp_path: Path,
) -> None:
    respx.get(f"{BASE_URL}/api/correspondents/5/").mock(
        return_value=httpx.Response(200, json=correspondent_item(5, "IONITY"))
    )
    patch = respx.patch(f"{BASE_URL}/api/correspondents/5/").mock(
        return_value=httpx.Response(
            200, json=correspondent_item(5, "IONITY", algorithm=3, match="IONITY")
        )
    )
    configured = settings(tmp_path)

    async with PaperlessClient(configured) as client:
        result = await TaxonomyMutationService(client, configured).execute(
            [set_matching(5, "IONITY", EMPTY_ANY, IONITY_LITERAL)], apply=True
        )

    assert result.status is MutationStatus.APPLIED
    assert json.loads(patch.calls.last.request.content) == {
        "matching_algorithm": 3,
        "match": "IONITY",
        "is_insensitive": True,
    }
    assert result.mutations[0].after_matching == IONITY_LITERAL
    assert result.run_id is not None
    rollback = json.loads(next(tmp_path.rglob("rollback.json")).read_text())
    restore = rollback["operations"][0]["restore_matching"]
    assert restore == {"algorithm": "any", "match": "", "is_insensitive": True}


@pytest.mark.integration
@respx.mock
async def test_set_matching_refuses_when_the_live_rule_has_drifted() -> None:
    respx.get(f"{BASE_URL}/api/correspondents/5/").mock(
        return_value=httpx.Response(
            200, json=correspondent_item(5, "IONITY", algorithm=3, match="IONITY GmbH")
        )
    )
    patch = respx.patch(f"{BASE_URL}/api/correspondents/5/").mock(return_value=httpx.Response(200))

    async with PaperlessClient(settings()) as client:
        result = await TaxonomyMutationService(client, settings()).execute(
            [set_matching(5, "IONITY", EMPTY_ANY, IONITY_LITERAL)], apply=True
        )

    assert result.status is MutationStatus.REJECTED
    assert not patch.called
    assert "Stale proposal" in (result.mutations[0].error_message or "")


@pytest.mark.integration
@respx.mock
async def test_set_matching_is_blocked_when_writes_are_disabled() -> None:
    patch = respx.patch(f"{BASE_URL}/api/correspondents/5/").mock(return_value=httpx.Response(200))
    disabled = settings(PAPERLESS_MCP_WRITE_ENABLED=False)

    async with PaperlessClient(disabled) as client:
        result = await TaxonomyMutationService(client, disabled).execute(
            [set_matching(5, "IONITY", EMPTY_ANY, IONITY_LITERAL)], apply=True
        )

    assert result.status is MutationStatus.REJECTED
    assert not patch.called
    assert result.mutations[0].error_code == "WritesDisabledError"


@pytest.mark.integration
@respx.mock
async def test_set_matching_refuses_a_protected_tag() -> None:
    respx.get(f"{BASE_URL}/api/tags/12/").mock(
        return_value=httpx.Response(
            200, json={**tag_item(12, "needs-review", count=1), "matching_algorithm": 6}
        )
    )
    patch = respx.patch(f"{BASE_URL}/api/tags/12/").mock(return_value=httpx.Response(200))
    auto = MatchingRule(algorithm=MatchingAlgorithm.AUTO)

    async with PaperlessClient(settings()) as client:
        result = await TaxonomyMutationService(client, settings()).execute(
            [
                set_matching(
                    12,
                    "needs-review",
                    auto,
                    EMPTY_ANY.model_copy(update={"algorithm": MatchingAlgorithm.NONE}),
                    kind=TaxonomyKind.TAG,
                )
            ],
            apply=True,
        )

    assert result.status is MutationStatus.REJECTED
    assert not patch.called
    assert "protected" in (result.mutations[0].error_message or "")


@pytest.mark.integration
async def test_set_matching_rejects_a_rule_that_can_never_fire() -> None:
    with pytest.raises(ValueError, match="requires non-empty match text"):
        set_matching(5, "IONITY", IONITY_LITERAL, EMPTY_ANY)


@pytest.mark.integration
async def test_set_matching_rejects_an_invalid_regex() -> None:
    with pytest.raises(ValueError, match="not a valid regular expression"):
        set_matching(
            5,
            "IONITY",
            EMPTY_ANY,
            MatchingRule(algorithm=MatchingAlgorithm.REGEX, match="INV-(DF"),
        )


@pytest.mark.integration
async def test_matching_fields_are_refused_on_other_actions() -> None:
    with pytest.raises(ValueError, match="apply to set_matching only"):
        ProposedTaxonomyChange(
            action=TaxonomyAction.RENAME,
            kind=TaxonomyKind.TAG,
            item_id=5,
            expected_current_name="finance",
            name="area/banking",
            matching=IONITY_LITERAL,
            reason="consolidating",
        )

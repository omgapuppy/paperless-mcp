"""Guard behavior for taxonomy create, rename and delete."""

from __future__ import annotations

import httpx
import pytest
import respx

from paperless_mcp.client import PaperlessClient
from paperless_mcp.config import Settings
from paperless_mcp.models import (
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

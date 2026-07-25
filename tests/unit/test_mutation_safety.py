from __future__ import annotations

import json
import stat
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from paperless_mcp.client import PaperlessClient
from paperless_mcp.config import Settings
from paperless_mcp.errors import RollbackConflictError, WritesDisabledError
from paperless_mcp.models import (
    BatchProposal,
    ExpectedCurrentState,
    InitiatingInterface,
    MutationStatus,
    ProposedDocumentChange,
    ProposedDocumentChanges,
)
from paperless_mcp.services.mutations import MutationService, current_metadata
from paperless_mcp.services.proposals import ProposalService, TaxonomyPolicy
from paperless_mcp.services.rollback import RollbackService, load_rollback_file

BASE_URL = "https://paperless.example.test"


class FakePaperless:
    def __init__(self) -> None:
        self.documents: dict[int, dict[str, Any]] = {7: self.document()}
        self.taxonomy: dict[str, dict[int, str]] = {
            "tags": {1: "Inbox", 2: "Finance"},
            "correspondents": {3: "Utility"},
            "document_types": {4: "Invoice"},
            "storage_paths": {5: "Archive"},
            "custom_fields": {7: "Old", 8: "Account"},
        }
        self.requests: list[httpx.Request] = []
        self.fail_patch_ids: set[int] = set()
        self.fail_get_ids: set[int] = set()
        self.patch_calls: dict[int, int] = {}
        self.notes: dict[int, list[dict[str, Any]]] = {}
        self.tag_parents: dict[int, int] = {}
        self.note_response_omits_new = False

    @staticmethod
    def document(document_id: int = 7, title: str = "scan_001") -> dict[str, Any]:
        return {
            "id": document_id,
            "title": title,
            "content": "sensitive OCR must not be audited",
            "created": "2026-07-01",
            "modified": "2026-07-03T11:00:00Z",
            "correspondent": None,
            "document_type": None,
            "storage_path": None,
            "tags": [],
            "archive_serial_number": None,
            "custom_fields": [],
        }

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        parts = request.url.path.strip("/").split("/")
        if len(parts) == 3 and parts[:1] == ["api"] and parts[1] in self.taxonomy:
            item_id = int(parts[2])
            name = self.taxonomy[parts[1]].get(item_id)
            if name is None:
                return httpx.Response(404, json={"detail": "not found"})
            payload: dict[str, Any] = {"id": item_id, "name": name}
            if parts[1] == "tags":
                payload["parent"] = self.tag_parents.get(item_id)
                payload["children"] = [
                    self._tag_payload(child_id)
                    for child_id, parent_id in self.tag_parents.items()
                    if parent_id == item_id
                ]
            return httpx.Response(200, json=payload)

        if len(parts) == 3 and parts[:2] == ["api", "documents"]:
            document_id = int(parts[2])
            if request.method == "GET":
                if document_id in self.fail_get_ids:
                    return httpx.Response(503, json={"detail": "temporary"})
                return httpx.Response(200, json=self.documents[document_id])
            if request.method == "PATCH":
                self.patch_calls[document_id] = self.patch_calls.get(document_id, 0) + 1
                if document_id in self.fail_patch_ids:
                    return httpx.Response(503, json={"detail": "temporary"})
                payload = json.loads(request.content)
                document = self.documents[document_id]
                if "tags" in payload:
                    payload["tags"] = self._effective_tags(
                        set(document["tags"]),
                        set(payload["tags"]),
                    )
                document.update(payload)
                if "custom_fields" in payload:
                    document["custom_fields"] = payload["custom_fields"]
                document["modified"] = "2026-07-04T12:00:00Z"
                return httpx.Response(200, json=document)

        if len(parts) == 4 and parts[:2] == ["api", "documents"] and parts[3] == "notes":
            document_id = int(parts[2])
            if request.method == "GET":
                return httpx.Response(200, json=self.notes.get(document_id, []))
            assert request.method == "POST"
            note = json.loads(request.content)["note"]
            notes = self.notes.setdefault(document_id, [])
            notes.append({"id": len(notes) + 1, "note": note})
            return httpx.Response(
                200,
                json=notes[:-1] if self.note_response_omits_new else notes,
            )
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    def _tag_payload(self, tag_id: int) -> dict[str, Any]:
        return {
            "id": tag_id,
            "name": self.taxonomy["tags"][tag_id],
            "parent": self.tag_parents.get(tag_id),
            "children": [
                self._tag_payload(child_id)
                for child_id, parent_id in self.tag_parents.items()
                if parent_id == tag_id
            ],
        }

    def _effective_tags(self, before: set[int], requested: set[int]) -> list[int]:
        removed = before - requested
        blocked = set(removed)
        for tag_id in before:
            current = tag_id
            while current in self.tag_parents:
                current = self.tag_parents[current]
                if current in removed:
                    blocked.add(tag_id)
                    break
        final = set(requested)
        for tag_id in requested:
            current = tag_id
            while current in self.tag_parents:
                current = self.tag_parents[current]
                final.add(current)
        return sorted(final - blocked)


def settings(tmp_path: Path, *, writes: bool, max_batch_size: int = 25) -> Settings:
    return Settings(
        paperless_url=BASE_URL,
        paperless_api_token=SecretStr("test-token"),
        write_enabled=writes,
        max_batch_size=max_batch_size,
        retry_attempts=3,
        audit_dir=tmp_path / "audit",
    )


def services(
    fake: FakePaperless,
    app_settings: Settings,
) -> tuple[PaperlessClient, ProposalService, MutationService, RollbackService]:
    http_client = httpx.AsyncClient(transport=httpx.MockTransport(fake))
    client = PaperlessClient(app_settings, http_client=http_client)
    proposals = ProposalService(
        client,
        app_settings,
        TaxonomyPolicy(protected_tags=("Inbox",), review_tag="Needs Review"),
    )
    mutations = MutationService(client, app_settings, proposals)
    return client, proposals, mutations, RollbackService(mutations, app_settings)


def expected(document: dict[str, Any]) -> ExpectedCurrentState:
    payload = current_metadata_from_dict(document)
    return ExpectedCurrentState.model_validate(payload.model_dump())


def current_metadata_from_dict(document: dict[str, Any]) -> Any:
    from paperless_mcp.client import DocumentPayload

    return current_metadata(DocumentPayload.model_validate(document))


def proposal(
    document: dict[str, Any],
    changes: ProposedDocumentChanges,
    *,
    allow_protected: tuple[str, ...] = (),
) -> BatchProposal:
    return BatchProposal(
        description="safety-test",
        changes=(
            ProposedDocumentChange(
                document_id=int(document["id"]),
                expected_current_state=expected(document),
                changes=changes,
                confidence=0.97,
                reason="Explicit test evidence.",
                allow_protected_tag_removal=allow_protected,
            ),
        ),
    )


@pytest.fixture
def fake() -> Iterator[FakePaperless]:
    yield FakePaperless()


async def test_apply_flag_is_required_and_dry_run_never_patches(
    tmp_path: Path,
    fake: FakePaperless,
) -> None:
    _, _, mutations, _ = services(fake, settings(tmp_path, writes=True))
    result = await mutations.execute(
        proposal(fake.documents[7], ProposedDocumentChanges(title="Useful title"))
    )

    assert result.status is MutationStatus.DRY_RUN
    assert result.dry_run is True
    assert fake.patch_calls == {}
    assert not (tmp_path / "audit").exists()


async def test_server_disabled_writes_fail_before_any_http(
    tmp_path: Path,
    fake: FakePaperless,
) -> None:
    _, _, mutations, _ = services(fake, settings(tmp_path, writes=False))
    with pytest.raises(WritesDisabledError):
        await mutations.execute(
            proposal(fake.documents[7], ProposedDocumentChanges(title="Useful title")),
            apply=True,
        )
    assert fake.requests == []


async def test_batch_limit_and_missing_taxonomy_fail_closed(
    tmp_path: Path,
    fake: FakePaperless,
) -> None:
    _, _, mutations, _ = services(fake, settings(tmp_path, writes=True, max_batch_size=1))
    change = proposal(
        fake.documents[7],
        ProposedDocumentChanges(add_tag_ids=(999,)),
    ).changes[0]
    fake.documents[8] = fake.document(8)
    second = change.model_copy(
        update={"document_id": 8, "expected_current_state": expected(fake.documents[8])}
    )
    batch = BatchProposal(description="too large", changes=(change, second))

    result = await mutations.execute(batch)
    assert result.status is MutationStatus.REJECTED
    assert result.failure_count == 2
    assert fake.patch_calls == {}


async def test_stale_state_reports_exact_fields_and_force_is_audited(
    tmp_path: Path,
    fake: FakePaperless,
) -> None:
    app_settings = settings(tmp_path, writes=True)
    _, _, mutations, _ = services(fake, app_settings)
    proposed = proposal(fake.documents[7], ProposedDocumentChanges(title="Useful title"))
    fake.documents[7]["title"] = "Human changed this"

    stale = await mutations.execute(proposed)
    assert stale.conflict_count == 1
    assert stale.mutations[0].conflicting_fields == ("title",)

    forced = await mutations.execute(
        proposed,
        apply=True,
        force=True,
        interface=InitiatingInterface.CLI,
    )
    assert forced.status is MutationStatus.APPLIED
    manifest = json.loads(
        (Path(forced.rollback_path or "").parent / "manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["force"] is True


async def test_protected_tag_requires_exact_name_opt_in(
    tmp_path: Path,
    fake: FakePaperless,
) -> None:
    fake.documents[7]["tags"] = [1]
    _, _, mutations, _ = services(fake, settings(tmp_path, writes=True))
    blocked = await mutations.execute(
        proposal(fake.documents[7], ProposedDocumentChanges(remove_tag_ids=(1,))),
        apply=True,
    )
    assert blocked.failure_count == 1
    assert blocked.mutations[0].error_code == "protected_tag"
    assert fake.patch_calls == {}

    wrong_case = await mutations.execute(
        proposal(
            fake.documents[7],
            ProposedDocumentChanges(remove_tag_ids=(1,)),
            allow_protected=("inbox",),
        ),
        apply=True,
    )
    assert wrong_case.failure_count == 1

    allowed = await mutations.execute(
        proposal(
            fake.documents[7],
            ProposedDocumentChanges(remove_tag_ids=(1,)),
            allow_protected=("Inbox",),
        ),
        apply=True,
    )
    assert allowed.applied_count == 1
    assert fake.documents[7]["tags"] == []


async def test_parent_removal_cannot_cascade_remove_protected_descendant(
    tmp_path: Path,
    fake: FakePaperless,
) -> None:
    # Finance is the parent of protected Inbox. Paperless removes descendants
    # even when the requested full tag list still contains the child.
    fake.tag_parents[1] = 2
    fake.documents[7]["tags"] = [1, 2]
    _, _, mutations, _ = services(fake, settings(tmp_path, writes=True))

    blocked = await mutations.execute(
        proposal(fake.documents[7], ProposedDocumentChanges(remove_tag_ids=(2,))),
        apply=True,
    )

    assert blocked.status is MutationStatus.REJECTED
    assert blocked.mutations[0].error_code == "protected_tag"
    assert "Inbox" in (blocked.mutations[0].error_message or "")
    assert fake.patch_calls == {}
    assert fake.documents[7]["tags"] == [1, 2]

    allowed = await mutations.execute(
        proposal(
            fake.documents[7],
            ProposedDocumentChanges(remove_tag_ids=(2,)),
            allow_protected=("Inbox",),
        ),
        apply=True,
    )
    assert allowed.status is MutationStatus.APPLIED
    assert fake.documents[7]["tags"] == []


async def test_patch_uses_fresh_full_tag_and_custom_field_replacements(
    tmp_path: Path,
    fake: FakePaperless,
) -> None:
    fake.documents[7]["tags"] = [1]
    fake.documents[7]["custom_fields"] = [{"field": 7, "value": "old"}]
    _, _, mutations, _ = services(fake, settings(tmp_path, writes=True))
    result = await mutations.execute(
        proposal(
            fake.documents[7],
            ProposedDocumentChanges(
                add_tag_ids=(2,),
                custom_fields={8: "new"},
            ),
        ),
        apply=True,
    )

    assert result.status is MutationStatus.APPLIED
    patch = next(request for request in fake.requests if request.method == "PATCH")
    body = json.loads(patch.content)
    assert body["tags"] == [1, 2]
    assert body["custom_fields"] == [
        {"field": 7, "value": "old"},
        {"field": 8, "value": "new"},
    ]


async def test_partial_batch_is_sequential_audited_and_patch_is_not_retried(
    tmp_path: Path,
    fake: FakePaperless,
) -> None:
    fake.documents[8] = fake.document(8)
    fake.fail_patch_ids.add(8)
    _, _, mutations, _ = services(fake, settings(tmp_path, writes=True))
    first = proposal(fake.documents[7], ProposedDocumentChanges(title="First updated")).changes[0]
    second = proposal(fake.documents[8], ProposedDocumentChanges(title="Second updated")).changes[0]
    batch = BatchProposal(description="partial-test", changes=(first, second))

    result = await mutations.execute(batch, apply=True)

    assert result.status is MutationStatus.PARTIAL
    assert result.applied_count == 1
    assert result.failure_count == 1
    assert fake.patch_calls == {7: 1, 8: 1}
    run = Path(result.rollback_path or "").parent
    assert {
        "manifest.json",
        "proposal.json",
        "before.json",
        "applied.jsonl",
        "failures.jsonl",
        "rollback.json",
        "summary.md",
    } <= {path.name for path in run.iterdir()}
    assert "sensitive OCR" not in "\n".join(
        path.read_text(encoding="utf-8") for path in run.iterdir()
    )
    rollback = json.loads((run / "rollback.json").read_text(encoding="utf-8"))
    assert [operation["document_id"] for operation in rollback["operations"]] == [7]


async def test_rollback_preview_conflict_and_application_use_same_guards(
    tmp_path: Path,
    fake: FakePaperless,
) -> None:
    app_settings = settings(tmp_path, writes=True)
    _, _, mutations, rollback_service = services(fake, app_settings)
    applied = await mutations.execute(
        proposal(fake.documents[7], ProposedDocumentChanges(title="Updated")),
        apply=True,
    )
    rollback_path = Path(applied.rollback_path or "")
    record = load_rollback_file(rollback_path, app_settings)

    preview = await rollback_service.execute(record)
    assert preview.status is MutationStatus.DRY_RUN
    assert preview.mutations[0].after is not None
    assert preview.mutations[0].after.title == "scan_001"

    fake.documents[7]["title"] = "Human changed after apply"
    conflict = await rollback_service.execute(record)
    assert conflict.conflict_count == 1
    assert conflict.mutations[0].conflicting_fields == ("title",)

    fake.documents[7]["title"] = "Updated"
    restored = await rollback_service.execute(record, apply=True)
    assert restored.status is MutationStatus.APPLIED
    assert fake.documents[7]["title"] == "scan_001"
    assert restored.run_id is not None


async def test_batch_preflight_failure_prevents_every_patch(
    tmp_path: Path,
    fake: FakePaperless,
) -> None:
    fake.documents[8] = fake.document(8)
    first = proposal(fake.documents[7], ProposedDocumentChanges(title="First updated")).changes[0]
    second = proposal(fake.documents[8], ProposedDocumentChanges(title="Second updated")).changes[0]
    fake.fail_get_ids.add(8)
    _, _, mutations, _ = services(fake, settings(tmp_path, writes=True))

    result = await mutations.execute(
        BatchProposal(description="two-phase", changes=(first, second)),
        apply=True,
    )

    assert result.status is MutationStatus.REJECTED
    assert result.applied_count == 0
    assert result.failure_count == 2
    assert fake.patch_calls == {}
    assert result.mutations[0].error_code == "batch_preflight_failed"
    assert result.mutations[1].error_code == "invalid_paperless_response"
    run = tmp_path / "audit" / str(result.run_id)
    assert (run / "manifest.json").is_file()
    assert json.loads((run / "manifest.json").read_text(encoding="utf-8"))["finalized"] is True


async def test_noop_apply_reports_no_write_or_applied_item(
    tmp_path: Path,
    fake: FakePaperless,
) -> None:
    _, _, mutations, _ = services(fake, settings(tmp_path, writes=True))

    result = await mutations.execute(
        proposal(fake.documents[7], ProposedDocumentChanges(title="scan_001")),
        apply=True,
    )

    assert result.status is MutationStatus.NO_OP
    assert result.applied_count == 0
    assert result.noop_count == 1
    assert result.rollback_path is None
    assert fake.patch_calls == {}


async def test_final_audit_is_read_only_and_rollback_integrity_is_verified(
    tmp_path: Path,
    fake: FakePaperless,
) -> None:
    app_settings = settings(tmp_path, writes=True)
    _, _, mutations, _ = services(fake, app_settings)
    result = await mutations.execute(
        proposal(fake.documents[7], ProposedDocumentChanges(title="Updated")),
        apply=True,
    )
    rollback_path = Path(result.rollback_path or "")
    run = rollback_path.parent
    manifest = json.loads((run / "manifest.json").read_text(encoding="utf-8"))

    assert stat.S_IMODE(run.stat().st_mode) == 0o500
    assert all(stat.S_IMODE(path.stat().st_mode) == 0o400 for path in run.iterdir())
    assert manifest["finalized"] is True
    assert manifest["artifacts"]["rollback.json"]["sha256"]
    assert load_rollback_file(rollback_path, app_settings).source_run_id == result.run_id

    rollback_path.chmod(0o600)
    rollback_path.write_text("{}\n", encoding="utf-8")
    rollback_path.chmod(0o400)
    with pytest.raises(RollbackConflictError, match="integrity"):
        load_rollback_file(rollback_path, app_settings)


async def test_note_creation_verifies_exact_new_note_and_audits_ambiguity(
    tmp_path: Path,
    fake: FakePaperless,
) -> None:
    _, _, mutations, _ = services(fake, settings(tmp_path, writes=True))
    verified = await mutations.add_note(7, "Verified note", apply=True)
    assert verified.status is MutationStatus.APPLIED
    assert verified.applied_count == 1

    fake.note_response_omits_new = True
    ambiguous = await mutations.add_note(7, "Ambiguous note", apply=True)
    assert ambiguous.status is MutationStatus.INDETERMINATE
    assert ambiguous.applied_count == 0
    assert ambiguous.failure_count == 1
    assert ambiguous.mutations[0].error_code == "note_outcome_indeterminate"
    run = tmp_path / "audit" / str(ambiguous.run_id)
    recovery = json.loads((run / "rollback.json").read_text(encoding="utf-8"))
    assert recovery["outcome"] == "indeterminate"
    assert "Ambiguous note" not in "\n".join(
        path.read_text(encoding="utf-8") for path in run.iterdir()
    )

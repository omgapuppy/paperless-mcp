"""Guarded document mutations shared by CLI, MCP, and rollback workflows."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any, Literal

from pydantic import JsonValue

from paperless_mcp import __version__
from paperless_mcp.client import DocumentPayload, PaperlessClient, TaxonomyPayload
from paperless_mcp.config import Settings
from paperless_mcp.errors import (
    AuditDirectoryError,
    PaperlessMCPError,
    UnsupportedPaperlessBehaviorError,
    WritesDisabledError,
)
from paperless_mcp.logging import get_logger
from paperless_mcp.models import (
    BatchProposal,
    CurrentDocumentMetadata,
    DocumentMutation,
    DryRunAuditPreview,
    DryRunRollbackOperation,
    InitiatingInterface,
    MutationResult,
    MutationStatus,
    ProposedDocumentChange,
    ProposedDocumentChanges,
    RollbackOperation,
    RollbackOperationVerification,
    RollbackRecord,
)
from paperless_mcp.services.audit import AuditRun, metadata_hash, text_hash
from paperless_mcp.services.proposals import (
    ProposalService,
    effective_protected_tags,
)

MAX_NOTE_CHARACTERS = 10_000
_Outcome = Literal["ready", "applied", "noop", "conflict", "failure", "indeterminate"]
logger = get_logger(__name__)


@dataclass(frozen=True)
class _PreparedChange:
    proposed: ProposedDocumentChange
    before: CurrentDocumentMetadata
    target: CurrentDocumentMetadata
    preview: DocumentMutation


class MutationService:
    """Apply typed metadata changes only after every server-side safeguard."""

    def __init__(
        self,
        client: PaperlessClient,
        settings: Settings,
        proposals: ProposalService,
    ) -> None:
        self._client = client
        self._settings = settings
        self._proposals = proposals

    async def execute(
        self,
        proposal: BatchProposal,
        *,
        apply: bool = False,
        force: bool = False,
        interface: InitiatingInterface = InitiatingInterface.MCP,
    ) -> MutationResult:
        if force and not apply:
            raise ValueError("force is available only for an explicitly applied operation")
        if apply and not self._settings.write_enabled:
            raise WritesDisabledError(
                "Writes are disabled. Enable them server-side and explicitly set apply=true."
            )

        validation = await self._proposals.validate(proposal)
        if not validation.valid:
            return _log_mutation_result(
                MutationResult(
                    status=MutationStatus.REJECTED,
                    dry_run=not apply,
                    requested_count=len(proposal.changes),
                    failure_count=len(proposal.changes),
                    mutations=tuple(
                        DocumentMutation(
                            document_id=change.document_id,
                            before=_snapshot_metadata(change),
                            error_code="invalid_proposal",
                            error_message="The proposal failed server-side validation.",
                        )
                        for change in proposal.changes
                    ),
                    summary=validation.summary,
                ),
                operation=proposal.description,
            )

        audit = (
            AuditRun(
                settings=self._settings,
                client=self._client,
                operation=proposal.description,
                interface=interface,
                force=force,
                proposal=proposal,
            )
            if apply
            else None
        )

        prepared_by_id: dict[int, _PreparedChange] = {}
        preflight_mutations: dict[int, tuple[DocumentMutation, _Outcome]] = {}
        for proposed in proposal.changes:
            try:
                prepared, mutation, outcome = await self._preflight_change(proposed, force=force)
            except Exception as exc:
                prepared = None
                mutation = _failure_mutation(
                    proposed,
                    before=None,
                    after=None,
                    exc=exc,
                    phase="preflight",
                )
                outcome = "failure"
            if prepared is not None:
                prepared_by_id[proposed.document_id] = prepared
            preflight_mutations[proposed.document_id] = (mutation, outcome)

        preflight_failed = any(
            outcome in {"conflict", "failure"} for _, outcome in preflight_mutations.values()
        )
        if not apply:
            return _log_mutation_result(
                _preview_result(
                    proposal,
                    preflight_mutations,
                    interface=interface,
                    force=force,
                ),
                operation=proposal.description,
            )

        if preflight_failed:
            aborted_mutations: list[DocumentMutation] = []
            conflict_count = 0
            failure_count = 0
            for proposed in proposal.changes:
                mutation, outcome = preflight_mutations[proposed.document_id]
                if outcome == "ready":
                    mutation = mutation.model_copy(
                        update={
                            "error_code": "batch_preflight_failed",
                            "error_message": (
                                "No write was attempted because another item failed batch "
                                "preflight."
                            ),
                        }
                    )
                    failure_count += 1
                elif outcome == "conflict":
                    conflict_count += 1
                else:
                    failure_count += 1
                aborted_mutations.append(mutation)
                _record_audit(audit, applied=False, mutation=mutation)

            result = MutationResult(
                status=MutationStatus.REJECTED,
                dry_run=False,
                requested_count=len(proposal.changes),
                conflict_count=conflict_count,
                failure_count=failure_count,
                mutations=tuple(aborted_mutations),
                summary=(
                    "Batch preflight failed; no Paperless writes were attempted. "
                    f"{conflict_count} item(s) were stale and {failure_count} item(s) "
                    "failed or were safely aborted."
                ),
                run_id=audit.run_id if audit is not None else None,
                rollback_path=None,
            )
            self._finalize_audit(audit, prepared_by_id, (), result)
            return _log_mutation_result(result, operation=proposal.description)

        mutations: list[DocumentMutation] = []
        rollback_operations: list[RollbackOperation] = []
        applied_count = 0
        noop_count = 0
        conflict_count = 0
        failure_count = 0
        indeterminate_count = 0

        for proposed in proposal.changes:
            prepared = prepared_by_id[proposed.document_id]
            try:
                mutation, rollback_operation, outcome = await self._apply_prepared(
                    prepared,
                    force=force,
                )
            except Exception as exc:
                mutation = _failure_mutation(
                    proposed,
                    before=prepared.before,
                    after=None,
                    exc=exc,
                    phase="apply",
                )
                rollback_operation = None
                outcome = "failure"

            mutations.append(mutation)
            if rollback_operation is not None:
                rollback_operations.append(rollback_operation)
            if outcome == "applied":
                applied_count += 1
                _record_audit(audit, applied=True, mutation=mutation)
            elif outcome == "noop":
                noop_count += 1
            else:
                if outcome == "conflict":
                    conflict_count += 1
                else:
                    failure_count += 1
                    if outcome == "indeterminate":
                        indeterminate_count += 1
                _record_audit(audit, applied=False, mutation=mutation)

        status = _applied_status(
            applied=applied_count,
            noops=noop_count,
            conflicts=conflict_count,
            failures=failure_count,
            indeterminate=indeterminate_count,
        )
        rollback_path = (
            str(audit.path / "rollback.json") if audit is not None and rollback_operations else None
        )
        result = MutationResult(
            status=status,
            dry_run=False,
            requested_count=len(proposal.changes),
            applied_count=applied_count,
            noop_count=noop_count,
            conflict_count=conflict_count,
            failure_count=failure_count,
            mutations=tuple(mutations),
            summary=_result_summary(
                apply=True,
                requested=len(proposal.changes),
                applied=applied_count,
                noops=noop_count,
                conflicts=conflict_count,
                failures=failure_count,
                indeterminate=indeterminate_count,
            ),
            run_id=audit.run_id if audit is not None else None,
            rollback_path=rollback_path,
        )
        self._finalize_audit(audit, prepared_by_id, tuple(rollback_operations), result)
        return _log_mutation_result(result, operation=proposal.description)

    async def add_note(
        self,
        document_id: int,
        note: str,
        *,
        apply: bool = False,
        interface: InitiatingInterface = InitiatingInterface.MCP,
    ) -> MutationResult:
        if isinstance(document_id, bool) or not isinstance(document_id, int) or document_id < 1:
            raise ValueError("document_id must be a positive integer")
        if not isinstance(note, str) or not note.strip():
            raise ValueError("note must not be blank")
        if len(note) > MAX_NOTE_CHARACTERS:
            raise ValueError(f"note must not exceed {MAX_NOTE_CHARACTERS} characters")
        if apply and not self._settings.write_enabled:
            raise WritesDisabledError(
                "Writes are disabled. Enable them server-side and explicitly set apply=true."
            )

        current = current_metadata(await self._client.get_document(document_id))
        preview = DocumentMutation(
            document_id=document_id,
            before=current,
            after=current,
            changed_fields=("note",),
        )
        if not apply:
            return _log_mutation_result(
                MutationResult(
                    status=MutationStatus.DRY_RUN,
                    dry_run=True,
                    requested_count=1,
                    mutations=(preview,),
                    summary="Dry-run only: one note would be added; Paperless was not changed.",
                ),
                operation="add-document-note",
            )

        audit = AuditRun(
            settings=self._settings,
            client=self._client,
            operation="add-document-note",
            interface=interface,
            force=False,
            proposal={
                "document_id": document_id,
                "note_length": len(note),
                "note_sha256": text_hash(note),
                "note_text_stored": False,
            },
        )
        write_attempted = False
        try:
            before_notes = await self._client.list_document_notes(document_id)
            if any(existing.id is None for existing in before_notes):
                mutation = preview.model_copy(
                    update={
                        "error_code": "note_preflight_failed",
                        "error_message": (
                            "Paperless returned notes without stable IDs; no note was created."
                        ),
                    }
                )
                result = _note_failure_result(audit, mutation, indeterminate=False)
                _record_audit(audit, applied=False, mutation=mutation)
                self._finalize_note_audit(
                    audit,
                    document_id,
                    current,
                    result,
                    outcome="not_attempted",
                )
                return _log_mutation_result(result, operation="add-document-note")

            write_attempted = True
            returned_notes = await self._client.add_document_note(document_id, note)
            before_ids = {existing.id for existing in before_notes}
            new_notes = [
                returned
                for returned in returned_notes
                if returned.id is not None and returned.id not in before_ids
            ]
            verified = (
                len(new_notes) == 1
                and new_notes[0].note == note
                and all(returned.id is not None for returned in returned_notes)
            )
            if not verified:
                mutation = preview.model_copy(
                    update={
                        "error_code": "note_outcome_indeterminate",
                        "error_message": (
                            "Paperless accepted the note request, but the returned notes array "
                            "did not identify exactly one newly created note with the exact text."
                        ),
                    }
                )
                result = _note_failure_result(audit, mutation, indeterminate=True)
                _record_audit(audit, applied=False, mutation=mutation)
                self._finalize_note_audit(
                    audit,
                    document_id,
                    current,
                    result,
                    outcome="indeterminate",
                )
                return _log_mutation_result(result, operation="add-document-note")

            mutation_record = {
                **preview.model_dump(mode="json"),
                "write_performed": True,
                "created_note_id": new_notes[0].id,
                "note_length": len(note),
                "note_sha256": text_hash(note),
            }
            try:
                audit.record_applied(mutation_record)
            except AuditDirectoryError:
                audit.mark_incomplete("applied.jsonl")
            result = MutationResult(
                status=MutationStatus.APPLIED,
                dry_run=False,
                requested_count=1,
                applied_count=1,
                mutations=(preview,),
                summary=(
                    "Added and verified one exact document note. Note creation has no safe "
                    "delete rollback in this release."
                ),
                run_id=audit.run_id,
                rollback_path=None,
            )
            self._finalize_note_audit(
                audit,
                document_id,
                current,
                result,
                outcome="verified",
            )
            return _log_mutation_result(result, operation="add-document-note")
        except AuditDirectoryError:
            raise
        except Exception as exc:
            code, message = _safe_error(exc, phase="note")
            indeterminate = write_attempted
            mutation = preview.model_copy(
                update={
                    "error_code": ("note_outcome_indeterminate" if indeterminate else code),
                    "error_message": (
                        "The note request may have succeeded, but its exact outcome could not "
                        "be verified; inspect Paperless before retrying."
                        if indeterminate
                        else message
                    ),
                }
            )
            result = _note_failure_result(audit, mutation, indeterminate=indeterminate)
            _record_audit(audit, applied=False, mutation=mutation)
            self._finalize_note_audit(
                audit,
                document_id,
                current,
                result,
                outcome="indeterminate" if indeterminate else "not_attempted",
            )
            return _log_mutation_result(result, operation="add-document-note")

    async def _preflight_change(
        self,
        proposed: ProposedDocumentChange,
        *,
        force: bool,
    ) -> tuple[_PreparedChange | None, DocumentMutation, _Outcome]:
        before = current_metadata(await self._client.get_document(proposed.document_id))
        conflicts = conflicting_fields(proposed, before)
        if conflicts and not force:
            return (
                None,
                DocumentMutation(
                    document_id=proposed.document_id,
                    before=before,
                    conflicting_fields=conflicts,
                    error_code="stale_proposal",
                    error_message="The document no longer matches the proposal snapshot.",
                ),
                "conflict",
            )

        requested_target = target_metadata(before, proposed.changes)
        target, tag_names = await self._hierarchy_adjusted_target(before, requested_target)
        protected_error = self._protected_tag_error(proposed, before, target, tag_names)
        if protected_error is not None:
            return (
                None,
                DocumentMutation(
                    document_id=proposed.document_id,
                    before=before,
                    after=target,
                    error_code="protected_tag",
                    error_message=protected_error,
                ),
                "failure",
            )

        preview = DocumentMutation(
            document_id=proposed.document_id,
            before=before,
            after=target,
            changed_fields=changed_fields(before, target),
            conflicting_fields=conflicts if force else (),
        )
        return (
            _PreparedChange(
                proposed=proposed,
                before=before,
                target=target,
                preview=preview,
            ),
            preview,
            "ready",
        )

    async def _apply_prepared(
        self,
        prepared: _PreparedChange,
        *,
        force: bool,
    ) -> tuple[DocumentMutation, RollbackOperation | None, _Outcome]:
        proposed = prepared.proposed
        try:
            fresh = current_metadata(await self._client.get_document(proposed.document_id))
        except Exception as exc:
            return (
                _failure_mutation(
                    proposed,
                    before=prepared.before,
                    after=None,
                    exc=exc,
                    phase="pre_write_read",
                ),
                None,
                "failure",
            )

        changed_since_preflight = compare_states(
            prepared.before,
            fresh,
            tuple(CurrentDocumentMetadata.model_fields),
        )
        if changed_since_preflight and not force:
            return (
                DocumentMutation(
                    document_id=proposed.document_id,
                    before=fresh,
                    conflicting_fields=changed_since_preflight,
                    error_code="stale_during_batch",
                    error_message=(
                        "The document changed after batch preflight; no write was attempted."
                    ),
                ),
                None,
                "conflict",
            )

        try:
            requested_target = target_metadata(fresh, proposed.changes)
            target, tag_names = await self._hierarchy_adjusted_target(fresh, requested_target)
            protected_error = self._protected_tag_error(proposed, fresh, target, tag_names)
        except Exception as exc:
            return (
                _failure_mutation(
                    proposed,
                    before=fresh,
                    after=None,
                    exc=exc,
                    phase="pre_write_policy",
                ),
                None,
                "failure",
            )

        if protected_error is not None:
            return (
                DocumentMutation(
                    document_id=proposed.document_id,
                    before=fresh,
                    after=target,
                    error_code="protected_tag",
                    error_message=protected_error,
                ),
                None,
                "failure",
            )

        fields = changed_fields(fresh, target)
        preview = DocumentMutation(
            document_id=proposed.document_id,
            before=fresh,
            after=target,
            changed_fields=fields,
            conflicting_fields=conflicting_fields(proposed, fresh) if force else (),
        )
        if not fields:
            return preview, None, "noop"

        try:
            await self._client.patch_document(
                proposed.document_id,
                patch_payload(fresh, target, proposed.changes),
            )
            verified = current_metadata(await self._client.get_document(proposed.document_id))
        except Exception as exc:
            recovery, after = await self._recovery_operation(
                proposed.document_id,
                before=fresh,
                target=target,
            )
            code, message = _safe_error(exc, phase="write_or_verification")
            indeterminate = (
                recovery is not None
                and recovery.verification is RollbackOperationVerification.INDETERMINATE
            )
            return (
                DocumentMutation(
                    document_id=proposed.document_id,
                    before=fresh,
                    after=after,
                    changed_fields=fields,
                    error_code="mutation_outcome_indeterminate" if indeterminate else code,
                    error_message=(
                        "The write may have changed Paperless, but recovery state could not be "
                        "read. The audit includes an indeterminate recovery operation."
                        if indeterminate
                        else message
                    ),
                ),
                recovery,
                "indeterminate" if indeterminate else "failure",
            )

        verification_conflicts = compare_states(target, verified, fields)
        rollback_operation = RollbackOperation(
            document_id=proposed.document_id,
            expected_current_state=verified,
            restore_state=fresh,
        )
        if verification_conflicts:
            return (
                DocumentMutation(
                    document_id=proposed.document_id,
                    before=fresh,
                    after=verified,
                    changed_fields=fields,
                    conflicting_fields=verification_conflicts,
                    error_code="verification_failed",
                    error_message="Paperless did not persist the requested metadata exactly.",
                ),
                rollback_operation,
                "failure",
            )
        return (
            DocumentMutation(
                document_id=proposed.document_id,
                before=fresh,
                after=verified,
                changed_fields=fields,
                conflicting_fields=conflicting_fields(proposed, fresh) if force else (),
            ),
            rollback_operation,
            "applied",
        )

    async def _recovery_operation(
        self,
        document_id: int,
        *,
        before: CurrentDocumentMetadata,
        target: CurrentDocumentMetadata,
    ) -> tuple[RollbackOperation | None, CurrentDocumentMetadata | None]:
        try:
            after = current_metadata(await self._client.get_document(document_id))
        except Exception:
            return (
                RollbackOperation(
                    document_id=document_id,
                    expected_current_state=target,
                    restore_state=before,
                    verification=RollbackOperationVerification.INDETERMINATE,
                ),
                None,
            )
        if after == before:
            return None, after
        return (
            RollbackOperation(
                document_id=document_id,
                expected_current_state=after,
                restore_state=before,
            ),
            after,
        )

    async def _hierarchy_adjusted_target(
        self,
        before: CurrentDocumentMetadata,
        requested: CurrentDocumentMetadata,
    ) -> tuple[CurrentDocumentMetadata, dict[int, str]]:
        """Simulate Paperless's ancestor-add and descendant-removal tag algorithm."""
        if before.tag_ids == requested.tag_ids:
            return requested, {}

        cache: dict[int, TaxonomyPayload] = {}

        async def get_tag(tag_id: int) -> TaxonomyPayload:
            if tag_id not in cache:
                value = await self._client.get_taxonomy_item("tags", tag_id)
                if not isinstance(value, TaxonomyPayload):
                    raise UnsupportedPaperlessBehaviorError(
                        "Paperless returned an invalid tag hierarchy response."
                    )
                cache[tag_id] = value
            return cache[tag_id]

        requested_ids = set(requested.tag_ids)
        removed_roots = set(before.tag_ids) - requested_ids
        blocked_ids = set(removed_roots)

        def collect_nested_children(tag: TaxonomyPayload, seen: set[int]) -> None:
            if tag.id in seen:
                raise UnsupportedPaperlessBehaviorError(
                    "Paperless returned a cyclic tag hierarchy."
                )
            next_seen = {*seen, tag.id}
            for child in tag.children:
                cache.setdefault(child.id, child)
                blocked_ids.add(child.id)
                collect_nested_children(child, next_seen)

        for tag_id in sorted(removed_roots):
            collect_nested_children(await get_tag(tag_id), set())

        # Check every currently assigned tag's parent chain too. This fails closed
        # if a server omits a protected descendant from a nested children result.
        for tag_id in sorted(before.tag_ids):
            current = await get_tag(tag_id)
            seen = {tag_id}
            while current.parent is not None:
                parent_id = current.parent
                if parent_id in seen:
                    raise UnsupportedPaperlessBehaviorError(
                        "Paperless returned a cyclic tag hierarchy."
                    )
                if parent_id in removed_roots:
                    blocked_ids.add(tag_id)
                    break
                seen.add(parent_id)
                current = await get_tag(parent_id)

        final_ids = set(requested_ids)
        for tag_id in sorted(requested_ids):
            current = await get_tag(tag_id)
            seen = {tag_id}
            while current.parent is not None:
                parent_id = current.parent
                if parent_id in seen:
                    raise UnsupportedPaperlessBehaviorError(
                        "Paperless returned a cyclic tag hierarchy."
                    )
                final_ids.add(parent_id)
                seen.add(parent_id)
                current = await get_tag(parent_id)

        final_ids.difference_update(blocked_ids)
        names = {tag_id: tag.name for tag_id, tag in cache.items()}
        for tag_id in set(before.tag_ids) - final_ids:
            if tag_id not in names:
                names[tag_id] = (await get_tag(tag_id)).name
        return (
            requested.model_copy(update={"tag_ids": tuple(sorted(final_ids))}),
            names,
        )

    def _protected_tag_error(
        self,
        proposed: ProposedDocumentChange,
        before: CurrentDocumentMetadata,
        target: CurrentDocumentMetadata,
        tag_names: dict[int, str],
    ) -> str | None:
        removed_ids = set(before.tag_ids) - set(target.tag_ids)
        if not removed_ids:
            return None
        protected_casefold = {
            name.casefold()
            for name in effective_protected_tags(self._settings, self._proposals.policy)
        }
        protected_removed = sorted(
            tag_names[tag_id]
            for tag_id in removed_ids
            if tag_id in tag_names and tag_names[tag_id].casefold() in protected_casefold
        )
        allowed_exact = set(proposed.allow_protected_tag_removal)
        blocked = [name for name in protected_removed if name not in allowed_exact]
        if not blocked:
            return None
        return (
            "Protected tags, including hierarchy-cascaded descendants, require an exact-name "
            "removal opt-in: " + ", ".join(blocked)
        )

    def _finalize_audit(
        self,
        audit: AuditRun | None,
        prepared_by_id: dict[int, _PreparedChange],
        rollback_operations: tuple[RollbackOperation, ...],
        result: MutationResult,
    ) -> None:
        if audit is None:
            return
        rollback_record = RollbackRecord(
            source_run_id=audit.run_id,
            operations=rollback_operations,
            app_version=__version__,
            paperless_url=self._settings.base_url,
        )
        audit.finalize(
            before={
                document_id: prepared.before for document_id, prepared in prepared_by_id.items()
            },
            rollback=rollback_record,
            result=result,
        )

    def _finalize_note_audit(
        self,
        audit: AuditRun,
        document_id: int,
        before: CurrentDocumentMetadata,
        result: MutationResult,
        *,
        outcome: str,
    ) -> None:
        rollback: dict[str, Any] = {
            "source_run_id": audit.run_id,
            "operations": [],
            "reversible": False,
            "outcome": outcome,
            "reason": "Note deletion rollback is not supported safely.",
        }
        audit.finalize(before={document_id: before}, rollback=rollback, result=result)


def current_metadata(payload: DocumentPayload) -> CurrentDocumentMetadata:
    return CurrentDocumentMetadata(
        title=payload.title,
        created=payload.created,
        correspondent_id=payload.correspondent,
        document_type_id=payload.document_type,
        storage_path_id=payload.storage_path,
        tag_ids=payload.tags,
        custom_fields={field.field: field.value for field in payload.custom_fields},
        archive_serial_number=payload.archive_serial_number,
        modified=payload.modified,
    )


def conflicting_fields(
    proposed: ProposedDocumentChange,
    current: CurrentDocumentMetadata,
) -> tuple[str, ...]:
    fields = tuple(sorted(proposed.expected_current_state.model_fields_set))
    return compare_states(proposed.expected_current_state, current, fields)


def compare_states(
    expected: CurrentDocumentMetadata,
    current: CurrentDocumentMetadata,
    fields: tuple[str, ...],
) -> tuple[str, ...]:
    return tuple(field for field in fields if getattr(expected, field) != getattr(current, field))


def target_metadata(
    before: CurrentDocumentMetadata,
    changes: ProposedDocumentChanges,
) -> CurrentDocumentMetadata:
    updates: dict[str, Any] = {}
    fields_set = changes.model_fields_set
    for field in (
        "title",
        "created",
        "correspondent_id",
        "document_type_id",
        "storage_path_id",
    ):
        if field in fields_set:
            updates[field] = getattr(changes, field)

    if changes.replace_tag_ids is not None:
        updates["tag_ids"] = changes.replace_tag_ids
    elif changes.add_tag_ids or changes.remove_tag_ids:
        tags = (set(before.tag_ids) | set(changes.add_tag_ids)) - set(changes.remove_tag_ids)
        updates["tag_ids"] = tuple(sorted(tags))

    if changes.replace_custom_fields is not None:
        updates["custom_fields"] = dict(changes.replace_custom_fields)
    elif changes.custom_fields is not None:
        custom_fields = dict(before.custom_fields)
        custom_fields.update(changes.custom_fields)
        updates["custom_fields"] = custom_fields
    return before.model_copy(update=updates)


def changed_fields(
    before: CurrentDocumentMetadata,
    after: CurrentDocumentMetadata,
) -> tuple[str, ...]:
    return tuple(
        field
        for field in (
            "title",
            "created",
            "correspondent_id",
            "document_type_id",
            "storage_path_id",
            "tag_ids",
            "custom_fields",
        )
        if getattr(before, field) != getattr(after, field)
    )


def patch_payload(
    before: CurrentDocumentMetadata,
    target: CurrentDocumentMetadata,
    changes: ProposedDocumentChanges,
) -> dict[str, JsonValue]:
    payload: dict[str, JsonValue] = {}
    fields = changed_fields(before, target)
    wire_names = {
        "correspondent_id": "correspondent",
        "document_type_id": "document_type",
        "storage_path_id": "storage_path",
        "tag_ids": "tags",
    }
    for field in fields:
        value = getattr(target, field)
        name = wire_names.get(field, field)
        if field == "created":
            payload[name] = value.isoformat() if isinstance(value, date) else None
        elif field == "tag_ids":
            payload[name] = list(target.tag_ids)
        elif field == "custom_fields":
            payload[name] = [
                {"field": field_id, "value": field_value}
                for field_id, field_value in sorted(target.custom_fields.items())
            ]
        else:
            payload[name] = value
    return payload


def _preview_result(
    proposal: BatchProposal,
    preflight: dict[int, tuple[DocumentMutation, _Outcome]],
    *,
    interface: InitiatingInterface,
    force: bool,
) -> MutationResult:
    mutations = tuple(preflight[change.document_id][0] for change in proposal.changes)
    conflict_count = sum(
        preflight[change.document_id][1] == "conflict" for change in proposal.changes
    )
    failure_count = sum(
        preflight[change.document_id][1] == "failure" for change in proposal.changes
    )
    rollback_operations = tuple(
        DryRunRollbackOperation(
            document_id=mutation.document_id,
            expected_current_state=mutation.after,
            restore_state=mutation.before,
        )
        for mutation, outcome in preflight.values()
        if outcome == "ready" and mutation.after is not None and mutation.changed_fields
    )
    return MutationResult(
        status=(
            MutationStatus.REJECTED if conflict_count or failure_count else MutationStatus.DRY_RUN
        ),
        dry_run=True,
        requested_count=len(proposal.changes),
        conflict_count=conflict_count,
        failure_count=failure_count,
        mutations=mutations,
        summary=_result_summary(
            apply=False,
            requested=len(proposal.changes),
            applied=0,
            noops=0,
            conflicts=conflict_count,
            failures=failure_count,
            indeterminate=0,
        ),
        audit_preview=DryRunAuditPreview(
            operation=proposal.description,
            interface=interface,
            proposal_id=proposal.proposal_id,
            force=force,
            document_ids=tuple(change.document_id for change in proposal.changes),
            rollback_operations=rollback_operations,
        ),
    )


def _applied_status(
    *,
    applied: int,
    noops: int,
    conflicts: int,
    failures: int,
    indeterminate: int,
) -> MutationStatus:
    if indeterminate:
        return MutationStatus.INDETERMINATE
    if conflicts or failures:
        return MutationStatus.PARTIAL if applied else MutationStatus.REJECTED
    if applied:
        return MutationStatus.APPLIED
    if noops:
        return MutationStatus.NO_OP
    return MutationStatus.REJECTED


def _result_summary(
    *,
    apply: bool,
    requested: int,
    applied: int,
    noops: int,
    conflicts: int,
    failures: int,
    indeterminate: int,
) -> str:
    if not apply and not conflicts and not failures:
        return f"Dry-run only: {requested} document change(s) are ready; Paperless was not changed."
    if not apply:
        return (
            f"Dry-run rejected {conflicts + failures} of {requested} document change(s): "
            f"{conflicts} stale, {failures} invalid."
        )
    if indeterminate:
        return (
            f"Performed {applied} verified write(s), {noops} no-op(s), and encountered "
            f"{indeterminate} indeterminate outcome(s). Inspect and preview the sealed recovery "
            "artifact before any retry."
        )
    return (
        f"Performed {applied} verified write(s) for {requested} requested change(s); "
        f"{noops} required no write, {conflicts} were stale, and {failures} failed."
    )


def _snapshot_metadata(proposed: ProposedDocumentChange) -> CurrentDocumentMetadata:
    return CurrentDocumentMetadata.model_validate(
        proposed.expected_current_state.model_dump(mode="python")
    )


def _safe_error(exc: Exception, *, phase: str) -> tuple[str, str]:
    if isinstance(exc, PaperlessMCPError):
        return exc.code, exc.message
    return (
        "internal_mutation_error",
        f"An unexpected internal error occurred during {phase}; no automatic retry was attempted.",
    )


def _failure_mutation(
    proposed: ProposedDocumentChange,
    *,
    before: CurrentDocumentMetadata | None,
    after: CurrentDocumentMetadata | None,
    exc: Exception,
    phase: str,
) -> DocumentMutation:
    code, message = _safe_error(exc, phase=phase)
    return DocumentMutation(
        document_id=proposed.document_id,
        before=before or _snapshot_metadata(proposed),
        after=after,
        error_code=code,
        error_message=message,
    )


def _record_audit(
    audit: AuditRun | None,
    *,
    applied: bool,
    mutation: DocumentMutation,
) -> None:
    if audit is None:
        return
    try:
        if applied:
            audit.record_applied(mutation)
        else:
            audit.record_failure(mutation)
    except PaperlessMCPError:
        audit.mark_incomplete("result_streams")


def _note_failure_result(
    audit: AuditRun,
    mutation: DocumentMutation,
    *,
    indeterminate: bool,
) -> MutationResult:
    return MutationResult(
        status=MutationStatus.INDETERMINATE if indeterminate else MutationStatus.REJECTED,
        dry_run=False,
        requested_count=1,
        failure_count=1,
        mutations=(mutation,),
        summary=(
            "The note write outcome is indeterminate. It may exist in Paperless; inspect the "
            "document before retrying."
            if indeterminate
            else "The note was not written because preflight failed."
        ),
        run_id=audit.run_id,
        rollback_path=None,
    )


def state_hashes(states: dict[int, CurrentDocumentMetadata]) -> dict[int, str]:
    return {document_id: metadata_hash(state) for document_id, state in states.items()}


def _log_mutation_result(result: MutationResult, *, operation: str) -> MutationResult:
    logger.info(
        "mutation_summary",
        extra={
            "operation": operation,
            "run_id": result.run_id,
            "dry_run": result.dry_run,
            "mutation_count": result.requested_count,
            "applied_count": result.applied_count,
            "conflict_count": result.conflict_count,
            "failure_count": result.failure_count,
        },
    )
    return result

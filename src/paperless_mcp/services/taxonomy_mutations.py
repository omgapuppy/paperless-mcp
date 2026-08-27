"""Guarded create, rename and delete operations against Paperless taxonomy.

Taxonomy mutation is deliberately narrower than document mutation. A tag or
correspondent is referenced by many documents, so the destructive direction is
delete, and a delete that silently detached documents would be unrecoverable
through this server. Every delete therefore refuses while the item is still in
use, and reassignment must happen first through the document mutation path.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence

from paperless_mcp.client import PaperlessClient, TaxonomyEndpoint, TaxonomyPayload
from paperless_mcp.config import Settings
from paperless_mcp.errors import (
    DeletesDisabledError,
    PaperlessMCPError,
    TaxonomyCreationDisabledError,
    WritesDisabledError,
)
from paperless_mcp.models import (
    InitiatingInterface,
    MutationStatus,
    ProposedTaxonomyChange,
    TaxonomyAction,
    TaxonomyKind,
    TaxonomyMutation,
    TaxonomyMutationResult,
)
from paperless_mcp.services.audit import AuditRun

logger = logging.getLogger(__name__)

_ENDPOINTS: dict[TaxonomyKind, TaxonomyEndpoint] = {
    TaxonomyKind.TAG: "tags",
    TaxonomyKind.CORRESPONDENT: "correspondents",
    TaxonomyKind.DOCUMENT_TYPE: "document_types",
    TaxonomyKind.STORAGE_PATH: "storage_paths",
}


def _endpoint(kind: TaxonomyKind) -> TaxonomyEndpoint:
    endpoint = _ENDPOINTS.get(kind)
    if endpoint is None:
        raise PaperlessMCPError(f"Taxonomy mutation does not support {kind}.")
    return endpoint


class TaxonomyMutationService:
    """Preview and guarded application of taxonomy create/rename/delete."""

    def __init__(self, client: PaperlessClient, settings: Settings) -> None:
        self._client = client
        self._settings = settings

    async def execute(
        self,
        changes: Sequence[ProposedTaxonomyChange],
        *,
        apply: bool = False,
        interface: InitiatingInterface = InitiatingInterface.MCP,
    ) -> TaxonomyMutationResult:
        if not changes:
            return TaxonomyMutationResult(
                status=MutationStatus.NO_OP,
                dry_run=not apply,
                requested_count=0,
                summary="No taxonomy changes were requested.",
            )
        if len(changes) > self._settings.max_batch_size:
            raise PaperlessMCPError(
                f"{len(changes)} taxonomy changes exceeds the server cap of "
                f"{self._settings.max_batch_size}."
            )

        prepared: list[tuple[ProposedTaxonomyChange, TaxonomyMutation]] = []
        for change in changes:
            prepared.append((change, await self._preflight(change)))

        blocked = [m for _, m in prepared if m.error_code is not None]
        if not apply:
            return self._result(
                [m for _, m in prepared],
                dry_run=True,
                wrote=False,
                status=MutationStatus.REJECTED if blocked else MutationStatus.DRY_RUN,
            )

        if blocked:
            # Preflight runs over every item before the first write, so a rejected
            # entry stops the whole batch rather than leaving it half-applied.
            return self._result(
                [m for _, m in prepared],
                dry_run=False,
                wrote=False,
                status=MutationStatus.REJECTED,
                note="No changes were applied because at least one was rejected.",
            )

        run = AuditRun(
            settings=self._settings,
            client=self._client,
            operation="taxonomy-mutation",
            interface=interface,
            force=False,
            proposal={
                "changes": [change.model_dump(mode="json") for change, _ in prepared],
            },
        )
        applied: list[TaxonomyMutation] = []
        for change, planned in prepared:
            try:
                applied.append(await self._apply_one(change, planned))
            except PaperlessMCPError as exc:
                failed = planned.model_copy(
                    update={
                        "error_code": type(exc).__name__,
                        "error_message": str(exc),
                    }
                )
                run.record_failure(failed)
                applied.append(failed)
                # Stop at the first write failure rather than continuing over a
                # taxonomy whose state is no longer what preflight observed.
                break
            run.record_applied(applied[-1])
        run_id = run.run_id

        failures = sum(1 for m in applied if m.error_code is not None)
        status = (
            MutationStatus.APPLIED
            if failures == 0
            else MutationStatus.PARTIAL
            if any(m.error_code is None for m in applied)
            else MutationStatus.INDETERMINATE
        )
        result = self._result(applied, dry_run=False, wrote=True, status=status, run_id=run_id)
        run.finalize(
            before={},
            rollback={
                "source_run_id": run_id,
                "note": (
                    "Taxonomy rollback is a recorded plan, not an automated apply. "
                    "Deletes are not reversible: a recreated item receives a new id."
                ),
                "operations": [
                    {
                        "kind": m.kind.value,
                        "item_id": m.item_id,
                        "inverse": m.inverse,
                        "reversible": m.reversible,
                    }
                    for m in applied
                    if m.error_code is None
                ],
            },
            result=result,
        )
        logger.info("taxonomy_mutation_result", extra={"operation": "taxonomy-mutation"})
        return result

    async def _preflight(self, change: ProposedTaxonomyChange) -> TaxonomyMutation:
        """Check enablement, protection, staleness and usage without writing."""
        endpoint = _endpoint(change.kind)
        planned = TaxonomyMutation(
            action=change.action,
            kind=change.kind,
            item_id=change.item_id,
            after_name=change.name,
        )

        if not self._settings.write_enabled:
            return self._reject(planned, WritesDisabledError, "Writes are disabled.")
        if change.action is TaxonomyAction.CREATE and not self._settings.allow_taxonomy_creation:
            return self._reject(
                planned,
                TaxonomyCreationDisabledError,
                "Taxonomy creation is disabled.",
            )
        if change.action is TaxonomyAction.DELETE and not self._settings.delete_enabled:
            return self._reject(planned, DeletesDisabledError, "Deletes are disabled.")

        if change.action is TaxonomyAction.CREATE:
            assert change.name is not None
            planned = planned.model_copy(
                update={"inverse": f"delete the created {change.kind}", "reversible": True}
            )
            if self._is_protected(change.kind, change.name):
                return self._reject(
                    planned,
                    PaperlessMCPError,
                    f"{change.name!r} is a protected tag name.",
                )
            return planned

        current = await self._client.get_taxonomy_item(endpoint, int(change.item_id or 0))
        if not isinstance(current, TaxonomyPayload):
            return self._reject(
                planned, PaperlessMCPError, "That item is not a mutable taxonomy item."
            )

        planned = planned.model_copy(
            update={"before_name": current.name, "document_count": current.document_count}
        )
        if current.name != change.expected_current_name:
            return self._reject(
                planned,
                PaperlessMCPError,
                f"Stale proposal: item {change.item_id} is now named {current.name!r}, "
                f"not {change.expected_current_name!r}.",
            )
        if self._is_protected(change.kind, current.name):
            return self._reject(
                planned,
                PaperlessMCPError,
                f"{current.name!r} is protected and cannot be renamed or deleted.",
            )

        if change.action is TaxonomyAction.DELETE:
            in_use = current.document_count or 0
            if in_use > 0:
                return self._reject(
                    planned,
                    PaperlessMCPError,
                    f"{current.name!r} is still on {in_use} document(s). Reassign them "
                    "before deleting it.",
                )
            if current.children:
                return self._reject(
                    planned,
                    PaperlessMCPError,
                    f"{current.name!r} still has {len(current.children)} child tag(s).",
                )
            # A deleted item cannot be restored with its original id, so the
            # inverse is a recreate that new documents would have to be pointed at.
            return planned.model_copy(
                update={
                    "inverse": f"recreate {current.name!r} (a new id would be issued)",
                    "reversible": False,
                }
            )

        return planned.model_copy(
            update={"inverse": f"rename back to {current.name!r}", "reversible": True}
        )

    async def _apply_one(
        self,
        change: ProposedTaxonomyChange,
        planned: TaxonomyMutation,
    ) -> TaxonomyMutation:
        endpoint = _endpoint(change.kind)
        if change.action is TaxonomyAction.CREATE:
            payload: dict[str, object] = {"name": change.name}
            if change.parent_id is not None:
                payload["parent"] = change.parent_id
            created = await self._client.create_taxonomy_item(endpoint, payload)  # type: ignore[arg-type]
            return planned.model_copy(update={"item_id": created.id, "after_name": created.name})
        if change.action is TaxonomyAction.RENAME:
            updated = await self._client.patch_taxonomy_item(
                endpoint,
                int(change.item_id or 0),
                {"name": change.name},
            )
            return planned.model_copy(update={"after_name": updated.name})
        await self._client.delete_taxonomy_item(endpoint, int(change.item_id or 0))
        return planned.model_copy(update={"after_name": None})

    def _is_protected(self, kind: TaxonomyKind, name: str) -> bool:
        if kind is not TaxonomyKind.TAG:
            return False
        return _casefold(name) in {_casefold(t) for t in self._settings.protected_tags}

    @staticmethod
    def _reject(
        planned: TaxonomyMutation,
        error: type[Exception],
        message: str,
    ) -> TaxonomyMutation:
        return planned.model_copy(update={"error_code": error.__name__, "error_message": message})

    @staticmethod
    def _result(
        mutations: Iterable[TaxonomyMutation],
        *,
        dry_run: bool,
        status: MutationStatus,
        wrote: bool,
        run_id: str | None = None,
        note: str | None = None,
    ) -> TaxonomyMutationResult:
        """Build a result. `wrote` records whether the write phase actually ran.

        A batch blocked at preflight is not a dry run, but nothing was written
        either, so applied_count must stay zero rather than counting the entries
        that merely passed their checks.
        """
        items = tuple(mutations)
        errored = sum(1 for m in items if m.error_code is not None)
        applied = sum(1 for m in items if m.error_code is None) if wrote else 0
        verb = "Previewed" if dry_run else "Applied"
        counted = len(items) if not wrote and dry_run else applied
        summary = f"{verb} {counted} of {len(items)} taxonomy change(s)."
        if errored:
            summary += f" {errored} rejected." if not wrote else f" {errored} failed."
        if note:
            summary += f" {note}"
        return TaxonomyMutationResult(
            status=status,
            dry_run=dry_run,
            requested_count=len(items),
            applied_count=applied,
            rejected_count=0 if wrote else errored,
            failure_count=errored if wrote else 0,
            mutations=items,
            summary=summary,
            run_id=run_id,
        )


def _casefold(value: str) -> str:
    return value.strip().casefold()

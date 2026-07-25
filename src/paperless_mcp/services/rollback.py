"""First-class rollback loading, preview, and guarded application."""

from __future__ import annotations

import json
import stat
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from paperless_mcp.config import Settings
from paperless_mcp.errors import (
    BatchTooLargeError,
    MalformedProposalError,
    RollbackConflictError,
)
from paperless_mcp.models import (
    BatchProposal,
    ExpectedCurrentState,
    InitiatingInterface,
    MutationResult,
    ProposedDocumentChange,
    ProposedDocumentChanges,
    RollbackRecord,
)
from paperless_mcp.services.audit import file_hash, secure_audit_root
from paperless_mcp.services.mutations import MutationService
from paperless_mcp.services.proposals import MAX_PROPOSAL_BYTES


def load_rollback_file(path: Path, settings: Settings) -> RollbackRecord:
    """Load a sealed, integrity-verified rollback artifact from one direct audit run."""
    try:
        audit_root = secure_audit_root(settings.audit_dir, create=False)
        unresolved = path.expanduser().absolute()
        if unresolved.is_symlink():
            raise RollbackConflictError("The rollback path must not be a symbolic link.")
        candidate = unresolved.resolve(strict=True)
        run_directory = candidate.parent
        if (
            run_directory.parent != audit_root
            or candidate.name != "rollback.json"
            or run_directory.is_symlink()
        ):
            raise RollbackConflictError(
                "Rollback files must be named rollback.json in a direct audit run directory."
            )
        candidate_info = candidate.lstat()
        run_info = run_directory.lstat()
        if (
            not stat.S_ISREG(candidate_info.st_mode)
            or stat.S_ISLNK(candidate_info.st_mode)
            or not stat.S_ISDIR(run_info.st_mode)
            or stat.S_ISLNK(run_info.st_mode)
        ):
            raise RollbackConflictError("The rollback path is not a regular file.")
        if run_info.st_mode & 0o222 or candidate_info.st_mode & 0o222:
            raise RollbackConflictError("The audit run is not sealed read-only.")
        if candidate.stat().st_size > MAX_PROPOSAL_BYTES:
            raise MalformedProposalError("The rollback file is too large.")
        manifest = _load_and_verify_manifest(run_directory)
        record = RollbackRecord.model_validate_json(candidate.read_text(encoding="utf-8"))
        if (
            record.source_run_id != run_directory.name
            or manifest.get("run_id") != record.source_run_id
            or manifest.get("paperless_url") != record.paperless_url
            or manifest.get("app_version") != record.app_version
        ):
            raise RollbackConflictError(
                "Rollback provenance does not match its sealed audit manifest."
            )
        return record
    except (RollbackConflictError, MalformedProposalError):
        raise
    except (OSError, UnicodeError, ValidationError, json.JSONDecodeError) as exc:
        raise MalformedProposalError("The rollback file is invalid or not reversible.") from exc


def _load_and_verify_manifest(run_directory: Path) -> dict[str, Any]:
    manifest_path = run_directory / "manifest.json"
    if manifest_path.is_symlink():
        raise RollbackConflictError("The audit manifest must not be a symbolic link.")
    info = manifest_path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o222:
        raise RollbackConflictError("The audit manifest is missing or not sealed read-only.")
    if info.st_size > MAX_PROPOSAL_BYTES:
        raise MalformedProposalError("The audit manifest is too large.")
    value = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or value.get("finalized") is not True:
        raise RollbackConflictError("The audit run did not finalize successfully.")
    artifacts = value.get("artifacts")
    if not isinstance(artifacts, dict):
        raise RollbackConflictError("The audit manifest has no artifact integrity map.")
    required = {
        "run.json",
        "proposal.json",
        "before.json",
        "applied.jsonl",
        "failures.jsonl",
        "rollback.json",
        "summary.md",
    }
    if not required.issubset(artifacts):
        raise RollbackConflictError("The audit manifest is incomplete.")
    for name, integrity in artifacts.items():
        if (
            not isinstance(name, str)
            or Path(name).name != name
            or not isinstance(integrity, dict)
            or not isinstance(integrity.get("sha256"), str)
            or not isinstance(integrity.get("size"), int)
        ):
            raise RollbackConflictError("The audit manifest contains an invalid artifact entry.")
        artifact = run_directory / name
        if artifact.is_symlink():
            raise RollbackConflictError("An audit artifact is a symbolic link.")
        artifact_info = artifact.lstat()
        if not stat.S_ISREG(artifact_info.st_mode) or artifact_info.st_mode & 0o222:
            raise RollbackConflictError("An audit artifact is missing or not sealed read-only.")
        if artifact_info.st_size != integrity["size"] or file_hash(artifact) != integrity["sha256"]:
            raise RollbackConflictError("Audit artifact integrity verification failed.")
    return value


class RollbackService:
    def __init__(self, mutations: MutationService, settings: Settings) -> None:
        self._mutations = mutations
        self._settings = settings

    async def execute(
        self,
        rollback: RollbackRecord,
        *,
        apply: bool = False,
        force: bool = False,
        allow_protected_tag_removal: tuple[str, ...] = (),
    ) -> MutationResult:
        if rollback.paperless_url.rstrip("/") != self._settings.base_url:
            raise RollbackConflictError(
                "The rollback file belongs to a different Paperless instance."
            )
        if not rollback.operations:
            raise RollbackConflictError("This audit run has no safely reversible operations.")
        if len(rollback.operations) > self._settings.max_batch_size:
            raise BatchTooLargeError(len(rollback.operations), self._settings.max_batch_size)

        proposal = BatchProposal(
            description=f"rollback-{rollback.source_run_id}",
            changes=tuple(
                ProposedDocumentChange(
                    document_id=operation.document_id,
                    expected_current_state=ExpectedCurrentState.model_validate(
                        operation.expected_current_state.model_dump()
                    ),
                    changes=ProposedDocumentChanges(
                        title=operation.restore_state.title,
                        created=operation.restore_state.created,
                        correspondent_id=operation.restore_state.correspondent_id,
                        document_type_id=operation.restore_state.document_type_id,
                        storage_path_id=operation.restore_state.storage_path_id,
                        replace_tag_ids=operation.restore_state.tag_ids,
                        replace_custom_fields=operation.restore_state.custom_fields,
                    ),
                    confidence=1.0,
                    reason=f"Restore metadata recorded by audit run {rollback.source_run_id}.",
                    allow_protected_tag_removal=allow_protected_tag_removal,
                )
                for operation in rollback.operations
            ),
        )
        return await self._mutations.execute(
            proposal,
            apply=apply,
            force=force,
            interface=InitiatingInterface.ROLLBACK,
        )

"""Crash-conscious mutation audit runs with sealed integrity manifests."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from datetime import UTC, datetime
from pathlib import Path
from typing import IO, Any
from uuid import uuid4

from pydantic import BaseModel

from paperless_mcp import __version__
from paperless_mcp.client import PaperlessClient
from paperless_mcp.config import Settings
from paperless_mcp.errors import AuditDirectoryError
from paperless_mcp.models import (
    BatchProposal,
    CurrentDocumentMetadata,
    InitiatingInterface,
    MutationResult,
    RollbackRecord,
)

FINAL_FILE_MODE = 0o400
FINAL_DIRECTORY_MODE = 0o500
WORKING_FILE_MODE = 0o600
WORKING_DIRECTORY_MODE = 0o700


def metadata_hash(metadata: CurrentDocumentMetadata) -> str:
    canonical = metadata.model_dump_json(exclude_none=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def text_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(64 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def secure_audit_root(path: Path, *, create: bool) -> Path:
    """Return a non-symlink, owner-private audit root."""
    configured = path.expanduser().absolute()
    try:
        if configured.is_symlink():
            raise AuditDirectoryError("The audit directory must not be a symbolic link.")
        if create:
            configured.mkdir(parents=True, exist_ok=True, mode=WORKING_DIRECTORY_MODE)
        info = configured.lstat()
        if not stat.S_ISDIR(info.st_mode):
            raise AuditDirectoryError("The audit path must be a directory.")
        if stat.S_ISLNK(info.st_mode):
            raise AuditDirectoryError("The audit directory must not be a symbolic link.")
        configured.chmod(WORKING_DIRECTORY_MODE)
        return configured.resolve(strict=True)
    except AuditDirectoryError:
        raise
    except OSError as exc:
        raise AuditDirectoryError("The audit directory is unavailable.") from exc


class AuditRun:
    """One unique run directory, mutable only until its final manifest is sealed."""

    def __init__(
        self,
        *,
        settings: Settings,
        client: PaperlessClient,
        operation: str,
        interface: InitiatingInterface,
        force: bool,
        proposal: BatchProposal | dict[str, Any],
    ) -> None:
        self.run_id = _run_id(operation)
        self._root = secure_audit_root(settings.audit_dir, create=True)
        self.path = self._root / self.run_id
        self._applied: IO[str] | None = None
        self._failures: IO[str] | None = None
        self._closed = False
        self._runtime_errors: list[str] = []
        self._manifest: dict[str, Any] = {
            "schema_version": 1,
            "run_id": self.run_id,
            "created_at": datetime.now(UTC).isoformat(),
            "operation": operation,
            "interface": interface.value,
            "app_version": __version__,
            "paperless_url": settings.base_url,
            "paperless_version": client.version_headers.server_version,
            "dry_run": False,
            "force": force,
        }
        try:
            if self.path.exists() or self.path.is_symlink():
                raise AuditDirectoryError("The generated audit run path already exists.")
            self.path.mkdir(mode=WORKING_DIRECTORY_MODE)
            self.path.chmod(WORKING_DIRECTORY_MODE)
            self._write_once("run.json", self._manifest)
            self._write_once("proposal.json", _jsonable(proposal))
            self._applied = _open_append_only(self.path / "applied.jsonl")
            self._failures = _open_append_only(self.path / "failures.jsonl")
        except (OSError, TypeError, ValueError, AuditDirectoryError) as exc:
            self.close()
            if isinstance(exc, AuditDirectoryError):
                raise
            raise AuditDirectoryError("Could not create the mutation audit run.") from exc

    def record_applied(self, value: BaseModel | dict[str, Any]) -> None:
        self._append(self._applied, value)

    def record_failure(self, value: BaseModel | dict[str, Any]) -> None:
        self._append(self._failures, value)

    def mark_incomplete(self, artifact: str) -> None:
        """Mark an in-memory audit gap so finalization cannot claim completeness."""
        self._runtime_errors.append(artifact)

    def finalize(
        self,
        *,
        before: dict[int, CurrentDocumentMetadata],
        rollback: RollbackRecord | dict[str, Any],
        result: MutationResult,
    ) -> None:
        """Publish final artifacts, an integrity manifest, and seal the run read-only."""
        errors = list(self._runtime_errors)
        try:
            try:
                self._flush_streams()
            except AuditDirectoryError:
                errors.append("result_streams")
            self.close()

            final_artifacts: tuple[tuple[str, object, bool], ...] = (
                (
                    "before.json",
                    {
                        str(document_id): metadata.model_dump(mode="json")
                        for document_id, metadata in sorted(before.items())
                    },
                    False,
                ),
                ("rollback.json", _jsonable(rollback), False),
                (
                    "summary.md",
                    (
                        f"# Audit run {self.run_id}\n\n"
                        f"- Status: {result.status.value}\n"
                        f"- Requested: {result.requested_count}\n"
                        f"- Applied writes: {result.applied_count}\n"
                        f"- No-op: {result.noop_count}\n"
                        f"- Conflicts: {result.conflict_count}\n"
                        f"- Failures: {result.failure_count}\n"
                        f"- Force: {'yes' if self._manifest['force'] else 'no'}\n\n"
                        f"{result.summary}\n"
                    ),
                    True,
                ),
            )
            for name, value, raw_text in final_artifacts:
                try:
                    self._write_once(name, value, raw_text=raw_text)
                except (OSError, TypeError, ValueError, AuditDirectoryError):
                    errors.append(name)

            artifact_integrity: dict[str, dict[str, str | int]] = {}
            for artifact in sorted(self.path.iterdir(), key=lambda candidate: candidate.name):
                if artifact.name == "manifest.json" or artifact.name.startswith("."):
                    continue
                try:
                    info = artifact.lstat()
                    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
                        errors.append(artifact.name)
                        continue
                    artifact_integrity[artifact.name] = {
                        "sha256": file_hash(artifact),
                        "size": info.st_size,
                    }
                except OSError:
                    errors.append(artifact.name)

            manifest = {
                **self._manifest,
                "finalized": not errors,
                "finalized_at": datetime.now(UTC).isoformat(),
                "artifacts": artifact_integrity,
            }
            if errors:
                manifest["finalization_errors"] = sorted(set(errors))
            try:
                self._write_once("manifest.json", manifest)
            except (OSError, TypeError, ValueError, AuditDirectoryError):
                errors.append("manifest.json")
        finally:
            self.close()
            try:
                self._seal()
            except (OSError, AuditDirectoryError):
                errors.append("permissions")

        if errors:
            raise AuditDirectoryError(
                "Could not completely finalize the mutation audit run; "
                "the recoverable artifacts were sealed.",
                details={"run_id": self.run_id, "audit_path": str(self.path)},
            )

    def close(self) -> None:
        if self._closed:
            return
        for stream in (self._applied, self._failures):
            if stream is not None and not stream.closed:
                stream.close()
        self._closed = True

    def _append(self, stream: IO[str] | None, value: BaseModel | dict[str, Any]) -> None:
        if stream is None or stream.closed:
            raise AuditDirectoryError("The mutation audit stream is unavailable.")
        try:
            stream.write(json.dumps(_jsonable(value), sort_keys=True, separators=(",", ":")) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        except (OSError, TypeError, ValueError) as exc:
            raise AuditDirectoryError("Could not append to the mutation audit run.") from exc

    def _flush_streams(self) -> None:
        try:
            for stream in (self._applied, self._failures):
                if stream is not None and not stream.closed:
                    stream.flush()
                    os.fsync(stream.fileno())
        except OSError as exc:
            raise AuditDirectoryError("Could not flush the mutation audit run.") from exc

    def _write_once(
        self,
        name: str,
        value: object,
        *,
        raw_text: bool = False,
    ) -> None:
        target = self.path / name
        if target.exists() or target.is_symlink():
            raise AuditDirectoryError(f"Audit artifact {name} already exists.")
        temporary = self.path / f".{name}.{uuid4().hex}.tmp"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(temporary, flags, WORKING_FILE_MODE)
        try:
            rendered = str(value) if raw_text else json.dumps(value, indent=2, sort_keys=True)
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                descriptor = -1
                stream.write(rendered)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            temporary.replace(target)
            _fsync_directory(self.path)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            if temporary.exists():
                temporary.unlink()

    def _seal(self) -> None:
        if self.path.is_symlink():
            raise AuditDirectoryError("The audit run directory became a symbolic link.")
        for artifact in self.path.iterdir():
            info = artifact.lstat()
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
                raise AuditDirectoryError("The audit run contains an unsafe artifact.")
            artifact.chmod(FINAL_FILE_MODE)
        _fsync_directory(self.path)
        self.path.chmod(FINAL_DIRECTORY_MODE)
        _fsync_directory(self._root)


def _jsonable(value: BaseModel | dict[str, Any] | object) -> object:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    return value


def _open_append_only(path: Path) -> IO[str]:
    flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, WORKING_FILE_MODE)
    return os.fdopen(descriptor, "a", encoding="utf-8")


def _run_id(operation: str) -> str:
    timestamp = datetime.now(UTC).strftime("%Y-%m-%dT%H%M%SZ")
    slug = re.sub(r"[^a-z0-9]+", "-", operation.casefold()).strip("-")[:40] or "mutation"
    return f"{timestamp}-{slug}-{uuid4().hex[:10]}"


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)

"""Human-friendly read-only command-line adapter."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable, Sequence
from datetime import date
from pathlib import Path
from typing import Any

import typer
from pydantic import BaseModel, ValidationError

from paperless_mcp import __version__
from paperless_mcp.application import ApplicationServices, create_services
from paperless_mcp.errors import (
    AuditDirectoryError,
    AuthenticationError,
    ConfigurationError,
    NotFoundError,
    PaperlessConnectionError,
    PaperlessMCPError,
)
from paperless_mcp.logging import configure_logging
from paperless_mcp.models import (
    ContentChunk,
    DocumentDetail,
    DocumentFilters,
    DocumentOrdering,
    DocumentPage,
    InitiatingInterface,
    MissingMetadataField,
    MutationResult,
    TaxonomyItem,
    TaxonomyKind,
    TaxonomySnapshot,
)
from paperless_mcp.services.proposals import load_proposal_file
from paperless_mcp.services.rollback import load_rollback_file

app = typer.Typer(
    name="paperless-mcp",
    help="Safety-focused Paperless-ngx CLI and MCP stdio server.",
    no_args_is_help=True,
    pretty_exceptions_enable=False,
)
documents_app = typer.Typer(help="Inspect document metadata.", no_args_is_help=True)
taxonomy_app = typer.Typer(help="Inspect Paperless taxonomy.", no_args_is_help=True)
proposals_app = typer.Typer(
    help="Validate and explicitly apply proposal files.",
    no_args_is_help=True,
)
rollback_app = typer.Typer(
    help="Preview and explicitly apply rollback files.",
    no_args_is_help=True,
)
app.add_typer(documents_app, name="documents")
app.add_typer(taxonomy_app, name="taxonomy")
app.add_typer(proposals_app, name="proposals")
app.add_typer(rollback_app, name="rollback")
_verbose = False

ORDERING_OPTION = typer.Option(DocumentOrdering.ADDED_DESC)
TAG_ID_OPTION = typer.Option(None, "--tag-id", min=1, help="Tag ID; repeatable.")
TAG_NAME_OPTION = typer.Option(
    None,
    "--tag",
    help="Exact tag name resolved safely to an ID; repeatable.",
)
CREATED_AFTER_OPTION = typer.Option(None, help="Created date on/after YYYY-MM-DD.")
CREATED_BEFORE_OPTION = typer.Option(None, help="Created date on/before YYYY-MM-DD.")
ADDED_AFTER_OPTION = typer.Option(None, help="Added date on/after YYYY-MM-DD.")
ADDED_BEFORE_OPTION = typer.Option(None, help="Added date on/before YYYY-MM-DD.")
MISSING_FIELD_ARGUMENT = typer.Argument(...)
TAXONOMY_KIND_ARGUMENT = typer.Argument(...)
PROPOSAL_PATH_ARGUMENT = typer.Argument(..., exists=True, dir_okay=False, readable=True)
ROLLBACK_PATH_ARGUMENT = typer.Argument(..., exists=True, dir_okay=False, readable=True)
PROTECTED_TAG_REMOVAL_OPTION = typer.Option(
    None,
    "--allow-protected-tag-removal",
    help="Exact protected tag name explicitly approved for removal; repeat as needed.",
)


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(__version__)
        raise typer.Exit()


@app.callback()
def main_callback(
    version: bool = typer.Option(
        False,
        "--version",
        callback=_version_callback,
        is_eager=True,
        help="Show the installed version and exit.",
    ),
    verbose: bool = typer.Option(
        False,
        "--verbose",
        "-v",
        help="Enable redacted debug diagnostics on stderr.",
    ),
) -> None:
    """Run bounded read operations or start the MCP server."""
    global _verbose
    _verbose = verbose


def _exit_code(exc: Exception) -> int:
    if isinstance(exc, (ConfigurationError, ValidationError, ValueError)):
        return 2
    if isinstance(exc, AuthenticationError):
        return 3
    if isinstance(exc, PaperlessConnectionError):
        return 4
    if isinstance(exc, NotFoundError):
        return 5
    return 1


def _safe_error_text(exc: PaperlessMCPError) -> str:
    message = f"Error [{exc.code}]: {exc.message}"
    if isinstance(exc, AuditDirectoryError):
        run_id = exc.details.get("run_id")
        audit_path = exc.details.get("audit_path")
        if isinstance(run_id, str):
            message += f" Recoverable audit run: {run_id}."
        if isinstance(audit_path, str):
            message += f" Path: {audit_path}."
    return message


def _date_option(value: str | None) -> date | None:
    if value is None:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("date filters must use YYYY-MM-DD") from exc


async def _run_operation[ResultT](
    operation: Callable[[ApplicationServices], Awaitable[ResultT]],
) -> ResultT:
    services = create_services()
    if _verbose:
        configure_logging("DEBUG", secrets=(services.settings.api_token,))
    try:
        return await operation(services)
    finally:
        await services.aclose()


def _invoke[ResultT](operation: Callable[[ApplicationServices], Awaitable[ResultT]]) -> ResultT:
    try:
        return asyncio.run(_run_operation(operation))
    except PaperlessMCPError as exc:
        typer.echo(_safe_error_text(exc), err=True)
        raise typer.Exit(_exit_code(exc)) from None
    except (ValidationError, ValueError):
        typer.echo("Error [invalid_input]: One or more arguments are invalid.", err=True)
        raise typer.Exit(2) from None
    except Exception:
        # Avoid transport-level tracebacks or accidental sensitive exception strings.
        typer.echo("Error [internal_error]: The operation could not be completed.", err=True)
        raise typer.Exit(1) from None


def _emit_json(value: BaseModel | Sequence[BaseModel] | dict[str, Any]) -> None:
    if isinstance(value, BaseModel):
        typer.echo(value.model_dump_json(indent=2))
        return
    if isinstance(value, dict):
        typer.echo(json.dumps(value, indent=2, default=str))
        return
    typer.echo(
        json.dumps(
            [item.model_dump(mode="json") for item in value],
            indent=2,
        )
    )


def _emit_documents(page: DocumentPage) -> None:
    typer.echo("ID\tCREATED\tTITLE")
    for document in page.items:
        typer.echo(f"{document.id}\t{document.created or '-'}\t{document.title}")
    pagination = page.pagination
    typer.echo(
        f"\nPage {pagination.page}; showing {len(page.items)} of {pagination.total_count} documents"
    )


def _emit_taxonomy(items: Sequence[TaxonomyItem]) -> None:
    typer.echo("ID\tKIND\tDOCUMENTS\tNAME")
    for item in items:
        typer.echo(
            f"{item.id}\t{item.kind.value}\t"
            f"{item.document_count if item.document_count is not None else '-'}\t{item.name}"
        )


def _emit_mutation(result: MutationResult) -> None:
    typer.echo(result.summary)
    typer.echo(f"Status: {result.status.value}")
    typer.echo(f"Dry-run: {'yes' if result.dry_run else 'NO — PAPERLESS WAS WRITTEN'}")
    if result.run_id is not None:
        typer.echo(f"Audit run: {result.run_id}")
    if result.rollback_path is not None:
        typer.echo(f"Rollback: {result.rollback_path}")


@app.command()
def health(
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON."),
) -> None:
    """Check authenticated Paperless connectivity."""
    result = _invoke(lambda services: services.documents.health())
    if json_output:
        _emit_json(result)
    else:
        typer.echo("Paperless is reachable and authentication succeeded.")
        typer.echo(f"Server version: {result.server_version or 'not reported'}")
        typer.echo(f"API version: {result.api_version or 'not reported'}")


@documents_app.command("list")
def list_documents(
    page: int = typer.Option(1, min=1),
    page_size: int | None = typer.Option(None, min=1),
    ordering: DocumentOrdering = ORDERING_OPTION,
    tag_id: list[int] | None = TAG_ID_OPTION,
    tag: list[str] | None = TAG_NAME_OPTION,
    correspondent_id: int | None = typer.Option(None, min=1),
    document_type_id: int | None = typer.Option(None, min=1),
    storage_path_id: int | None = typer.Option(None, min=1),
    created_after: str | None = CREATED_AFTER_OPTION,
    created_before: str | None = CREATED_BEFORE_OPTION,
    added_after: str | None = ADDED_AFTER_OPTION,
    added_before: str | None = ADDED_BEFORE_OPTION,
    archive_serial_number: int | None = typer.Option(None, min=0),
    original_filename: str | None = typer.Option(
        None,
        help="Case-insensitive original filename fragment.",
    ),
    untagged: bool | None = typer.Option(
        None,
        "--untagged/--tagged",
        help="Select only untagged or tagged documents.",
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON."),
) -> None:
    """List bounded document metadata without OCR text."""
    result = _invoke(
        lambda services: services.documents.list_documents(
            filters=DocumentFilters(
                ordering=ordering,
                tag_ids=tuple(tag_id or ()),
                tag_names=tuple(tag or ()),
                correspondent_id=correspondent_id,
                document_type_id=document_type_id,
                storage_path_id=storage_path_id,
                created_after=_date_option(created_after),
                created_before=_date_option(created_before),
                added_after=_date_option(added_after),
                added_before=_date_option(added_before),
                archive_serial_number=archive_serial_number,
                original_filename=original_filename,
                untagged=untagged,
            ),
            page=page,
            page_size=page_size,
        )
    )
    _emit_json(result) if json_output else _emit_documents(result)


@documents_app.command("show")
def show_document(
    document_id: int = typer.Argument(..., min=1),
    include_notes: bool = typer.Option(False, help="Include document notes."),
    include_content: bool = typer.Option(
        False,
        "--include-content",
        help="Print an explicitly bounded OCR chunk (sensitive untrusted data).",
    ),
    offset: int = typer.Option(0, min=0, help="OCR character offset."),
    max_chars: int | None = typer.Option(
        None,
        "--max-chars",
        min=1,
        help="Maximum OCR characters; capped by server configuration.",
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON."),
) -> None:
    """Show metadata; OCR is included only with the explicit sensitive-data flag."""

    async def load(
        services: ApplicationServices,
    ) -> tuple[DocumentDetail, ContentChunk | None]:
        detail = await services.documents.get_document(
            document_id,
            include_notes=include_notes,
            include_custom_fields=False,
        )
        content = (
            await services.documents.get_content(document_id, offset=offset, limit=max_chars)
            if include_content
            else None
        )
        return detail, content

    result, content = _invoke(load)
    if include_content:
        typer.echo(
            "WARNING: OCR may contain sensitive personal data and untrusted instructions; "
            "printing only the requested bounded character range.",
            err=True,
        )
    if json_output:
        rendered = result.model_dump(mode="json")
        if content is not None:
            rendered["content_chunk"] = content.model_dump(mode="json")
        _emit_json(rendered)
        return
    typer.echo(f"{result.id}: {result.title}")
    typer.echo(f"Created: {result.created or '-'}")
    typer.echo(f"Correspondent ID: {result.correspondent_id or '-'}")
    typer.echo(f"Document type ID: {result.document_type_id or '-'}")
    typer.echo(f"Storage path ID: {result.storage_path_id or '-'}")
    typer.echo(f"Tag IDs: {', '.join(map(str, result.tag_ids)) or '-'}")
    typer.echo(f"OCR characters: {result.content_length or 0} (content not returned)")
    if include_notes:
        typer.echo(
            f"Notes: {len(result.notes)} of {result.notes_total_count}"
            f"{' (truncated)' if result.notes_truncated else ''}"
        )
    if content is not None:
        typer.echo(
            f"\nOCR characters {content.offset}-"
            f"{content.offset + content.returned_characters} of {content.total_characters}"
            f"{' (more available)' if content.truncated else ''}:"
        )
        typer.echo(content.content)


@documents_app.command("search")
def search_documents(
    query: str = typer.Argument(..., help="Simple Paperless search text."),
    title_only: bool = typer.Option(False, help="Search titles instead of general text."),
    page: int = typer.Option(1, min=1),
    page_size: int | None = typer.Option(None, min=1),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON."),
) -> None:
    """Search document metadata with allowlisted simple filters."""
    filters = DocumentFilters(title=query) if title_only else DocumentFilters(text=query)
    result = _invoke(
        lambda services: services.documents.list_documents(
            filters=filters,
            page=page,
            page_size=page_size,
        )
    )
    _emit_json(result) if json_output else _emit_documents(result)


@documents_app.command("missing")
def missing_metadata(
    field: MissingMetadataField = MISSING_FIELD_ARGUMENT,
    page: int = typer.Option(1, min=1),
    page_size: int | None = typer.Option(None, min=1),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON."),
) -> None:
    """List documents missing one supported metadata field."""
    result = _invoke(
        lambda services: services.documents.list_missing_metadata(
            field,
            page=page,
            page_size=page_size,
        )
    )
    _emit_json(result) if json_output else _emit_documents(result)


@taxonomy_app.command("list")
def list_taxonomy(
    kind: TaxonomyKind = TAXONOMY_KIND_ARGUMENT,
    limit: int | None = typer.Option(None, min=1),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON."),
) -> None:
    """List one bounded taxonomy kind."""
    if kind is TaxonomyKind.CUSTOM_FIELD:
        custom_fields = _invoke(lambda services: services.taxonomy.list_custom_fields(limit=limit))
        if json_output:
            _emit_json(custom_fields)
        else:
            typer.echo("ID\tDOCUMENTS\tNAME")
            for item in custom_fields:
                typer.echo(
                    f"{item.id}\t"
                    f"{item.document_count if item.document_count is not None else '-'}\t"
                    f"{item.name}"
                )
        return
    items = _invoke(lambda services: services.taxonomy.list_items(kind, limit=limit))
    _emit_json(items) if json_output else _emit_taxonomy(items)


@taxonomy_app.command("export")
def export_taxonomy(
    limit: int | None = typer.Option(None, min=1),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON."),
) -> None:
    """Export a bounded snapshot of every taxonomy kind."""
    result: TaxonomySnapshot = _invoke(lambda services: services.taxonomy.get_snapshot(limit=limit))
    if json_output:
        _emit_json(result)
        return
    _emit_taxonomy(
        (*result.tags, *result.correspondents, *result.document_types, *result.storage_paths)
    )
    typer.echo(f"\nCustom fields: {len(result.custom_fields)}")


@proposals_app.command("validate")
def validate_proposal(
    proposal_path: Path = PROPOSAL_PATH_ARGUMENT,
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON."),
) -> None:
    """Validate JSON shape, taxonomy references, expected state, and batch cap."""
    result = _invoke(
        lambda services: services.proposals.validate(load_proposal_file(proposal_path))
    )
    if json_output:
        _emit_json(result)
    else:
        typer.echo(result.summary)
        for issue in result.issues:
            typer.echo(f"{issue.severity.value}: {issue.code}: {issue.message}")


@proposals_app.command("apply")
def apply_proposal(
    proposal_path: Path = PROPOSAL_PATH_ARGUMENT,
    apply: bool = typer.Option(
        False,
        "--apply",
        help="Actually write. Omit for the default dry-run.",
    ),
    force: bool = typer.Option(
        False,
        "--force",
        help="Override stale-state conflicts when applying.",
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON."),
) -> None:
    """Preview by default; write only with --apply and server write enablement."""
    result = _invoke(
        lambda services: services.mutations.execute(
            load_proposal_file(proposal_path),
            apply=apply,
            force=force,
            interface=InitiatingInterface.CLI,
        )
    )
    _emit_json(result) if json_output else _emit_mutation(result)


@rollback_app.command("preview")
def preview_rollback(
    rollback_path: Path = ROLLBACK_PATH_ARGUMENT,
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON."),
) -> None:
    """Preview a rollback against freshly read Paperless state."""
    result = _invoke(
        lambda services: services.rollback.execute(
            load_rollback_file(rollback_path, services.settings),
            apply=False,
        )
    )
    _emit_json(result) if json_output else _emit_mutation(result)


@rollback_app.command("apply")
def apply_rollback(
    rollback_path: Path = ROLLBACK_PATH_ARGUMENT,
    apply: bool = typer.Option(
        False,
        "--apply",
        help="Actually restore metadata. Omit for the default dry-run.",
    ),
    force: bool = typer.Option(
        False,
        "--force",
        help="Override stale-state conflicts when applying.",
    ),
    allow_protected_tag_removal: list[str] | None = PROTECTED_TAG_REMOVAL_OPTION,
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON."),
) -> None:
    """Preview by default; restore only with --apply and server write enablement."""
    result = _invoke(
        lambda services: services.rollback.execute(
            load_rollback_file(rollback_path, services.settings),
            apply=apply,
            force=force,
            allow_protected_tag_removal=tuple(allow_protected_tag_removal or ()),
        )
    )
    _emit_json(result) if json_output else _emit_mutation(result)


@app.command("mcp")
def run_mcp() -> None:
    """Start the MCP server over stdio."""
    from paperless_mcp.mcp_server import run

    try:
        run()
    except PaperlessMCPError as exc:
        typer.echo(f"Error [{exc.code}]: {exc.message}", err=True)
        raise typer.Exit(_exit_code(exc)) from None
    except Exception:
        typer.echo("Error [startup_failed]: The MCP server could not start.", err=True)
        raise typer.Exit(1) from None


def main() -> None:
    """Console-script entry point."""
    app()

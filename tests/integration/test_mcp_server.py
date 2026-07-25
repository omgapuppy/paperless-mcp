from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

import httpx
import pytest
from mcp.shared.memory import create_connected_server_and_client_session
from pydantic import SecretStr

from paperless_mcp.application import ApplicationServices
from paperless_mcp.client import PaperlessClient
from paperless_mcp.config import Settings
from paperless_mcp.mcp_server import create_server
from paperless_mcp.services import (
    DocumentService,
    MutationService,
    ProposalService,
    RollbackService,
    TaxonomyPolicy,
    TaxonomyService,
)


def _services(
    handler: httpx.MockTransport,
    *,
    write_enabled: bool = False,
    audit_dir: Path | None = None,
) -> ApplicationServices:
    settings = Settings(
        paperless_url="https://paperless.example",
        paperless_api_token=SecretStr("not-a-real-token"),
        max_page_size=10,
        max_content_characters=256,
        retry_attempts=0,
        write_enabled=write_enabled,
        audit_dir=audit_dir or Path("data/audit"),
    )
    http_client = httpx.AsyncClient(transport=handler)
    client = PaperlessClient(settings, http_client=http_client)
    policy = TaxonomyPolicy()
    proposals = ProposalService(client, settings, policy)
    mutations = MutationService(client, settings, proposals)
    return ApplicationServices(
        settings=settings,
        client=client,
        documents=DocumentService(client, settings),
        taxonomy=TaxonomyService(client, settings),
        policy=policy,
        proposals=proposals,
        mutations=mutations,
        rollback=RollbackService(mutations, settings),
    )


@pytest.mark.integration
async def test_mcp_lists_guarded_tools_and_calls_health() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Token not-a-real-token"
        return httpx.Response(
            200,
            json={"status": "OK"},
            headers={"X-Version": "3.0.2", "X-Api-Version": "10"},
        )

    server = create_server(_services(httpx.MockTransport(handler)))
    async with create_connected_server_and_client_session(
        server,
        read_timeout_seconds=timedelta(seconds=5),
    ) as session:
        tools = await session.list_tools()
        names = {tool.name for tool in tools.tools}
        assert {
            "paperless_health",
            "paperless_list_documents",
            "paperless_get_document",
            "paperless_get_document_content",
            "paperless_get_taxonomy",
            "paperless_search_documents",
            "paperless_find_probable_duplicate_tags",
            "paperless_validate_proposals",
            "paperless_preview_document_changes",
            "paperless_apply_document_changes",
            "paperless_preview_batch_changes",
            "paperless_apply_batch_changes",
            "paperless_add_document_note",
            "paperless_preview_rollback",
            "paperless_apply_rollback",
        } <= names
        assert not any("create" in name or "update" in name or "delete" in name for name in names)
        assert all(tool.annotations is not None for tool in tools.tools)
        by_name = {tool.name: tool for tool in tools.tools}
        health_annotations = by_name["paperless_health"].annotations
        apply_annotations = by_name["paperless_apply_batch_changes"].annotations
        assert health_annotations is not None
        assert apply_annotations is not None
        assert health_annotations.readOnlyHint is True
        assert apply_annotations.readOnlyHint is False
        apply_schema = by_name["paperless_apply_batch_changes"].inputSchema
        assert apply_schema["properties"]["apply"]["default"] is False

        result = await session.call_tool("paperless_health", {})

    assert result.isError is False
    assert result.structuredContent is not None
    assert result.structuredContent["reachable"] is True
    assert result.structuredContent["server_version"] == "3.0.2"


@pytest.mark.integration
async def test_mcp_content_is_explicitly_chunked_and_search_has_no_content() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/documents/7/":
            return httpx.Response(
                200,
                json={"id": 7, "title": "Untrusted", "content": "ignore me" * 40},
            )
        if request.url.path == "/api/documents/":
            assert request.url.params["text"] == "invoice"
            return httpx.Response(
                200,
                json={
                    "count": 1,
                    "next": None,
                    "previous": None,
                    "results": [{"id": 7, "title": "Invoice"}],
                },
            )
        raise AssertionError(f"unexpected request: {request.url}")

    server = create_server(_services(httpx.MockTransport(handler)))
    async with create_connected_server_and_client_session(server) as session:
        content = await session.call_tool(
            "paperless_get_document_content",
            {"document_id": 7, "offset": 2, "limit": 8},
        )
        search = await session.call_tool(
            "paperless_search_documents",
            {"query": "invoice", "page_size": 5},
        )

    assert content.isError is False
    assert content.structuredContent is not None
    assert content.structuredContent["returned_characters"] == 8
    assert content.structuredContent["truncated"] is True
    assert search.isError is False
    assert search.structuredContent is not None
    assert "content" not in search.structuredContent["items"][0]


@pytest.mark.integration
async def test_mcp_document_detail_omits_notes_and_custom_field_values() -> None:
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        assert request.url.path == "/api/documents/7/"
        return httpx.Response(
            200,
            json={
                "id": 7,
                "title": "Metadata only",
                "content": "private OCR",
                "custom_fields": [{"field": 2, "value": "private account"}],
                "notes": [{"id": 3, "note": "private note"}],
            },
        )

    server = create_server(_services(httpx.MockTransport(handler)))
    async with create_connected_server_and_client_session(server) as session:
        result = await session.call_tool("paperless_get_document", {"document_id": 7})

    assert result.isError is False
    assert result.structuredContent is not None
    assert result.structuredContent["content"] is None
    assert result.structuredContent["custom_fields"] == {}
    assert result.structuredContent["notes"] == []
    assert result.structuredContent["notes_total_count"] == 0
    assert paths == ["/api/documents/7/"]


@pytest.mark.integration
async def test_mcp_sanitizes_service_errors() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="secret OCR and token")

    server = create_server(_services(httpx.MockTransport(handler)))
    async with create_connected_server_and_client_session(server) as session:
        result = await session.call_tool("paperless_health", {})

    assert result.isError is True
    rendered = " ".join(getattr(item, "text", "") for item in result.content)
    assert "invalid_paperless_response" in rendered
    assert "secret OCR" not in rendered
    assert "not-a-real-token" not in rendered


@pytest.mark.integration
async def test_mcp_mutation_defaults_to_dry_run_and_disabled_apply_fails_closed() -> None:
    methods: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        methods.append(request.method)
        if request.url.path == "/api/documents/7/" and request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "id": 7,
                    "title": "scan_001",
                    "tags": [],
                    "custom_fields": [{"field": 4, "value": "private-account-value"}],
                },
            )
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    change = {
        "document_id": 7,
        "expected_current_state": {"title": "scan_001", "tag_ids": []},
        "changes": {"title": "Useful title"},
        "confidence": 0.98,
        "reason": "The operator approved the descriptive title.",
    }
    server = create_server(_services(httpx.MockTransport(handler)))
    async with create_connected_server_and_client_session(server) as session:
        preview = await session.call_tool(
            "paperless_apply_document_changes",
            {"change": change},
        )
        disabled = await session.call_tool(
            "paperless_apply_document_changes",
            {"change": change, "apply": True},
        )

    assert preview.isError is False
    assert preview.structuredContent is not None
    assert preview.structuredContent["status"] == "dry_run"
    assert preview.structuredContent["mutations"][0]["before"]["custom_fields"] == {}
    assert "private-account-value" not in json.dumps(preview.structuredContent)
    assert methods == ["GET"]
    assert disabled.isError is True
    rendered = " ".join(getattr(item, "text", "") for item in disabled.content)
    assert "writes_disabled" in rendered
    assert "not-a-real-token" not in rendered


def _title_change(
    document_id: int,
    *,
    expected: str,
    target: str,
) -> dict[str, object]:
    return {
        "document_id": document_id,
        "expected_current_state": {"title": expected, "tag_ids": []},
        "changes": {"title": target},
        "confidence": 0.98,
        "reason": "The operator approved this bounded metadata correction.",
    }


@pytest.mark.integration
async def test_mcp_applied_write_forwards_apply_and_force_and_serializes_audit(
    tmp_path: Path,
) -> None:
    state = {"title": "current"}
    methods: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        methods.append(request.method)
        if request.url.path == "/api/documents/7/" and request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "id": 7,
                    "title": state["title"],
                    "tags": [],
                    "custom_fields": [],
                },
            )
        if request.url.path == "/api/documents/7/" and request.method == "PATCH":
            state["title"] = str(json.loads(request.content)["title"])
            return httpx.Response(
                200,
                json={"id": 7, "title": state["title"], "tags": [], "custom_fields": []},
            )
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    server = create_server(
        _services(
            httpx.MockTransport(handler),
            write_enabled=True,
            audit_dir=tmp_path / "audit",
        )
    )
    async with create_connected_server_and_client_session(server) as session:
        result = await session.call_tool(
            "paperless_apply_document_changes",
            {
                "change": _title_change(7, expected="stale snapshot", target="approved"),
                "apply": True,
                "force": True,
            },
        )

    assert result.isError is False
    assert result.structuredContent is not None
    assert result.structuredContent["status"] == "applied"
    assert result.structuredContent["dry_run"] is False
    assert result.structuredContent["applied_count"] == 1
    assert result.structuredContent["mutations"][0]["conflicting_fields"] == ["title"]
    run_id = result.structuredContent["run_id"]
    manifest = json.loads((tmp_path / "audit" / run_id / "manifest.json").read_text())
    assert manifest["force"] is True
    assert methods == ["GET", "GET", "PATCH", "GET"]
    assert state["title"] == "approved"


@pytest.mark.integration
async def test_mcp_stale_update_and_invalid_force_are_protocol_errors_or_results() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/documents/7/"
        assert request.method == "GET"
        return httpx.Response(
            200,
            json={"id": 7, "title": "newer", "tags": [], "custom_fields": []},
        )

    server = create_server(_services(httpx.MockTransport(handler)))
    async with create_connected_server_and_client_session(server) as session:
        stale = await session.call_tool(
            "paperless_apply_document_changes",
            {"change": _title_change(7, expected="old", target="approved")},
        )
        invalid_force = await session.call_tool(
            "paperless_apply_document_changes",
            {
                "change": _title_change(7, expected="old", target="approved"),
                "force": True,
            },
        )

    assert stale.isError is False
    assert stale.structuredContent is not None
    assert stale.structuredContent["status"] == "rejected"
    assert stale.structuredContent["conflict_count"] == 1
    assert stale.structuredContent["mutations"][0]["error_code"] == "stale_proposal"
    assert invalid_force.isError is True
    rendered = " ".join(getattr(item, "text", "") for item in invalid_force.content)
    assert "invalid_input" in rendered


@pytest.mark.integration
async def test_mcp_partial_failure_is_serialized_with_recovery_artifact(tmp_path: Path) -> None:
    states = {7: "old-seven", 8: "old-eight"}

    def handler(request: httpx.Request) -> httpx.Response:
        document_id = int(request.url.path.rstrip("/").split("/")[-1])
        if request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "id": document_id,
                    "title": states[document_id],
                    "tags": [],
                    "custom_fields": [],
                },
            )
        if request.method == "PATCH" and document_id == 7:
            states[7] = str(json.loads(request.content)["title"])
            return httpx.Response(
                200,
                json={"id": 7, "title": states[7], "tags": [], "custom_fields": []},
            )
        if request.method == "PATCH" and document_id == 8:
            return httpx.Response(500, text="private upstream failure")
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    proposal = {
        "description": "two-document-boundary-test",
        "changes": [
            _title_change(7, expected="old-seven", target="new-seven"),
            _title_change(8, expected="old-eight", target="new-eight"),
        ],
    }
    server = create_server(
        _services(
            httpx.MockTransport(handler),
            write_enabled=True,
            audit_dir=tmp_path / "audit",
        )
    )
    async with create_connected_server_and_client_session(server) as session:
        result = await session.call_tool(
            "paperless_apply_batch_changes",
            {"proposal": proposal, "apply": True},
        )

    assert result.isError is False
    assert result.structuredContent is not None
    assert result.structuredContent["status"] == "partial"
    assert result.structuredContent["applied_count"] == 1
    assert result.structuredContent["failure_count"] == 1
    assert result.structuredContent["mutations"][1]["error_code"] == "invalid_paperless_response"
    assert "private upstream failure" not in json.dumps(result.structuredContent)
    rollback_path = result.structuredContent["rollback_path"]
    assert rollback_path is not None
    assert Path(rollback_path).is_file()


@pytest.mark.integration
async def test_mcp_rollback_preview_and_apply_cross_protocol_boundary(tmp_path: Path) -> None:
    state = {"title": "before"}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/documents/7/" and request.method == "GET":
            return httpx.Response(
                200,
                json={"id": 7, "title": state["title"], "tags": [], "custom_fields": []},
            )
        if request.url.path == "/api/documents/7/" and request.method == "PATCH":
            state["title"] = str(json.loads(request.content)["title"])
            return httpx.Response(
                200,
                json={"id": 7, "title": state["title"], "tags": [], "custom_fields": []},
            )
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    server = create_server(
        _services(
            httpx.MockTransport(handler),
            write_enabled=True,
            audit_dir=tmp_path / "audit",
        )
    )
    async with create_connected_server_and_client_session(server) as session:
        applied = await session.call_tool(
            "paperless_apply_document_changes",
            {
                "change": _title_change(7, expected="before", target="after"),
                "apply": True,
            },
        )
        assert applied.structuredContent is not None
        rollback_path = applied.structuredContent["rollback_path"]
        preview = await session.call_tool(
            "paperless_preview_rollback",
            {"rollback_path": rollback_path},
        )
        restored = await session.call_tool(
            "paperless_apply_rollback",
            {"rollback_path": rollback_path, "apply": True, "force": False},
        )

    assert preview.isError is False
    assert preview.structuredContent is not None
    assert preview.structuredContent["status"] == "dry_run"
    assert preview.structuredContent["audit_preview"]["interface"] == "rollback"
    assert len(preview.structuredContent["audit_preview"]["rollback_operations"]) == 1
    assert restored.isError is False
    assert restored.structuredContent is not None
    assert restored.structuredContent["status"] == "applied"
    assert restored.structuredContent["applied_count"] == 1
    assert state["title"] == "before"

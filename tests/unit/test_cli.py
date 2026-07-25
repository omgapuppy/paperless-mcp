from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Iterator

import httpx
import pytest
from pydantic import SecretStr
from typer.testing import CliRunner

from paperless_mcp.application import ApplicationServices
from paperless_mcp.cli import app
from paperless_mcp.client import PaperlessClient
from paperless_mcp.config import Settings
from paperless_mcp.services import (
    DocumentService,
    MutationService,
    ProposalService,
    RollbackService,
    TaxonomyPolicy,
    TaxonomyService,
)

runner = CliRunner()


@pytest.fixture
def services(monkeypatch: pytest.MonkeyPatch) -> Iterator[ApplicationServices]:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/":
            return httpx.Response(
                200,
                json={"status": "OK"},
                headers={"X-Version": "3.0.2", "X-Api-Version": "10"},
            )
        if request.url.path == "/api/documents/":
            return httpx.Response(
                200,
                json={
                    "count": 1,
                    "next": None,
                    "previous": None,
                    "results": [{"id": 9, "title": "Electricity invoice"}],
                },
            )
        raise AssertionError(f"unexpected request: {request.url}")

    settings = Settings(
        paperless_url="https://paperless.example",
        paperless_api_token=SecretStr("cli-test-token"),
        retry_attempts=0,
    )
    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = PaperlessClient(settings, http_client=http_client)
    policy = TaxonomyPolicy()
    proposals = ProposalService(client, settings, policy)
    mutations = MutationService(client, settings, proposals)
    built = ApplicationServices(
        settings=settings,
        client=client,
        documents=DocumentService(client, settings),
        taxonomy=TaxonomyService(client, settings),
        policy=policy,
        proposals=proposals,
        mutations=mutations,
        rollback=RollbackService(mutations, settings),
    )
    monkeypatch.setattr("paperless_mcp.cli.create_services", lambda: built)
    yield built


def test_cli_health_json(services: ApplicationServices) -> None:
    result = runner.invoke(app, ["health", "--json"])

    assert result.exit_code == 0
    assert '"reachable": true' in result.stdout
    assert '"server_version": "3.0.2"' in result.stdout
    assert "cli-test-token" not in result.stdout


def test_cli_search_human_output(services: ApplicationServices) -> None:
    result = runner.invoke(app, ["documents", "search", "invoice"])

    assert result.exit_code == 0
    assert "ID\tCREATED\tTITLE" in result.stdout
    assert "9\t-\tElectricity invoice" in result.stdout


def test_cli_configuration_error_never_echoes_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PAPERLESS_URL", "not-a-url")
    monkeypatch.setenv("PAPERLESS_API_TOKEN", "do-not-print-this")

    result = runner.invoke(app, ["health"])

    assert result.exit_code == 2
    assert "configuration_error" in result.stderr
    assert "do-not-print-this" not in result.stdout
    assert "do-not-print-this" not in result.stderr
    assert result.exception is not None
    assert "Traceback" not in result.stderr


def test_module_help_starts_without_configuration() -> None:
    environment = os.environ.copy()
    environment.pop("PAPERLESS_URL", None)
    environment.pop("PAPERLESS_API_TOKEN", None)
    completed = subprocess.run(
        [sys.executable, "-m", "paperless_mcp", "--help"],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )

    assert completed.returncode == 0
    assert "documents" in completed.stdout
    assert "mcp" in completed.stdout
    assert "Traceback" not in completed.stderr

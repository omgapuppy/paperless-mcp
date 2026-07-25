"""Application service construction shared by transport entry points."""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import ValidationError

from paperless_mcp.client import PaperlessClient
from paperless_mcp.config import Settings
from paperless_mcp.errors import ConfigurationError
from paperless_mcp.logging import configure_logging
from paperless_mcp.services import (
    DocumentService,
    MutationService,
    ProposalService,
    RollbackService,
    TaxonomyPolicy,
    TaxonomyService,
)
from paperless_mcp.services.proposals import load_taxonomy_policy


@dataclass(frozen=True, slots=True)
class ApplicationServices:
    """The transport-neutral services available to CLI and MCP adapters."""

    settings: Settings
    client: PaperlessClient
    documents: DocumentService
    taxonomy: TaxonomyService
    policy: TaxonomyPolicy
    proposals: ProposalService
    mutations: MutationService
    rollback: RollbackService

    async def aclose(self) -> None:
        """Release owned HTTP resources."""
        await self.client.aclose()


def create_services(settings: Settings | None = None) -> ApplicationServices:
    """Build production services while translating configuration errors safely."""
    try:
        active_settings = settings or Settings()
    except ValidationError as exc:
        # Pydantic's rendered validation errors may include raw input values, including
        # credentials. Never let them cross a user-facing transport boundary.
        raise ConfigurationError(
            "Configuration is invalid. Check PAPERLESS_URL and the API token settings."
        ) from exc

    client = PaperlessClient(active_settings)
    configure_logging(
        active_settings.log_level,
        secrets=(active_settings.api_token,),
    )
    policy = load_taxonomy_policy(active_settings)
    proposals = ProposalService(client, active_settings, policy)
    mutations = MutationService(client, active_settings, proposals)
    return ApplicationServices(
        settings=active_settings,
        client=client,
        documents=DocumentService(client, active_settings),
        taxonomy=TaxonomyService(client, active_settings),
        policy=policy,
        proposals=proposals,
        mutations=mutations,
        rollback=RollbackService(mutations, active_settings),
    )

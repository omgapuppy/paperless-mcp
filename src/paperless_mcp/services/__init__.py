"""Transport-neutral Paperless application services."""

from paperless_mcp.services.documents import DocumentService
from paperless_mcp.services.mutations import MutationService
from paperless_mcp.services.proposals import ProposalService, TaxonomyPolicy
from paperless_mcp.services.rollback import RollbackService
from paperless_mcp.services.taxonomy import TaxonomyService

__all__ = [
    "DocumentService",
    "MutationService",
    "ProposalService",
    "RollbackService",
    "TaxonomyPolicy",
    "TaxonomyService",
]

# Design notes and verified integration facts

This document records the external contracts that shape the implementation. The initial research
baseline, checked on 2026-07-25, is Paperless-ngx 3.0.2 API schema version 10 and MCP Python SDK
1.28.1. Integration changes must re-check current primary sources rather than relying on this
snapshot.

## Primary sources

### Paperless-ngx

- REST API overview and authentication:
  <https://docs.paperless-ngx.com/api/>
- Generated API schema/documentation:
  <https://docs.paperless-ngx.com/api/#api-schema>
- Paperless serves its generated OpenAPI schema from `/api/schema/` and its viewer from
  `/api/schema/view/`; see the official API-schema documentation above.
- Release used for the initial compatibility baseline:
  <https://github.com/paperless-ngx/paperless-ngx/releases/tag/v3.0.2>

Paperless accepts token authentication using `Authorization: Token <token>`. Its REST collections
use paginated responses and expose filtering parameters defined per endpoint. Document list
responses and document detail/content access are kept separate in this project so broad searches
cannot accidentally return large OCR bodies.

Paperless 3.0.2 does not document an ETag/`If-Match` conditional update contract for document
PATCH operations. The mutation design therefore uses a canonical snapshot of every proposal-
relevant field, fetched and compared immediately before a PATCH. A mismatch rejects the proposal
and reports exact conflicting fields. This is optimistic conflict detection, not a server-side
atomic compare-and-swap; a small race remains between the final GET and PATCH and is documented as
a known limitation. We do not claim stronger concurrency semantics than Paperless provides.

No generic endpoint passthrough will be exposed. Filters and mutation payloads are allowlisted and
typed from the verified schema.

Document PATCH collection semantics were verified against the v3.0.2 serializer and tests:

- [document serializer update behavior](https://github.com/paperless-ngx/paperless-ngx/blob/v3.0.2/src/documents/serialisers.py#L1048-L1270)
- [tag hierarchy PATCH test](https://github.com/paperless-ngx/paperless-ngx/blob/v3.0.2/src/documents/tests/test_tag_hierarchy.py#L37-L65)
- [custom-field replacement test](https://github.com/paperless-ngx/paperless-ngx/blob/v3.0.2/src/documents/tests/test_api_custom_fields.py#L595-L661)

When `tags` or `custom_fields` is present, Paperless treats the supplied collection as the desired
complete value. The service therefore starts from a fresh canonical document, computes the full
replacement, and never sends proposal deltas directly. It re-reads and verifies requested fields
after PATCH.

For nested tags, the serializer adds ancestors of requested tags, then removes every explicitly
removed tag and all of its descendants. Mutation preflight mirrors that ordering so removing a
parent cannot silently remove a protected child that remains in the caller's requested list.

The dedicated notes action is
[`POST /api/documents/{id}/notes/`](https://github.com/paperless-ngx/paperless-ngx/blob/v3.0.2/src/documents/views.py#L1673-L1751)
with `{"note": "..."}`. In v3.0.2 it returns HTTP 200 and a notes array. Note POST and document
PATCH are never retried automatically; Paperless documents neither an idempotency key nor a
conditional mutation contract.

### Model Context Protocol

- Python SDK repository and README:
  <https://github.com/modelcontextprotocol/python-sdk/tree/v1.28.1>
- SDK 1.28.1 release:
  <https://github.com/modelcontextprotocol/python-sdk/releases/tag/v1.28.1>
- MCP specification:
  <https://modelcontextprotocol.io/specification/2025-11-25>

The project uses the maintained high-level `FastMCP` API from MCP Python SDK 1.x and constrains the
dependency to `mcp>=1.28.1,<2`. Version 2 has a different development line, so adoption requires a
deliberate compatibility review; this release targets the current stable 2025-11-25 protocol
rather than the breaking 2026-07-28 release candidate. Stdio is the initial transport: it needs no listening port and
fits local Codex process launching. Tool handlers remain thin adapters over shared services.

MCP payloads have server-side page, batch, and character caps. Document content is retrieved only
by a dedicated, chunked text tool; binary documents are never base64-encoded into tool results.
Protocol-boundary tests use the SDK's official
`mcp.shared.memory.create_connected_server_and_client_session` in-memory helper so initialization,
tool discovery, annotations, JSON schemas, structured results, and error signaling are exercised
without relying on private direct handler calls.

### OpenAI Codex

- MCP configuration:
  <https://developers.openai.com/codex/mcp/>
- Skills:
  <https://developers.openai.com/codex/skills/>
- Repository instructions with `AGENTS.md`:
  <https://developers.openai.com/codex/guides/agents-md/>
- Codex configuration reference:
  <https://developers.openai.com/codex/config-reference/>

Codex configures local MCP servers in `config.toml` under `[mcp_servers.<server-name>]`; STDIO
servers use a command, optional arguments, and an explicit environment allowlist. Repository-local
skills live below `.agents/skills/<skill-name>/SKILL.md`, and root `AGENTS.md` provides project
instructions for Codex sessions. Examples in this repository must remain aligned with those
current official formats.

## Architectural decisions

1. **One shared application layer.** The CLI and MCP server are transport adapters over the same
   services, typed client, configuration, and models.
2. **Fail closed.** Reading requires valid URL/token configuration. Writes, deletion, and taxonomy
   creation are separate capabilities and all default off. Policy YAML may restrict but never
   elevate environment capabilities.
3. **Secrets stay at the HTTP boundary.** Configuration stores the token as `SecretStr`, supports
   `PAPERLESS_API_TOKEN_FILE`, and exposes only a redacted summary. Authorization headers are
   redacted independently.
4. **Explicit mutation intent.** Preview/dry-run is the default. Both server write enablement and
   an explicit `apply=true` are required. Batch size is checked server-side.
5. **Untrusted document data.** OCR, filenames, titles, email fields, notes, QR-derived text, and
   custom fields are data only. They can never select tools, override policy, or request secrets.
6. **Sealed audit runs.** Applied operations create a unique private directory, safely flush
   proposal, before state, line-oriented results, rollback plan, and summary, then publish an
   integrity manifest and seal files/directories read-only. OCR is excluded by default.
7. **Apache-2.0 license.** It is permissive while also providing an explicit patent grant and
   contribution patent protection, useful for an integration-oriented open-source project.

## Versioning and compatibility

- Python 3.12 is the minimum supported runtime.
- MCP tool names and JSON schemas are public interfaces. Compatible additions are preferred;
  removals or semantic changes require release notes and a migration path.
- Paperless API behavior is isolated in the typed client to keep service policy independent from
  wire-format changes.

## Read API implementation contract

The typed client uses the documented token header and pins every request to API version 10 with
`Accept: application/json; version=10`. It discovers `X-Api-Version` and `X-Version` from an
authenticated `GET /api/` response; either header may be absent on older or proxied deployments,
so absence is reported rather than guessed. Base URLs may include a path prefix. Endpoint paths
always include the trailing slash expected by Django REST Framework.

Read operations use only these verified API resources:

- `GET /api/` for authenticated connectivity and version headers.
- `GET /api/documents/` with a typed allowlist of simple `text`, `title_search`, exact taxonomy,
  created/added date ranges, archive serial number, original filename, tagged state,
  missing-taxonomy, ordering, page, and page-size filters. Exact tag names are first resolved
  through `GET /api/tags/?name__iexact=...` and must match exactly one returned tag; no raw query
  expression or arbitrary parameter map crosses the service boundary.
- `GET /api/documents/{id}/` for validated document metadata and explicitly requested OCR text.
- `GET /api/documents/{id}/notes/` for notes.
- `GET /api/tags/`, `/api/correspondents/`, `/api/document_types/`, `/api/storage_paths/`, and
  `/api/custom_fields/` for taxonomy reads.

Document lists send Paperless's documented `fields` parameter with a fixed metadata-only
allowlist; `content` is omitted from the HTTP response as well as from the returned schema.
Detail results never expose OCR text. MCP detail also omits custom-field values and notes by
default; explicit note reads are bounded by configured count and per-note character caps, and
custom-field definition `extra_data` is omitted from MCP taxonomy snapshots. The dedicated content workflow fetches the detail resource
but returns only a caller-selected `offset`/`limit` range bounded by
`PAPERLESS_MCP_MAX_CONTENT_CHARACTERS`; OCR remains inert untrusted data. Pagination links are
resolved only when their scheme, host, effective port, and configured `/api/` path prefix match
the configured Paperless deployment, preventing authorization headers from being sent to a
server-selected external URL. Every pagination workflow tracks canonical request URLs, rejects
cycles or malformed empty links, and has a request ceiling derived from the requested item limit
(with a hard cap of 1,000 requests).

Retries are limited to idempotent `GET` requests returning 429, 502, 503, or 504, or failing with
a transient connect, pool, or timeout transport error. TLS/certificate failures and non-GET
requests are never retried. Delays honor a valid `Retry-After` value or use capped exponential
backoff with jitter. Response bodies are excluded from translated errors so tokens, OCR, and
sensitive Paperless payloads cannot be echoed at transport boundaries.

Dry-run mutation results are intentionally stateless and include a complete audit-shaped preview:
operation, initiating interface, proposal ID, document IDs, force state, before/after mutations,
and the rollback operations that would be generated. No dry-run audit directory is written.
Applied audit finalization failures preserve the safe run ID and local audit path at CLI/MCP
boundaries so recoverable sealed artifacts can be located without exposing document content.

Diagnostics use one JSON object per line on stderr. HTTP request logs contain only operation/path,
status, duration, and retry count; mutation summaries contain only run/status counts. Query
values, headers, tokens, OCR, notes, response bodies, and custom-field values are never logged.

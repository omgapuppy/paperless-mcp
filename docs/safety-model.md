# Mutation safety model

Paperless MCP is read-only unless `PAPERLESS_MCP_WRITE_ENABLED=true`. Even then, every mutation
call defaults to a dry-run and a write occurs only when the caller also supplies `apply=true`
(`--apply` in the CLI). Deletion and taxonomy creation are separate capabilities; this release
exposes neither operation.

## Proposal checks

All transports call the same proposal and mutation services. Before a write, the server:

1. validates the strict proposal schema and confidence range;
2. enforces `PAPERLESS_MCP_MAX_BATCH_SIZE`;
3. resolves every referenced taxonomy ID through Paperless;
4. freshly reads each document and compares every explicitly captured expected-state field;
5. reports exact stale fields instead of overwriting them;
6. calculates tag and custom-field replacement payloads from the fresh state;
7. simulates Paperless's nested-tag removal cascade and prevents removal of configured protected
   tags (including descendants) unless the proposal names each approved tag exactly; and
8. reads the document again after PATCH and verifies every requested field.

`force=true` is rejected for dry-runs. During an applied operation it can bypass only stale-state
comparison, not write enablement, taxonomy checks, batch limits, or protected-tag checks. Forced
runs are marked in the immutable audit manifest.

## Partial batches and retries

Paperless does not provide a documented conditional PATCH or atomic batch update. The service
therefore preflights every item before the first PATCH; any preflight failure aborts the whole
batch without a write. After writes begin, changes are sequential. Every verified write is
flushed to `applied.jsonl`; every conflict or failure is flushed to `failures.jsonl`. The rollback
plan distinguishes verified recovery operations from indeterminate operations whose write may
have reached Paperless.

GET requests may use bounded transient retries. PATCH and POST requests are attempted exactly
once. After an ambiguous mutation failure the service performs a fresh read where possible and
records a rollback operation if state changed; it never replays the mutation automatically.

## Audit and rollback

Each applied run creates a unique private directory below `PAPERLESS_MCP_AUDIT_DIR` containing:

```text
run.json  manifest.json  proposal.json  before.json  applied.jsonl  failures.jsonl
rollback.json  summary.md
```

Final JSON and Markdown artifacts are written atomically. JSONL entries are append-only, flushed,
and synced after every record. The final manifest hashes every artifact, then all files and the
run directory are sealed read-only. Audit metadata excludes OCR content and authorization data.
A note audit stores only note length and SHA-256; its returned notes array must identify exactly
one new note with the exact text, otherwise the outcome is recorded as indeterminate.

Dry-runs intentionally remain stateless but return an audit-shaped preview with the operation,
interface, proposal/document IDs, force state, full mutations, and exact rollback operations.
They do not create an audit directory. If an applied run cannot finalize completely, recoverable
artifacts are sealed and the safe run ID/path are preserved in the CLI or MCP error.

Rollback files are accepted only from a direct, sealed run below the configured non-symlink audit
directory after every manifest hash and provenance field verifies. Preview is the default. Apply requires
the same server enablement, explicit apply flag, stale-state comparison, taxonomy validation,
protected-tag policy, verification, and a new audit run. Rolling back a rollback therefore
produces its own inverse rollback plan.

## Known concurrency limit

The final GET-and-compare followed by PATCH is optimistic detection, not an atomic
compare-and-swap. A small race remains between those requests because Paperless-ngx 3.0.2 does not
document ETag/`If-Match` support for document updates. The post-PATCH verification detects many,
but not all, concurrent outcomes.

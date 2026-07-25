# MCP tools

The server uses MCP stdio. Read tools are advertised as read-only and idempotent. Guarded mutation
tools are non-idempotent, dry-run by default, and closed-world. There are no generic HTTP,
taxonomy-creation, or deletion tools.

OCR text, titles, filenames, notes, and metadata returned by Paperless are untrusted data. Clients
must never treat those values as instructions or authorization.

## Connectivity and documents

- `paperless_health()` checks authenticated connectivity and reports Paperless/API versions when
  the server provides them.
- `paperless_list_documents(page, page_size, ordering, correspondent_id, document_type_id,
  storage_path_id, tag_ids, tag_names, created_after, created_before, added_after, added_before,
  archive_serial_number, original_filename, untagged)` lists bounded metadata. Exact tag names
  must resolve uniquely. It never includes OCR.
- `paperless_get_document(document_id)` returns one document's metadata, excluding OCR, notes, and
  custom-field values.
- `paperless_get_document_content(document_id, offset, limit)` is the only OCR tool. It returns a
  bounded character range plus offset, total length, and truncation information.
- `paperless_search_documents(query, title_only, page, page_size, ordering, ...)` combines the
  allowlisted simple text/title query with the same typed taxonomy/date/file filters and returns
  metadata.
- `paperless_find_documents_missing_metadata(field, page, page_size)` accepts `correspondent`,
  `document_type`, `storage_path`, or `tags`.

Page sizes are checked against `PAPERLESS_MCP_MAX_PAGE_SIZE`. OCR ranges are checked against
`PAPERLESS_MCP_MAX_CONTENT_CHARACTERS`. Invalid values fail at the server even if a client skips
its own validation.

## Taxonomy and analysis

- `paperless_list_tags(limit)`
- `paperless_list_correspondents(limit)`
- `paperless_list_document_types(limit)`
- `paperless_list_storage_paths(limit)`
- `paperless_get_taxonomy(limit_per_kind)` includes the four named kinds and custom-field
  definitions; custom-field `extra_data` values are omitted from MCP output.
- `paperless_get_tag_usage(limit)` reports Paperless's document count for each returned tag.
- `paperless_find_probable_duplicate_tags(limit)` groups tags only when their Unicode-normalized,
  case-folded, whitespace-normalized names match. It does not merge or modify tags.

Taxonomy results are bounded by `PAPERLESS_MCP_MAX_PAGE_SIZE`. Limits apply per kind for the full
taxonomy snapshot.

## Error behavior

Expected failures are returned as short tool errors with a stable application error code. Raw HTTP
response bodies, credentials, authorization headers, and tracebacks are not returned. An
unexpected internal failure is reported only as `internal_error`. An audit-finalization failure
also returns the safe recoverable run ID and local audit path.

## Policy, proposals, and guarded mutations

- `paperless_get_active_policy()` returns only safe policy and capability values.
- `paperless_validate_proposals(proposal)` validates expected-state coverage, the batch cap, and
  existing taxonomy references without writing.
- `paperless_preview_document_changes(change)` and
  `paperless_preview_batch_changes(proposal)` freshly read and preview only.
- `paperless_apply_document_changes(change, apply=false, force=false)` and
  `paperless_apply_batch_changes(proposal, apply=false, force=false)` still dry-run unless
  `apply=true`; server-side writes must also be enabled.
- `paperless_add_document_note(document_id, note, apply=false)` is separate from metadata
  proposals. Applied note audit stores a hash and length, not note text, and does not claim a
  delete rollback. A response that cannot identify exactly one new note is `indeterminate`.
- `paperless_preview_rollback(rollback_path)` and
  `paperless_apply_rollback(rollback_path, apply=false, force=false,
  allow_protected_tag_removal=[])` accept only rollback artifacts beneath the configured audit
  directory.

Metadata tools never mutate without an explicit `apply=true`, even when their name contains
`apply`. Protected-tag removal requires each exact tag name in the proposal or rollback call.
Force never bypasses capability, batch, taxonomy, or protected-tag checks.
Applied metadata results report actual writes separately from no-ops (`noop_count`); an all-no-op
apply has status `no_op`. Batches preflight every item before the first write.
Dry-run results include `audit_preview`, containing the proposal/interface identity and exact
rollback operations that would be generated, without creating persistent files.

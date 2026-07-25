# CLI and MCP operations

## Configure

Set `PAPERLESS_URL` and one token source:

```sh
export PAPERLESS_URL=https://paperless.example.test
export PAPERLESS_API_TOKEN=replace-with-a-runtime-secret
```

For containers, prefer `PAPERLESS_API_TOKEN_FILE` and a read-only mounted secret. Keep `.env`,
secret, proposal, and future audit files outside version control.

## CLI

Use `paperless-mcp --help` and the nested command help for the complete option list. The principal
read workflows are:

```sh
paperless-mcp health
paperless-mcp documents list
paperless-mcp documents list --tag Inbox --created-after 2026-01-01
paperless-mcp documents show 42
paperless-mcp documents show 42 --include-content --offset 0 --max-chars 2000
paperless-mcp documents search invoice
paperless-mcp documents missing tags
paperless-mcp taxonomy list correspondent
paperless-mcp taxonomy export --json
```

Human-readable output is the default. Add `--json` after a leaf command for machine-readable
output. Listing is always metadata-only. `--include-content` is an explicit sensitive-data
operation: the CLI warns on stderr and prints only the configured offset/character-bounded OCR
chunk. Notes are bounded by count and characters; custom-field values are omitted by default.
Exit codes are:

- `0`: success
- `1`: unexpected or other application failure
- `2`: configuration or input error
- `3`: authentication rejected
- `4`: Paperless connection or TLS failure
- `5`: resource not found

The CLI prints safe error summaries to stderr and suppresses tracebacks. `--verbose` enables
structured debug diagnostics on stderr. Redaction remains active, and request parameters,
credentials, OCR, notes, raw HTTP bodies, and custom-field values are excluded at the HTTP and
transport boundaries.

## Proposals and rollback

Validate a proposal before previewing it:

```sh
paperless-mcp proposals validate proposal.json
paperless-mcp proposals apply proposal.json --json
```

The second command is still a dry-run. To apply it, the server environment and call must agree:

```sh
export PAPERLESS_MCP_WRITE_ENABLED=true
paperless-mcp proposals apply proposal.json --apply
```

Dry-run results include the complete rollback operations that would be generated, but deliberately
write no audit directory. Applied operations create and seal the durable audit run.

An applied or partially applied batch prints a unique audit run and rollback path. Preview the
rollback against fresh state before applying it:

```sh
paperless-mcp rollback preview data/audit/<run-id>/rollback.json
paperless-mcp rollback apply data/audit/<run-id>/rollback.json --apply
```

`--force` is accepted only together with `--apply` and is recorded in the audit manifest. It
bypasses stale-state conflicts only. To remove a protected tag during rollback, repeat
`--allow-protected-tag-removal "Exact Tag Name"` for each explicitly approved name.

Batch application preflights every item before the first write, then is sequential because
Paperless has no atomic batch endpoint. A partial or indeterminate result records every outcome,
and its rollback includes verified and possibly changed subsets. Completed runs are sealed
read-only with a manifest hash for every artifact; rollback loading verifies those hashes. Keep
the audit directory on durable operator-controlled storage.

## MCP stdio

Start the dedicated process with:

```sh
paperless-mcp-server
```

or equivalently:

```sh
paperless-mcp mcp
```

Stdio MCP uses stdin/stdout for protocol messages, so do not wrap the process with commands that
print banners to stdout. Diagnostic logging belongs on stderr. Keep stdin open when running the
container (`docker run --rm -i ...`). There is no listening port or HTTP healthcheck; use
`paperless_health` after MCP initialization to verify the authenticated upstream connection.

The Codex examples inherit only explicitly named environment variables. Docker receives variable
names through `--env NAME`, not secret values in command-line arguments.

## Troubleshooting

- A configuration error before MCP initialization usually means `PAPERLESS_URL` or the token
  source is absent or invalid.
- An authentication failure means Paperless returned 401/403; rotate or correct the token without
  pasting it into logs.
- A TLS verification failure should be fixed by installing/trusting the correct CA. Disabling
  verification is available for exceptional local testing but is not recommended.
- A page/content limit error is a server-side safety bound. Request smaller pages or OCR chunks
  instead of raising the global limit without review.

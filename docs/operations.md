# CLI and MCP operations

## Configure

Set `PAPERLESS_URL` and choose exactly one token source:

```sh
export PAPERLESS_URL=https://paperless.example.test
export PAPERLESS_API_TOKEN=replace-with-a-runtime-secret
```

Keep `.env`, secret, proposal, and audit files outside version control. Never put a token value
in Codex configuration, Docker arguments, an image, or a committed Compose file.

## Local Docker MCP

The published image runs the stdio MCP server locally:

```text
Codex -> local Docker container -> Paperless-ngx REST API
```

It exposes no port. `docker run -i` keeps the protocol input stream open and `--rm` removes the
stopped container. A persistent host mount is still required for applied-change audit and rollback
artifacts because the rest of the container filesystem is deliberately read-only.

Create the audit location before starting Codex:

```sh
mkdir -p /absolute/path/to/paperless-audit
docker pull ghcr.io/omgapuppy/paperless-mcp:latest
```

### One-time GHCR visibility

The first workflow publication may leave the container package private. After it succeeds, a
repository owner should open the `paperless-mcp` package on the `omgapuppy` GitHub profile, choose
**Package settings**, and change its visibility to **Public**. This is a one-time package setting,
not a repository secret or workflow credential.

Public pulls need no GitHub login. Before visibility is changed, or where an authenticated pull is
otherwise required, use a GitHub personal access token (classic) with `read:packages`:

```sh
read -rsp 'GitHub package token: ' GHCR_TOKEN
printf '%s' "${GHCR_TOKEN}" | docker login ghcr.io --username omgapuppy --password-stdin
unset GHCR_TOKEN
docker pull ghcr.io/omgapuppy/paperless-mcp:latest
```

Docker stores login credentials using its configured credential store. Run
`docker logout ghcr.io` after the pull if that persistence is not wanted. Never put the token in
the image reference, Docker arguments, Codex TOML, or a committed file.

The image runs as UID/GID `10001:10001`. On native Linux, make the audit directory writable by
that identity and keep it accessible only to the appropriate host administrators, for example:

```sh
sudo chown 10001:10001 /absolute/path/to/paperless-audit
sudo chmod 0700 /absolute/path/to/paperless-audit
```

Docker Desktop mediates bind-mount permissions differently; verify by applying only a test
proposal before relying on audit persistence. Do not enable writes if the audit mount is not
durable and writable.

Copy one Docker block from
[`examples/codex-config.toml.example`](../examples/codex-config.toml.example) into
`~/.codex/config.toml`. Both examples use:

- the published `ghcr.io/omgapuppy/paperless-mcp` image;
- `--rm -i` for an ephemeral local stdio process;
- a read-only root filesystem and a size-bounded `/tmp` tmpfs;
- a persistent writable bind mount only for `/data/audit`;
- `no-new-privileges` and all Linux capabilities dropped; and
- no published or listening MCP port.

### Token choice 1: environment forwarding

Export the values before starting Codex:

```sh
export PAPERLESS_URL=https://paperless.example.test
read -rsp 'Paperless API token: ' PAPERLESS_API_TOKEN
export PAPERLESS_API_TOKEN
printf '\n'
```

The Codex example allowlists the variable names and Docker receives
`--env PAPERLESS_API_TOKEN`, so the token value is absent from TOML and the command arguments.
It is still part of the container environment. Users with permission to inspect Docker containers,
diagnostic code inside the container, or a sufficiently privileged child process may see it. This
is usually a reasonable convenience tradeoff on a trusted, single-user workstation.

### Token choice 2: read-only file mount

Store the token at a private absolute host path, then mount that one file read-only. The container
receives only `PAPERLESS_API_TOKEN_FILE=/run/secrets/paperless_api_token`; the token itself is not
in the container environment.

The image is non-root, so the mounted file must be readable by UID/GID `10001:10001`. On native
Linux, a group-readable file can provide that access:

```sh
sudo chown root:10001 /absolute/private/path/paperless-api-token
sudo chmod 0440 /absolute/private/path/paperless-api-token
```

On a single-user Docker Desktop host, another practical option is a `0444` token file inside a
host directory that only your account can traverse. Check the effective mount permissions on your
platform. File mounting reduces accidental environment disclosure; it does not protect the token
from a host administrator or someone with equivalent control of the Docker daemon.

The file-backed secret in [`examples/docker-compose.yml`](../examples/docker-compose.yml) follows
the same rule. Local Compose implements it as a bind mount, so service-level `uid`, `gid`, and
`mode` declarations would not remap the source file. On native Linux, set the source
`paperless_api_token.secret` itself to owner/group `root:10001` and mode `0440` as shown above.

### Image versions, updates, and rollback

Each release publishes three equivalent multi-platform tags:

- `X.Y.Z`: immutable version, for example `0.1.0`;
- `vX.Y.Z`: the same immutable version with the GitHub release prefix; and
- `latest`: mutable pointer to the newest successful release.

Pin `vX.Y.Z` or `X.Y.Z` when reproducibility matters. `latest` is convenient but Docker does not
refresh an already cached tag automatically:

```sh
docker pull ghcr.io/omgapuppy/paperless-mcp:latest
```

Restart Codex after pulling so it starts a new container. To roll back the application, change the
image in `config.toml` to an earlier immutable tag, pull it, and restart Codex:

```sh
docker pull ghcr.io/omgapuppy/paperless-mcp:v0.1.0
```

Application-image rollback does not undo Paperless metadata changes. Use the MCP/CLI rollback
workflow and persisted audit files for document rollback. Prefer the release that created an audit
artifact, or a later compatible release, when interpreting that artifact.

### Enabling writes in a container

Writes are disabled by default. To make them available, the operator must explicitly export
`PAPERLESS_MCP_WRITE_ENABLED=true`, add that name to the Codex `env_vars` list and Docker `--env`
arguments, restart Codex, and still set `apply=true` on each write-capable call. The persistent
audit mount is mandatory for this mode.

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

## Release process

Releases are made only from a pull request merged into `main` with the `release` label:

1. In the release PR, change `[project].version` in `pyproject.toml` to a new strict `X.Y.Z`
   version and move the relevant changelog entries from **Unreleased** into that version.
2. Add the `release` label and let the normal required CI checks pass.
3. Merge the PR into `main`.
4. The release workflow checks out the exact merge commit and builds one
   `linux/amd64` + `linux/arm64` image under a merge-specific `sha-<commit>` staging tag.
5. It verifies the staging index digest and its revision/version annotations, then confirms any
   existing immutable tags already identify that exact digest.
6. It promotes and re-verifies `X.Y.Z` and `vX.Y.Z`, creates or verifies Git tag `vX.Y.Z` against
   the merge commit, and creates or verifies the GitHub release.
7. As the final step, it moves `latest` to the same digest only if no higher strict `vX.Y.Z` Git
   tag exists. Re-running an older release therefore cannot rewind `latest`.

The image is published as `ghcr.io/omgapuppy/paperless-mcp:X.Y.Z`,
`ghcr.io/omgapuppy/paperless-mcp:vX.Y.Z`, and
`ghcr.io/omgapuppy/paperless-mcp:latest`. It includes OCI source, description, license, version,
revision, and build-time labels plus registry-hosted maximum-mode build provenance and an SBOM.
Version tags are never intentionally overwritten; `latest` is the only mutable tag.

The merge-specific staging tag is retained as a recovery anchor. If a run stops after any
publication step, use **Re-run failed jobs** in GitHub Actions. The workflow reuses that digest,
accepts an existing Git tag only when it dereferences to the same merge commit, and accepts an
existing release only after that tag is verified. An immutable tag pointing elsewhere fails the
run. Auth, network, and unexpected API failures also fail closed rather than being treated as
"not found". These rules let a partial release resume without rebuilding or silently replacing a
versioned image.

There is one deliberate bootstrap exception: before any `vX.Y.Z` Git tag exists, an authenticated
Packages API `404` is treated as "the package has not been created yet", allowing the first staging
push. After the package exists, a `404` is not accepted during collision checks; the workflow
fails closed and can be retried after package access or API availability is restored.

## Troubleshooting

- A configuration error before MCP initialization usually means `PAPERLESS_URL` or the token
  source is absent or invalid.
- An authentication failure means Paperless returned 401/403; rotate or correct the token without
  pasting it into logs.
- A TLS verification failure should be fixed by installing/trusting the correct CA. Disabling
  verification is available for exceptional local testing but is not recommended.
- A page/content limit error is a server-side safety bound. Request smaller pages or OCR chunks
  instead of raising the global limit without review.

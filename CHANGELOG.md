# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this
project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.0] - 2026-07-25

### Added

- Initial packaging, configuration, domain model, CI, and container foundations.
- Read-only-by-default safety controls and Docker secret support.
- Release-labeled PR publishing for multi-platform GHCR images with immutable version tags,
  mutable `latest`, OCI metadata, provenance, and an SBOM.
- Local Docker stdio operator examples for environment-forwarded or read-only-mounted tokens,
  hardened containers, persistent audit storage, updates, pinning, and application rollback.
- Proposal validation, guarded dry-run/apply MCP and CLI interfaces, exact stale-state reporting,
  protected-tag enforcement, immutable audit runs, and first-class rollback.
- Dedicated audited note creation without an unsafe claim of note-deletion rollback.
- Two-phase batch preflight, nested-tag cascade protection, sealed audit integrity manifests, and
  explicit indeterminate recovery outcomes.

### API

- Added the public MCP tools `paperless_get_active_policy`, `paperless_validate_proposals`,
  `paperless_preview_document_changes`, `paperless_apply_document_changes`,
  `paperless_preview_batch_changes`, `paperless_apply_batch_changes`,
  `paperless_add_document_note`, `paperless_preview_rollback`, and
  `paperless_apply_rollback`. Every write-capable schema defaults `apply` to `false`.
- Mutation results add `noop_count` plus `no_op` and `indeterminate` statuses so actual writes,
  unchanged requests, and ambiguous outcomes are not conflated.

[Unreleased]: https://github.com/omgapuppy/paperless-mcp/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/omgapuppy/paperless-mcp/releases/tag/v0.1.0

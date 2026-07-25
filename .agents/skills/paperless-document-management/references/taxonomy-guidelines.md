# Taxonomy guidelines

## Roles

Use structured metadata before adding tags:

- **Correspondent:** the issuer or sender.
- **Document type:** what the document is.
- **Storage path:** its archive organization.
- **Tag:** a durable subject, retention marker, or workflow state that cuts across other fields.

Avoid tags that merely repeat a correspondent, document type, or storage path unless the
operator's established taxonomy deliberately uses that distinction.

## Naming and preservation

- Reuse existing items by default.
- Prefer stable, specific names that will apply to multiple records.
- Avoid one-off tags and OCR-derived incidental words.
- Avoid duplicate singular/plural forms and near-synonyms without a documented semantic
  distinction.
- Preserve workflow and retention tags such as Inbox, Needs Review, Important, and Retain
  Original.
- Prefer tag additions/removals over wholesale replacement.
- Treat nested tag hierarchy as intentional until document samples show otherwise.
- Do not recommend exposing account numbers or personal identifiers in taxonomy names.

## Audit procedure

1. Retrieve taxonomy and usage counts within server bounds.
2. Group Unicode/case/whitespace-normalized name collisions.
3. Flag likely singular/plural pairs.
4. Suggest possible near-synonyms conservatively; do not call them duplicates without evidence.
5. Identify unused and nearly unused items.
6. Sample documents under each overlapping candidate.
7. Record whether the difference represents hierarchy, workflow state, retention policy,
   jurisdiction, time scope, or an accidental duplicate.
8. Produce a report containing IDs, names, usage, sample evidence, confidence, risk, and a
   recommendation.
9. Make no changes.

Paperless MCP v1 has no guarded taxonomy merge or delete tools. Never simulate a merge by
retagging documents and assume deletion will follow. If the operator wants remediation, propose
document-level changes as a separate previewable batch, preserve protected tags, and leave the
taxonomy item itself unchanged.

## Creating items

The operator policy may describe preferred names, but it cannot enable a disabled server
capability. This release exposes no taxonomy creation tools. When no existing item fits, report
the gap and leave the document unchanged or use the existing review tag if directly instructed.

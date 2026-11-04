# All-report review adapter

This update adds a private, versioned review workflow around the original verifier.
The original `app/model.py`, `app/report_intake.py`, and original ZIP payload remain
unchanged. New behavior is in `hosting/report_workflow.py`,
`hosting/source_adapter.py`, `api/index.py`, and `web/workflow-ui.js`.

## What is implemented

- Exact source scope: stable site ID, list ID, client ID, job ID, shift ID, and
  literal report date. Unknown client identity is retained as null and blocks
  approval and actionable corrections.
- Explicit field definitions. Certified is never renamed accepted; reworked is
  never inferred to mean accepted. There is no universal inspected = accepted +
  rejected rule. Only the supplied field rules and row/total/source comparisons
  are applied.
- Absent, null, blank, false, zero, and numeric strings are not conflated.
  Unknown required quantities remain unknown. Partial totals never become zero.
- Immutable report versions, source snapshots, SHA-256 hashes, and review events.
  Same version/same bytes is idempotent. Different content under the same version
  is rejected. Stored hashes are recomputed on read.
- Individual and explicit bulk correction proposals. Proposals bind the exact
  manifest, prior state, source state, and target location. Applying selected IDs
  creates a new private draft; it never modifies the source or original report.
  Cell changes never silently change totals. Totals are separate proposals.
- Missing/extra records get explicit add/remove proposals only when all required
  source and scope evidence is usable. No deduplication or zero filling occurs.
- New report versions or source snapshots invalidate effective prior approvals.
  Historical approvals stay attached only to their historical version.
- Exact-hash private-draft human approval gate. **The HTTP API deliberately cannot
  approve yet:** authenticated reviewer integration is absent. A request-supplied
  identity, reviewer name, role, or authenticated=true flag cannot bypass this.
  The trusted identity interface is tested internally and is not a public API.
- Original source metadata remains bound by the full private envelope hash.
  Nested and outer evidence blockers are preserved and malformed evidence rejects.
- Explicit atomic batch ingestion for up to 100 manifest objects, bounded by the
  hosted request size. One invalid/conflicting manifest rolls back the batch.

## Meaning and limits

A matching supplied snapshot does not establish source authenticity. Even an
approved private draft is not a production signoff or customer release.
Production source writes, customer release, SMS, email, source fetching,
automatic discovery, scheduling, recipient resolution, and live delivery are
not implemented or enabled by this adapter.

Source pagination completion is distinct from a consistent snapshot, correct
business-date timezone mapping, and confirmed shift completion. Any unresolved
condition blocks approval and actionable correction proposals. Silence or no
observed rows never means a shift ended or activity was zero.

Reports are identified by exact scope plus report_id. The same report_id in two
jobs/shifts represents two distinct reports, not a scope correction. Scope cannot
be edited through correction proposals. Do not use a different scope to claim
that an earlier report was superseded; explicit authenticated withdrawal and
scope-correction workflows remain future work.

Numeric fields accept finite JSON numbers within ±10^15 and at most six decimal
places. Integer fields require integers, not booleans or 1.0. Exact totals that
cannot safely round-trip through JSON numbers produce a blocking precision finding
rather than rounding. Each manifest is at most 2 MiB, 10,000 rows per side, and
30 explicit fields. Detail output shows up to 200 findings and 500 proposals with
accurate aggregate counts. Select a bounded proposal batch, then review the newly
validated draft for remaining proposals. Response-size limits are checked before
persistent storage commits.

## Schema 2

See `samples/workflow-report.json` for a fully synthetic, correctable example.
A manifest has:

- schema_version: 2
- report_id and immutable version
- scope: site_id, list_id, client_id, job_id, shift_id, report_date
- fields: objects with name, type (integer/number/text), required, total,
  nonnegative; all flags explicit booleans
- report: records [{id, values, optional field_presence/source_locator}], totals
- source_snapshot: exact scope and records, source_reference, captured_at,
  complete, consistency, shift_complete, timezone_status, field_definitions_hash
- optional generated_at, recipient_mapping_version, parent_manifest_hash,
  synthetic

Source capture time must include a timezone. Source scope and field-definition
hash must match exactly. `complete` describes traversal coverage only.
`consistency` is confirmed or unknown; `shift_complete` is true, false, or null;
`timezone_status` must be confirmed before actionable correction/approval.
Private intake additionally preserves source_revision, envelope_hash, and
validated evidence_blockers.

Hashes detect accidental changes; they are not a signature or tamper-proof audit
against an administrator who can rewrite both data and hash.

## Private source-envelope adapter

`convert_private_envelope()` accepts only `private-sharepoint-envelope/1`.
It verifies manifest_sha256 over the canonical envelope excluding that field,
checks the index/source identity and field definitions, and maps business_date to
report_date and shift_key to shift_id. It never guesses quantities or client IDs.
The original envelope is not rewritten.

To import an explicit local index into an isolated private review workspace:

    python3 hosting/import_source_envelopes.py /absolute/path/index.json --workspace /absolute/private/workspace

Then inspect locally with:

    python3 hosting/local_server.py --data /absolute/private/workspace --port 8765

This is local-only. It does not fetch sources, use credentials, upload data,
change SharePoint, or send anything. Actual client data and local workspace
snapshots are intentionally excluded from this source deliverable.

## HTTP routes

All routes inherit protected-preview, host, same-origin, JSON, request-size, and
no-store controls from the Vercel adapter.

- GET /api/workflow: paginated report summaries; optional before cursor
- GET /api/workflow/version?hash=...: findings, exact hashes, proposals and events
- GET /api/workflow/sample: bundled synthetic example
- POST /api/workflow/import: schema 2 or supported private envelope
- POST /api/workflow/batch: {manifests:[...]} with explicit bounded batch
- POST /api/workflow/apply: {manifest_hash,proposal_ids,new_version}
- POST /api/workflow/approve: {manifest_hash,reviewed}; fails closed until trusted
  reviewer authentication has been connected

The page supports manifest import, scope/version history, findings, individual
proposal application, and explicitly selected bulk application. The approval
control is disabled and explains the missing reviewer identity connection.

## Verification and remaining deployment blockers

Automated tests cover scope/hash integrity, null preservation, distinct numeric
types, exact decimal totals, correction selection, batch rollback, stale reports
and sources, private identity gating, API protection, envelope blockers, and the
retained spreadsheet-upload/manual-review behavior. Actual private input checks
are recorded separately outside this package and are not a claim of all-client
coverage.

Vercel team access, a separately authorized dedicated Postgres connection, and
trusted reviewer identity remain pending. No new project, credentials, external
messages, access grants, or production changes have been made. Browser rendering,
live PostgreSQL transactions, and hosted Vercel behavior remain unverified.

## Daily job reports (November 4 revision)

Daily scope explicitly contains site_id, list_id, client_id, job_id, report_date and report_granularity="daily". It has no fabricated physical shift ID. Every detail row carries its actual shift_id; report.shift_breakdown records each shift's record count and totals. A daily source has member_shifts with exact physical scopes, revisions, capture times, evidence flags and record-ID partitions, plus explicit shift_coverage_complete. Unknown or empty member activity remains blocked, not zero.

A daily report has one current head per exact daily scope, independent of display report ID. Member freshness binds actual rows, field definitions, evidence, revisions and timestamps. A newer physical shift snapshot, newly discovered shift or omitted locally known shift invalidates daily approval. Corrections to daily totals and per-shift totals are explicit individual/bulk proposals; applying a cell correction does not silently fix totals. All originals and source evidence remain unchanged.

The responsive internal UI displays day/shift breakdowns and does not put checks in client workbooks. See BRANDED_REPORT_CHECKS.md for the separate XLSX template/source verification path. Its source comparisons are review-only, not automatic workbook edits or business signoff.

# Explicit AI choice and minimized report questions

Integrated acceptance: see [verification/report-list-final.md](verification/report-list-final.md) for the combined final gates and exact source attribution. The development-stage results below remain historical records.

This patch is based on the Ask Dex core commit ec0d2f9. It does not include the separate temporary connectivity probe. No real provider request, production write, deployment, credential change, or source-service write was performed for these tests.

## Call-site inventory

- `inspect_spreadsheet` -> strict template / `_exact_or_adaptive` / `inspect_adaptive`: local checks by default. `allow_ai_layout=False` propagates through each entry point. The sole production reference to `ai_layout.detect` is behind `allow_ai_layout is True` in `inspect_adaptive`. No production caller enables it.
- `report_records` and `report_and_export_records`: deterministic default, including source comparison, dashboard, deterministic corrections and VIN indexing. Opening a report -> loading fixes -> `fix_proposals` -> `fixes.proposals` no longer implicitly sends layout context. Sheet/cell views use local extraction.
- Browser import and multipart finish: removed the shared automatic `self.ai_review` tail. Ordinary recheck preview/apply is local parsing plus the existing source comparison. Autoformat is already paused with an immediate return, not an alternate model path.
- Public API/MCP import and multipart finish: both enter `public_api._checked`; removed its separate automatic `('ai',)` tail. Other `_after_change` calls default to SharePoint/baseline only. `op_recheck` includes AI only if the caller explicitly passes `checks=['ai', ...]`.
- New Ask Dex: explicit Send -> `/api/spreadsheets/chat` -> `report_chat.answer` -> `ai_review._post`. No model tools or write actions. Relevant excerpt only.
- Selected AI proposals: separate integration-owned `/ai-proposals` route uses selected supported cells and the existing transport. It does not opt into layout detection.
- Remaining legacy AI review, fixes, resolve/verification/summary and dashboard edit paths are separate explicit AI POST actions; recommendation endpoints call the model only with `ask_ai=true`. They have broader contexts and are not covered by the new selected-context permission. Do not wire the new choices to those endpoints. The old edit-chat helper is not the restored Ask Dex route. Automatic cleanup is paused. This patch does not redesign those unrelated legacy features.

`ANTHROPIC_API_KEY` presence and default `AI_LAYOUT` previously made implicit layout attempts possible; metadata/health alone never established real production transmission. Tests use a fake key and forbidden/fake provider, with no real credential or remote request.

## Question context contract

Only the selected immutable report is read. At most 12 explicit or relevant finding cell references and their same-column headers are sent, each value bounded to 160 characters. Up to 12 relevant findings are bounded, plus a small summary/identity and source/hold status. Unknown sheet/report references do not resolve anywhere else; ranges do not expand. There is no whole-sheet or whole-workbook excerpt, adjacent-row value dump, original-file payload or secret/config source.

Question text, prior turns, report values and provider replies are untrusted text. No tools or legacy changes/undo fields execute. Source mapping/assignment holds downgrade stored comparison context to historical/unverified and omit source differences. Response context is bounded; blanks and missing saved formula results stay unknown. File version and check revision remain separate, with unknown values shown honestly.

History is memory-only and bound to file, check, review/hold/assignment state. The server binds later turns to a context fingerprint and rechecks access and context after the provider. Pending navigation remains usable; late success/error cannot paint another report. Unsent drafts are retained per context (maximum ten) and cleared on sign-in/reload.

## Compatibility and historical evidence

No checker-version bump or stored report rewrite is needed: an earlier result continues to describe its saved checks, not a new deterministic revalidation. Historical AI layout metadata remains unchanged on reads. New uploads and recheck previews may recognize fewer rows for unusual layouts. This is surfaced as unknown/unverified, never fabricated zero quantities. Applying a recheck remains an explicit review action with existing source-staleness rules.

There is no AI-layout mapping button in this release. For an unsupported layout, prepare a copy with recognized headers or a supported template and upload it, then inspect/recheck the result. Ask Dex can explain known sheet/cell evidence; it does not map or verify an unknown workbook. No UI wording promises automatic layout help.

Row analytics refuse missing, partial, or unknown recognized detail data instead of converting it to zero rejects. An explicitly all-zero saved summary is distinguished from unknown or positive saved totals, but still does not invent row analytics.

The source scope guard compares only authoritative adaptive detail counts or matched PPG branded detail counts with deterministic detail extraction. Summary/export duplicates are excluded by the existing dataset selection. Generic first-row counts include totals/blanks, and Pratt completed/failure/child counts can have different grains, so those incomparable counters do not trigger blanket invalidation. Unknown authoritative or historical-AI counts fail closed. Source comparison stops before saving a source upload or receipt on missing/mismatched rows.

A stored complete exact-source receipt can remain historical evidence for the same immutable workbook without a fresh parse: it binds the workbook hash, checker, source snapshot, exact site/list, complete record/quantity evidence and mapping. A receipt with a missing/mismatched authoritative row count is no longer current; existing holds, source-backed apply and PDF receipt checks fail closed. Equal complete receipts are not invalidated merely because implicit AI parsing was removed. Current source mapping and assignment checks still apply independently.

## Offline evidence

Synthetic actual HTTP/browser tests exercise key-present/default-AI configuration for opening reports, listing, fixes, sheet/cells, browser/public API upload, and ordinary rechecks. Provider attempts are forbidden except explicit fake Ask Dex sends. Headless desktop/phone tests have exactly eight fake chat calls, zero external traffic and unchanged originals/report decisions. Unrelated sentinel sheets/cells are absent from Q&A payloads. Source tests include adaptive totals, branded counts, multi-sheet source duplicates, missing/partial/unknown scope, historical receipts and no-save behavior.

Final aggregate PostgreSQL and integrated UI checks belong to the release integrator. A local run without `CI_PG_TEST_URL` is not proof of the PostgreSQL deployment gate. Real provider connectivity remains a separate, approved fixed-synthetic test, and its temporary route must be removed before final release.

### Final local gate results

- Final focused Python set: 80 passed, including the source receipt/quantity-completeness regressions.
- Final Node aggregate: 113 passed.
- Final headless desktop/phone acceptance: both passed, eight fake provider calls, zero external traffic, unchanged reports.
- Earlier broad local run: 1303 passed, 18 skipped, 282 subtests passed; two synthetic receipt fixtures lacked authoritative report_rows. Those fixtures were corrected and their complete finding-accounting suite passes in the final focused set. The release integrator must rerun the full combined suite, including disposable PostgreSQL, before release.

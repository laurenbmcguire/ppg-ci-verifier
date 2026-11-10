# Consistent saved-version verification workspace

Historical, approved, sending, sent and inconsistent-history reports now use the same report workspace styling, navigation and key totals as editable reports. The read-only Review view pairs the saved spreadsheet with grouped findings. History and Sources reuse the existing evidence views.

## Preserved boundaries

- The authoritative working-copy resolver and mutation APIs are unchanged. A legacy self-cycle remains an inconsistent history, never a repaired or editable copy.
- The visible file hash, check hash and download refer to the exact saved version.
- Saved findings are grouped by rule, column, sheet, severity, source and recorded status. Every occurrence remains reachable through the existing 25-item occurrence pager. Saved human decisions, deferred findings, informational entries and AI recommendations remain distinct. Local draft selections are not consulted.
- Editing, saving, AI, source refresh/mapping changes, checker revalidation, undo, approval and sending are unavailable in this view. The normal read-only sheet API is still used to inspect saved cells.
- The view does not mark a report verified, resolve holds, repair lineage, or rewrite report bytes. Unknown quantities remain unknown. Inconsistent ancestry is labeled as recorded, unverified references in History.
- Each sheet request is bound to its report, view and request number. Older responses cannot overwrite newer sheet selections, another tab or another report. Sheet failures offer a read-only retry.

## Local verification

- `node --check web/app.js`
- `node --test tests/test_*ui.js`: 238 passed.
- Clean Python non-Postgres suite in the existing test virtualenv: **1,645 passed, 35 skipped, 300 subtests passed**. `tests/test_site_timezones.py` excluded because its imported module is absent in the base checkout. Postgres tests intentionally skipped with no `CI_PG_TEST_URL`; no shared database was started. This aggregate preceded the final UI-only source scheduler guards and unverified-history wording changes, which were rechecked with the full Node and both browser suites.
- `node tests/browser_readonly_verification.js`: whole production app against an isolated synthetic loopback API, 1200px and 390px. All 1,294 findings reachable, five read-only states, exact-byte downloads, keyboard paging/collapse, saved-cell navigation, failed-sheet retry, stale sheet/report responses, Back navigation and no page overflow. Stale and unbound source evidence does not schedule automatic comparison or display a false waiting state. Zero mutation or AI requests, no external network requests and no browser errors.
- `node tests/browser_grouped_review_smoke.js`: existing grouped history/source evidence tests pass at both widths, with all 302 occurrences reachable.

The browser scripts use `PLAYWRIGHT_MODULE` and optional `CHROMIUM_EXECUTABLE` to select an already installed Playwright/Chromium. `READONLY_SCREENSHOT_DIR` optionally saves synthetic screenshots for the new test. No production report data is required.

No deployment, live database/source comparison, lineage repair, real approval or delivery was performed.

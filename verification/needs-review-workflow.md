# Needs review chooser candidate

Synthetic-only UI changes, based on 1740eda7436d38ab5a1618ecf1afa542f3be542c. Original files and backend decisions remain unchanged until explicit guarded actions.

- Chooser opens all loaded non-informational review items, including unresolved and grouped history, with incomplete-coverage notice.
- Review-scope selection by actual error code across groups, group, occurrence, all unresolved loaded items, or none. Pagination is presentation only.
- Supported selected proposals use existing immutable preview; no auto-apply. Manual scope navigates selected items and remains scoped after own successful decisions. External changes require reopening.
- Keep/defer records exact fingerprint-bound decisions, requires reasons and blocker evidence, retains unknowns/holds. Existing undo/source review remains available.
- Explicit AI-unavailable text is provisional pending separately requested scoped AI proposal support. No AI provider was contacted or enabled by this patch.
- Detail quantity metrics use explicit completeness/compatible units for rates; legacy totals labeled uncertain.

Validation: node --check web/app.js; node --test tests/test_*ui.js: 105 passed. tests/browser_needs_review_smoke.js passed approved headless Chromium at 1200 and 390 widths. Tests use fabricated items and stubs for decision/preview callbacks; they establish UI binding/selection behavior, not full API acceptance. Full integrated API/browser tests remain parent integration responsibility. No live reports/providers accessed; no push/deploy.

Independent review feedback addressed: real Needs review tab, error-type vs subgroup scope, selected navigation after decisions, keyboard focus, sticky Close, disabled-action explanation. Finite Claude review awaits merged candidate.

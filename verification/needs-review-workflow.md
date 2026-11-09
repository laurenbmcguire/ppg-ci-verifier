# Needs review chooser candidate

Synthetic-only UI changes, based on 1740eda7436d38ab5a1618ecf1afa542f3be542c. Original files and backend decisions remain unchanged until explicit guarded actions.

- Chooser opens all loaded non-informational review items, including unresolved and grouped history, with incomplete-coverage notice.
- Review-scope selection by actual error code across groups, group, occurrence, all unresolved loaded items, or none. Pagination is presentation only.
- Supported selected proposals use existing immutable preview; no auto-apply. Manual scope navigates selected items and remains scoped after own successful decisions. External changes require reopening.
- Keep/defer records exact fingerprint-bound decisions, requires reasons and blocker evidence, retains unknowns/holds. Existing undo/source review remains available.
- Scoped AI preparation uses explicit selected IDs and fingerprints with check/run/revision binding, after explicit user consent to selected relevant context sharing with Claude/Anthropic. No full workbook or unrelated report context is sent. AI suggestions retain machine safeguards and require unchecked per-item inclusion, preview/removal and explicit Apply. Legacy whole-report UI routes now open the chooser only. No live AI provider was contacted in development tests.
- Detail quantity metrics use explicit completeness/compatible units for rates; legacy totals labeled uncertain.

Validation: node --check web/app.js; node --test tests/test_*ui.js: 105 passed. tests/browser_needs_review_smoke.js passed approved headless Chromium at 1200 and 390 widths. Tests use fabricated items and stubs for decision/preview callbacks; they establish UI binding/selection behavior, not full API acceptance. Full integrated API/browser tests remain parent integration responsibility. No live reports/providers accessed; no push/deploy.

Independent review feedback addressed: real Needs review tab, error-type vs subgroup scope, selected navigation after decisions, keyboard focus, sticky Close, disabled-action explanation. Finite Claude review awaits merged candidate.

## Scoped AI validation

113 Node tests passed (all test_*.js), including seven owned-operation race tests: save/recheck/decision exclusion in both directions, exact selection, current/new controls, stale success/error, cancellation/successor ownership, navigation and unsent draft preservation. Synthetic actual-handler tests at 1200 and 390 use a deterministic fake provider and refuse external network: request two exact synthetic findings, cancel first preview without mutation, request again, remove one, apply one, verify the original bytes and every changed cell. One new corrected version is created and no approval/delivery is granted. Standalone chooser test keeps selected count and all eight controls hit-testable after deep scroll at both widths. These are synthetic tests, not live provider proof.

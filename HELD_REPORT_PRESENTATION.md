# Held-report labels

This patch changes read-only presentation, not stored finding status, report bytes,
source evidence, holds, reviewer decisions, approval eligibility, or sending.

- List and daily summaries include current local assignment/hold/source context.
  A failed context lookup produces a visible verification hold, never success.
- A zero-open-findings report with a source or advisory hold displays
  **Verification held** and stays out of Final reports. Missing/failed metadata
  displays an incomplete/unavailable check instead of Clear.
- Final reports require no open findings, complete current source verification,
  no holds, current checks, complete finding coverage, a settled client/job/date,
  and the existing saved-version rules. Final is not a new approval or send.
- Today uses the same display rules. Approval history is separate and an old
  approval is labeled out of date. Yesterday-only counters say so explicitly.
- The waiting count uses the To do predicate and only claims completeness for
  the loaded saved-report list, never missing reports or complete source coverage.
- Confirmed saved/rechecked correction labels remain Fixed. AI, manual correction,
  and Ignore controls are unchanged. Proposed changes are not labeled Fixed.

## Validation

All data is synthetic. No real source reads/writes, deployment, PostgreSQL startup,
approvals or sends were performed.

- `node --check web/app.js` and `git diff --check` pass.
- `node --test tests/test_*ui.js`: 320 passed.
- Focused Python suite: 99 passed, 17 subtests passed. Files:
  `test_held_report_presentation.py`, `test_report_list_context.py`,
  `test_source_pattern_classification.py`, `test_working_copy.py`,
  `test_periods.py`, `test_review_choice_eligibility.py`, `test_checked_copy.py`.
- `tests/browser_held_report_labels.py`: actual local HTTP handlers, SQLite and
  Chromium at 1200 and 390 px. A real synthetic source comparison with missing
  rejection evidence produces zero findings plus a source hold. The queue, Final,
  Today, unknown-rejected quantity, waiting count and unchanged saved bytes are
  checked, with all external traffic forbidden.

Run the browser script with `EVIDENCE_DIR`, `SOURCE_FIRST_CHROMIUM`, and a local
non-symlink `TMPDIR`, using a Python environment with Playwright installed.
The full native PostgreSQL aggregate is left to the integrating parent.

# Shared reporting statistics verification

This patch is local and not deployed. Tests use independent synthetic data, not live customer audit totals or source records.

## Verified

80 focused Python tests and 12 parameter subtests passed after the narrow review follow-up. The latest output is `verification/kpi-review-followup.txt`; `verification/kpi-focused.txt` retains the earlier 76-test result.

Command: `PYTHONDONTWRITEBYTECODE=1 ../test-venv/bin/python -B -m pytest -q -p no:cacheprovider tests/test_report_statistics.py tests/test_kpis.py tests/test_review_overview.py tests/test_dashboard.py tests/test_recommend.py tests/test_access.py`

The tests cover shared Overview/KPI totals and categories, source/client/job/date scope, duplicate versions, incompatible units, missing/partial quantities, known zeros, finite numeric limits, exact decimal sums, aggregate numeric-range limits, rate denominators, stale approvals, timezone-aware send timing, drafts versus recorded sends, category deduplication, source-label collisions, competing originals, unknown upload order, orphan corrected copies, truncated finding lists, artifact wording/decimals, and API access filtering before statistics and coverage. A manually reassigned report remains visible to the assigned client even if its original filename is wrong. A different client's assignment does not leak through a matching job label.

The review follow-up also proves that a child-only copy cannot establish original-upload turnaround, uncertain ordering makes the first-original finding metric incomplete, and only exact allowed integer durations reach aggregation/artifact keys. Float, boolean and string durations safely use the existing 30-day default. On-time recommendations name the recorded-send denominator.

`git diff --check` passed. No PostgreSQL, live source writes, external model calls, real approvals, sending, deployment, or publication were used for these tests.

## Contract

- `hosting/report_statistics.py` is the single read-only calculation path used by Overview and KPIs. Every quantity field and category is derived from the same selected daily records.
- The aggregation unit is one exact source/job/report-date group. SharePoint sources require site and list IDs. Source-only registry families use their distinct source identity. Ambiguous, incomplete, needs-person or conflicting-client groups are excluded with explicit coverage diagnostics.
- Multiple independent original uploads are disclosed, not asserted to be known revisions. Selection remains recorded-sent, current approval, corrected copy, then newest upload. Its scope is stated and is not called the latest report.
- `caught` is listed non-informational findings in the current review of the earliest saved original, with an explicit earliest-copy fallback. It is not immutable initial-check history or a count of confirmed distinct defects. `current_issues` describes selected versions.
- Saved correction records are reported separately. `stopped`, `reached` and related client-outcome measures are null because there are no immutable at-send snapshots or evidence of what a client saw. Zero recorded sends does not establish no external delivery.
- Missing, partial, invalid or incompatible quantities remain null. Known subtotals, complete/missing report counts, incompatible-unit counts, and supported-numeric-range flags accompany each quantity. Decimal arithmetic avoids binary floating-point accumulation; unsafe aggregate totals are unknown rather than rounded into a false number.
- Coverage diagnostics explicitly cover all authorized saved reports before client/job filtering, not the selected client/job denominator. Permission filtering uses the application's canonical `can_report` rule before aggregation, metadata or output.
- Generated Overview pages/slides and recommendation facts use the same definitions and evidence limits. Visible recommendation fallback wording uses Dex; internal provider identity is unchanged.

## Remaining limits

This is focused synthetic/API validation, not the final combined integration regression. The app/UI patch is owned separately and must be tested after merging. Downloaded Overview artifact layout has not had visual acceptance testing in this worktree. Arithmetic completeness does not verify the truth of source values or resolve remaining report findings. No live release or client-outcome accuracy is claimed.

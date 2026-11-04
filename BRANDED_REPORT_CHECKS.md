# Branded daily workbook verification

This revision fixes the generic parser's row-1 assumption for the PPG daily client report. A decorative brand row is not a quantity header. Unsupported mappings are explicitly **unverified**, never passed.

## Supported mapping

- Recognized revisions: original `ppg-daily-client-report/1`, sectioned `ppg-daily-sectioned-report/2`, all-fields `ppg-daily-all-fields-report/3`, and active-tabs `ppg-daily-active-tabs-report/4`. Unsupported or incomplete mappings never pass.
- Exact PPG Summary and named physical shift roles. The original/all-fields revisions include all three shift tabs and `Rejects & Photos`. The active-tabs revision permits only observed shift tabs, a `Rejects` register when supplied, and no empty photo tab. Missing tabs never establish zero activity, completed shifts, complete rejects, or clean photo evidence. Known reject register rows remain explicitly checked after renaming. Unsupported extra sheets still fail closed.
- Inspection detail headers are exactly row 15, records begin at row 16. Quantity columns are G (Inspected), J (Rejected), and K (Certified). Certified is not silently renamed Accepted.
- Summary project/date and detail project/physical-shift must agree. The all-fields revision uses the exact new project metadata headers instead of the old date/shift header cells; every inspection-row date still matches Summary D14. Records retain their physical sheet and row location.
- Sectioned records end at the unique `SHIFT INSPECTION RESULTS` marker. Appended totals, rates and billing fields cannot become inspection rows. Empty/omitted shifts remain unverified.
- All-fields shift totals, chart-source cells, per-part SUMIF formulas and positive known-reject registers have explicit range/coverage checks. Part labels must be strings; wildcard, operator, numeric-coercion and casefold-collision criteria remain blocked. Populated unsupported rates and register-like rows outside the table boundary are flagged.
- Known-reject registers exclude unknown quantities and explicit zero rejects. Photos, reasons/comments and business metadata are not authenticated by register quantity checks.
- Summary formulas are preserved byte-for-byte in the original. Only the documented exact cell/range structure is checked. Formulas and caches are never executed or trusted as source truth. A single observed shift may use K21/K22/K23 `=J9` only when J9 itself references the complete matching detail range.
- Missing, invalid, negative, fractional and unsupported-range quantities remain unknown. Known rejected sums are separate from complete totals. An empty shift does not establish zero activity or completion.
- Photos and business metadata are not validated by numeric checks. Blank part numbers are reported.

## Separate source evidence

Source comparisons require schema `verifier-spreadsheet-source/1`, a canonical SHA-256 over all fields except `source_sha256`, an explicit site/list/job/project/date scope, provenance category, capture time, completeness/consistency/timezone/shift-coverage flags, and records with actual source IDs. Each row contains date, shift_id, part, identifier, and explicit inspected/certified/rejected values. Missing source keys may be retained as `field_presence` states; a blank historical evidence cell does not prove that the underlying source key was absent.

`retained_supplied_snapshot` distinguishes separate historical evidence from `independently_retrieved` data. Neither client-supplied JSON category authenticates the source to the application. The application never fetches a URL in this evidence.

Report project aliases require an explicit, evidenced mapping of source project, report date, workbook project and exact workbook hash. A different version cannot reuse that binding. Unknown source identity remains blocking even when observed values can be compared.

Rows match by exact date/shift/identifier only when unique. Repeated identifiers are compared as whole-group multisets, with ambiguity retained. No cell correction or source ID is guessed. Known unequal values count as comparisons and produce explicit workbook/source differences. A newly observed source row makes an older workbook stale even if its own totals remain internally consistent.

## Review and versioning

The private upload UI offers separate source JSON comparison. Findings, before/source-value differences, hashes and provenance remain internal; no validation tabs are added to client workbooks. The original XLSX and formulas are unchanged. New check results and supplied source evidence are retained as immutable SQLite records inside the existing durable workspace mechanism.

A review binding includes the exact workbook hash, source hash, formula hash, template ID and checker version. Older or same-time conflicting source evidence cannot replace the latest review. Repeating the current comparison is idempotent. Legacy checks are marked obsolete and recomputed from the unchanged original on open.

All results remain held. The branded comparison does not approve, mutate a client workbook, change SharePoint, or send anything. Authenticated reviewer identity and live secure deployment remain unconfigured.

## Local use

Run `python3 hosting/check_branded_report.py /private/report.xlsx --source /private/source.json --output /private/checks.json` with the pinned requirements installed. Omit `--source` for mapped internal checks only. Input files are read-only. The output contains private report data and belongs in a protected location.

HTTP routes: existing `/api/spreadsheets/import`, plus `/api/spreadsheets/reconcile` with `{file_hash, source}`. They inherit the same protected-preview, host/origin, size, no-store and transaction guards. The local CLI is excluded from hosted deployment.

No actual client records or source snapshots are bundled with this code package. Synthetic regression fixtures cover mapping, malicious input, quantity differences, missing records, scope mismatches, hashes, source freshness, revision retention and HTTP guards. Live Postgres, Vercel deployment, authenticated signoff and real-browser rendering are not verified by those tests.

## Pratt audit reports

Two separate mappings support the original three-sheet Pratt audit layout (`pratt-kn-daily-audit/1`) and the current nine-sheet template union (`pratt-kn-template-union/2`). The latter includes explicit completed Main/child rows, per-part Main-audit counts, and unsupported production/billing fields that must remain blank. Audit counts are not production quantities. Main and Multi-pack child lists retain separate grains; only Status Complete counts as completed, even when an In Progress row says Pass. Recorded defect-category occurrences cover failed Main audits only, not every audit with a selected defect.

`hosting/pratt_reports.py` builds the bounded `verifier-pratt-source/1` envelope from exact original raw source bytes and their recorded hashes. A target-version binding separately names the current workbook hash. This is retained source evidence, not a live authenticated connection. When source capture metadata did not survive, the capture time stays null with a blocker. Different source revisions cannot replace each other in the inbox when either capture time is unknown; repeated identical evidence remains idempotent. A report-authored date or file mtime is never promoted into independent capture evidence. Typed identifiers, case and leading zeros are preserved. Completed details are compared as typed row multisets when natural keys repeat; source-ID identity is not invented. All supported formulas are structurally checked and never executed.

The protected HTTP upload and source-comparison routes and local CLI dispatch by explicit template ID. Cross-schema evidence fails closed. Pratt's source evidence, workbook hash and checker version are retained by the same immutable inbox revision path. No result is approved or released.

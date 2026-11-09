# Reporting reliability r8 integration

## Starting point and provenance

This isolated worktree starts at safe candidate `3d568e78ec067eadf41674ee3f468dc3a399e844`, on branch `integrate/verifier-r8-reliability`. The saved candidate and acceptance checkout were not modified.

The approved archive `Reporting_Reliability_Local_Patch_r8.zip` was materialized on the consuming Mac using the current Library transfer helpers. Its 254729 bytes hash to `0bc6701edf658bb8a98f27b8a102dc601e8bac9ff12166ba30850c67593c3b40`. Every hash entry in its implementation manifest was verified. The six supplied originals correspond to base `bf17f5627d6d78008fc0bd54ae9ff6408f0ba1b5`, with an extra trailing blank line. The seventh original, change_summary.py, was read from that Git base.

## Three-way review and decisions

- **sharepoint.py:** r8 exact site/list selectors replace newest-modified-list guesses and unbound export fallback. All source collection pages must be complete, correctly shaped and on the expected Graph endpoint. Captured source records/fields remain intact. Submission timestamps do not replace business dates; recorded shift is not overwritten by a clock-derived suggestion. Unbound job-only history cannot claim an exact-source comparison.
- **builder.py:** r8 replaces inferred quantities, blank-to-zero conversion and time-derived shift choices with explicit source values and recorded shifts. The 45-minute quiet period is not evidence of completion. A successful build remains provisional, with no implicit client release. Explicit verified source builds remain supported.
- **reconcile.py:** one real textual conflict concerned snapshot hashing. The newer candidate already hashed complete item contents rather than version stamps. r8's broader digest includes those contents plus site/list IDs, columns and coverage, so it preserves and strengthens that safeguard. Pagination/identity/business-date coverage changes were retained.
- **api/index.py, public_api.py, mcp_server.py:** r8 source-selector forwarding was layered under existing authentication, displayed-check/current-checker guards and paused-mutation barriers. A further review found the explicit source-reconcile route still resolved by site name; it now forwards exact stable IDs too. Source capture output is discarded when the saved check or assignment changed while it was being prepared.
- **change_summary.py:** the other textual conflict was PDF decision wording. The newer candidate's per-version reviewer, deferred, earlier-record and AI distinction is retained, superseding r8's older generic accepted-findings block. r8's exact source receipt and current-verdict precedence are retained. Historical differences and approvals never establish current source verification.

## Additional integration safeguards

- The checker version is now `2026-11-08-source-counts-v15`; old checks cannot silently inherit the new success semantics.
- Source count evidence distinguishes inspection-record count, inspected quantity, rejected quantity and records with rejected quantity. Quantities use exact decimal strings, or null when unavailable. Missing rejection columns and blank values cannot mean zero. Overall verification is held unless required quantities are explicitly present and match.
- Duplicate report references and source records outside a confirmed business date are explained individually. Nothing is deleted or silently excluded from report totals. Counts are computed before bounded mismatch examples are sliced, so display truncation does not reduce totals.
- A successful count label is not sufficient by itself: saved receipts validate complete positive record counts, unique matches, zero duplicate/missing/unmatched records and complete equal inspected/rejected evidence.
- Source capture persistence revalidates report/checker and assignment under writer locks. Failed recapture marks prior source success stale rather than continuing to display historical success as current.
- PDF persistence locks the report lineage in deterministic order and serializes settings writers. It recollects the exact state using the same connection immediately before storing the artifact and record. PDF bookkeeping is kept outside report JSON, preserving approval fingerprints and original bytes. PostgreSQL and SQLite concurrent-writer tests cover this path.

## Test compatibility changes

Legacy test assumptions about job-only lookup, newest-list selection, submission-day/clock shifts, inferred count values and 45-minute completion are replaced by explicit positive source/date/recorded-shift/quantity fixtures and negative missing-evidence holds. No classes or safety assertions are removed or blanket-skipped. Imported PDF tests were updated to include the current `dismissed` review field and valid current checker/count receipts. The original archive and failed run logs remain preserved.

## Boundaries

All development checks use synthetic fixtures/local files and the explicitly marked disposable PostgreSQL database. No live SharePoint reads, provider/model/email calls, credentials, grants, installs, pushes or deployments occurred. Existing automatic/AI workbook mutation, row removal/relocation, legacy unbound decisions, send and draft barriers remain in force. AI proposal preparation remains separately guarded and does not apply proposals. Outlook ledger work remains standalone and not wired to a provider.

## Evidence

Logs are in `verification/r8-integration/`. `focused-first.log` and `full-first.log` preserve initial incompatibilities; subsequent focused tests cover count/receipt/save guards and actual SQLite/PostgreSQL PDF transactions. Final aggregate results will be appended after all source and test owners freeze. No green full-suite or release claim is made from intermediate runs.

## Exact-source follow-up after ebd653c

Independent review found two additional source-identity blockers. Continuation pages are now pinned to the exact canonical Graph collection path, not merely the Graph host. Both reconciliation and export helpers reject cross-site, cross-list, items-to-columns, credential, fragment, encoded-separator, double-encoding and traversal continuations before fetching them. Site/list discovery uses the same guard. Valid same-collection query continuation remains supported.

Workbook source identity now retains the raw URL and canonical HTTPS origin/list path. Full item links and DispForm.aspx?ID links must agree with the verified snapshot's origin, full list path and item ID. Names alone, relative URLs without a known origin, embedded URL text and foreign-tenant links cannot establish authority. Raw relative links remain audit evidence and require a trustworthy source reference. The checker version is `2026-11-08-exact-source-v16`, so earlier saved checks are not silently treated as equivalent.

Live source-backed proposals require verified row identity. Embedded-export fill suggestions use only unique complete source identities: either both stable IDs plus item, or normalized origin/list URL plus agreeing item. Different identity formats are not inferred to be equivalent. Duplicate export identities and same-item foreign-source collisions produce no authoritative fill proposal. Explicit person-typed corrections remain supported.

### Column mapping migration and concurrency

Legacy `column_map|list_id` settings are preserved unchanged. They are not authoritative for an exact source; the next comparison is held and usable single-candidate fields are exposed as needing explicit reconfirmation. New settings are keyed by a digest of the exact site ID and list ID, with a fresh revision on each confirmation.

The reachable admin confirmation API requires file hash, displayed check hash, snapshot ID, site/list IDs, proposed field-to-column mapping and `expected_mapping_revision` (explicit null for no existing map). Under report/settings writer locks it checks the current receipt, assignment and captured candidate columns, then compares the expected mapping revision. A competing or stale confirmation is refused, not silently overwritten.

Source capture saves re-read the mapping under the same writer transaction and discard output if it changed. Changed mapping revisions put existing source verification and PDF receipts on hold; PDFs include the current mapping digest. Source-backed corrections also revalidate mapping under report/settings writer locks. No workbook, source row or historical mapping is silently rewritten. Confirmation does not auto-run comparison or release the report: a separate comparison is required.

Synthetic regression evidence includes exact-scope map isolation, legacy migration with explicit reconfirmation, stale mapping capture rejection, concurrent confirmation CAS, current receipt/candidate rejection, stale-source correction refusal, complete URL identity and pagination boundary attacks. Existing relative-link/identity and fake pagination tests were adapted to distinguish audited raw references from authoritative full source identity. Their prior failures remain preserved.

## Static review round 4 (5967bb6 follow-up)

Independent synthetic reproductions are in tests/test_static_review_round4.py. The before log preserves six failures: manual 999 retained source provenance; missing pre-capture check hash allowed older receipt replacement; stale, historical-checker and wrong-file receipts allowed mapping confirmation; dateOnly UTC midnight silently became the previous Chicago date. The historical review_decisions changed-value child test passed before edits, so historical context remains intact.

Resolutions:
- Explicit Your edit corrections copy the caller payload and remove snapshot/item/field/change-set provenance before generating the workbook/audit. Existing whole-change-set validation runs first; rejected/deferred holds remain enforced. Manual edits remain possible without claiming source verification.
- source_reconcile requires a nonempty pre-capture expected_check_hash. Both production API callers already supplied it. Synthetic direct callers now supply their observed hash explicitly. Missing and stale hashes leave the newer saved receipt unchanged.
- Report-bound confirm_columns uses reconcile.current plus CHECKER_VERSION inside the existing writer transaction, before accepting candidate columns. Existing exact-source assignment, permission, candidate and mapping revision checks remain.
- Graph dateOnly is presentation metadata, not a UTC calendar-date guarantee. Inconsistent timestamp encodings now return unavailable instead of a shifted business date. Plain dates and existing Chicago-local-midnight compatibility are retained. Checker version is now exact-source-v17 so old receipts require recheck.

Remaining release boundary: universal Chicago conversion is not evidence of an arbitrary site's timezone. A verified exact-source date/timezone contract (or refusal of unknown-timezone captures) is still needed before claiming live source dates are proven. This narrow patch does not invent site settings or claim that timezone issue resolved. No provider, model, source network or PostgreSQL calls ran in this follow-up. Original failure logs and intermediate collection/change-set failures remain preserved. Parent owns final aggregate and commit.

### Timestamp capture boundary closed (v18)

The remaining unknown-source-timezone risk is now fail-closed in production capture and date selection. `reconcile.item_day`, used by actual snapshot, list-day/history and builder selection, accepts valid plain YYYY-MM-DD business dates. Timestamp-valued business dates are retained verbatim in raw/original evidence but receive no day and `timezone_unconfirmed`; submission-only timestamps likewise receive no business day. Graph dateOnly/dateTime presentation, offset presence, and Chicago-looking midnight never establish confirmation. No timezone configuration or caller-supplied verified bypass was introduced. A future verified exact-source contract must include site/list/field/revision freshness before timestamps can be used.

Capture coverage and reconciliation include an actionable timezone reason and undated count. Unknown dates cannot become proven out-of-day exclusions or a verified quantity comparison. List-day/history refuse mixed unknown-date sources rather than dropping rows. The low-level business_day compatibility helper retains established conversions but does not verify production timestamp source dates. Checker v18 invalidates historical semantics.

Positive synthetic fixtures now provide actual plain calendar business dates, including overnight scans kept separate. Tests cover fake-Graph capture with UTC, offset, naive, winter/summer Chicago-like timestamps, dateOnly/dateTime/missing presentation metadata, mixed known/unknown counts, raw preservation, no false exclusions, plain-date positives and list-day/history rejection. Focused result:119 passed,14 subtests passed,1 existing LibreOffice skip in6.64s (`timezone-capture-final.log`). The first run's one legacy timestamp-success expectation failure is preserved in `timezone-capture-first.log`; its replacement explicitly asserts unavailable coverage and no exclusions rather than removing coverage. No live source, configuration changes, PG, or provider calls.

### Static review round 5: correction provenance and atomic batches

Six independent synthetic regressions failed before changes: three manual label variants retained partial source metadata, a forged Reverted export claim was accepted, a complete mixed source/manual four-cell set lost membership, and a missing size reached workbook generation. Evidence remains in static-round5-before.log.

Explicit manual labels are normalized without retaining source snapshot/item/field authority. Other changes without a complete source identity cannot retain partial item/field audit claims. Complete source proposals still undergo exact proposal proof and retain valid history. All Reverted-prefixed claims are refused because revert mutation is paused; no new revert feature was added. Atomic change_set/change_set_size is retained as batch metadata, including an individually typed override. One shared validator runs before generation and under the writer lock, requiring complete membership and an explicit positive integer size. Malformed/unhashable, blank and overlong set identifiers fail cleanly before writes. Caller payloads are copied, not mutated; human holds remain.

Caller audit: both production source_reconcile calls in api/index.py pass expected_check_hash and expected_assignment. The sole production confirm-columns route remains admin-only, requires a valid authorized file_hash, and supplies the displayed check/snapshot/mapping revision to report-bound validation. No production unbound confirmation caller was found. No provider, PostgreSQL, or live source calls ran in this follow-up. Parent owns the final aggregate and commit.

Final round5 focused result:148 passed,5 subtests passed,1 existing LibreOffice skip in27.76s, with clean git diff --check. Log: verification/r8-integration/static-round5-freeze.log. Product scope: hosting/spreadsheet_inbox.py only; regression file: tests/test_static_review_round5.py. Earlier intermediate failures remain preserved.

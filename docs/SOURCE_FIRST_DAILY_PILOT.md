# Source-first DAILY pilot: staged private workflow

This is code for a manual, administrator-reviewed pilot. It is not deployed, activated, a shift-completion detector, a general daily-report generator, or a release mechanism. No live capture was made to test it. Synthetic tests cannot establish actual job readiness.

## Exact base and selected asset

The isolated import of tracked cde5657c8a513f008abfc0d2136b2994912af5fa was verified against all 419 manifest entries and Git tree 89fbeec38e584996ed93b9e21fbfc168fb149f94. The binary-safe consistency delta was applied and verified as tree 4e7db2539b81baa9c6bb37b1dc5a5786433d71ce, the exact content of 5db010a2beb74e79cceed1e4326067c8b690bd84. Local imported-base commit: 52ac277. The separately staged lifecycle descendant 062a4143 is not included. Apply this delta serially in an isolated descendant of that owner-approved base; do not overwrite or publish a frozen worktree.

Approved source workbook: PPG-Quality-Daily-End-of-Shift-Report-No-Start-Here(1).xlsx, Library ID libfile_fde3_f-78a17c72e2608191b9fde25b2179baa6_30f716c0c5048191a1ca903407e662da, SHA256 9d43392f7a6869b38afee62a56bf502be963ca26bb7d46543415c08555be69bf. Summary D14 controls the business date. The native XML cleaner produces bundled daily-clean.xlsx SHA256 f156d383280f4e89ac3e35a21abffec17d3245f94a24ec6f1f1c33a1ebb85800. The selected file is a populated sample, not an approved clean master. The cleaner and contract clear sample records, shared strings, 11 defect images, headers/footers and caches while retaining both branding images and exact native styles, names, merges, panes, validation and print setup. Independent layout and native PDF audits passed; this does not authorize a real report.

## Workflow and boundaries

1. An authenticated, nonvirtual administrator opens /source-review and selects an already verified registry identity and business date. The source picker uses saved exact IDs, or a uniquely matched cached site/list/client identity; it does not search the provider or infer the site from job names.
2. Read only the exact site's list metadata, columns and complete observed item pagination. Preserve raw page envelopes, IDs, versions, field values, date/schema receipts and selected-day records privately. Other-day source data is never placed in the workbook or returned in the review UI. Terminal traversal is not evidence of shift completion or future submissions.
3. Explicitly map fields and every observed shift value. Review missing/null/blank versus zero, quantities, date coverage, duplicate keys, identifiers and supported photos. A correction is a typed evidence/actor/time/hash-bound overlay. Raw source bytes do not change. Unknowns are blocking, not cleared by acknowledgment.
4. Review manual header metadata and representative photos, then approve the exact source/mapping/overlay/header/template/generator binding. Labor and inspector fields are not mapped in this inspection-only pilot, not proved absent from the source.
5. Populate the pinned selected template, reopen saved XLSX, verify values, row provenance, totals, formulas/caches, identifiers, date/shift representation, images and layout. The held workbook is saved privately through the existing durable Files/settings store. **No ordinary SpreadsheetInbox row is created.** There is no client/member inbox exposure, ordinary approval, descendant, period/VIN/aggregate incorporation, or release operation.
6. Download only through /api/source-review/download with the review ID and expected file SHA. The server reauthenticates the administrator, resolves state to the saved path, checks exact bytes and uses the HELD filename. Source-review links require sign-in; they are not public share links.
7. A final current-source verdict requires a new comparison started after that exact workbook was saved, bound to its hash, and still inside the existing ten-minute window. Initial capture freshness or a refresh started before generation cannot satisfy it. Changes reopen content approval and clear stale corrections/header/photo choices; prior states remain in history. Unchanged semantic content renews freshness without resetting content approval. Current-source-with-overlays is distinguished from raw source equality. Release always remains held.

Pending operation IDs are persisted before captures. Reusing a decision/generation ID with changed payload is rejected. A request-local native PgSession depth guard keeps inner inbox operations as savepoints so that private Files bytes and operation state commit only with the outer PostgresWorkspace session. Accepted inner transactions are not reported as success until the outer store exits and a new authoritative session reads state and file bytes. Unknown transport/save outcomes are not automatically retried.

## Deliberate capacity and Date limits

The isolated expansion described in SOURCE_FIRST_DAILY_EXPANSION.md supersedes the template-only row/part/rejection limits below after its separate native verification and integration review. Source capture, Date, privacy, freshness and release limits remain unchanged.

- Selected template limits: 13 first-shift rows, 5 second-shift rows, 5 third-shift rows, one part number, 13 rejected-record ranking/traceability rows, 11 chosen defect-photo slots. Any overflow blocks generation. There is no truncation, automatic splitting or multipart workaround.
- Capture limits: 2,000 scoped rows, 50,000 list items, 100 pages per collection, 8 MiB records per collection. Photos: at most 12 attempted associations, including failed/duplicate-image attempts; 2 MiB per image and 8 MiB total. Only supported PNG/JPEG bytes from exact bounded attachment routes are accepted; unavailable/unsupported/unattempted photos remain explicit. No generated photos or guessed association.
- Only the exact internal column Date with dateTime.format=dateOnly is supported. Every capture, including literal YYYY-MM-DD dates, requires a current exact site/list/Date-column GUID/regional runtime contract before items are accepted. A literal YYYY-MM-DD is then interpreted as a calendar date. An aware timestamp requires that same current exact site/list/column runtime contract proving the supported Central Time regional settings and local-midnight encoding, including DST conversion. The retained core contract verifies timezone Id 11, exact description/bias values and column GUID; other zones/encodings are held. Nonmidnight, naive timestamp, fractional nonzero seconds, missing/invalid Date, Date0, Created/submission fields and other business-date columns are not fallback evidence. Unknown dates anywhere in the list leave a coverage blocker, because their inclusion in the requested day cannot be ruled out.
- A sealed snapshot can be reviewed after its live/date receipt expires. That does not keep it current indefinitely. Final live comparison must recapture valid metadata/versions and match the reviewed semantics.

## Recovery is not resumable capture

HTTP failures retain private page envelopes, the exact remaining cursor/status and the provider Retry-After without clipping. The exact site/list gets a durable not-before hold; unknown deadlines require verified recovery. This pilot has no continuation operation, no global limiter, no auto-retry, no background queue and no timed execution. After a known cooldown, an explicitly initiated new capture starts a new full traversal. It does not resume the old cursor. An unconfirmed request stays pending and needs receipt reconciliation; the UI does not claim it failed safely or silently replay it.

The synchronous bounded traversal can still exceed hosted request duration. Large real-source use needs persisted bounded work and continuation before activation. Do not describe this pilot as resumable or scheduled. No one-hour/lunch/SMS feature is activated.

## Verified expansion plan for real jobs

The current guards deliberately mean dozens/hundreds of rows will not fit. The next implementation requires these separate gates, in order:

1. **Layout engine and shift detail expansion (largest dependency).** Build a deterministic destination map from all reviewed record IDs before writing. Clone styled detail rows for each shift, move totals/notes below data, translate relative formulas and all cross-sheet references, preserve row styles/merges/data validation, extend ranges/defined names/print areas/repeated headers and explicitly place page breaks. Keep every source record once. Test capacities 0/1/5/13/14/100/500/2000 across all shifts, exact totals and native PDF pagination. One or two focused implementation/review passes plus a full native rendering matrix are needed; a simple insert_rows change is insufficient for OOXML drawings and chart references.
2. **Summary, ranking and charts.** Replace fixed _Calc row regions with mapped bounded ranges. Rank by quantity then stable record ID without LARGE/MATCH tie duplication. Permit more than 13 rejected rows in traceability and label a genuine top-N visual separately, never presenting top-N as all rejects. Expand chart formulas/caches/anchors and native print controls together. Test ties, zero rejects, missing quantities, empty shifts and maxima.
3. **Multipart summary.** Replace the single Summary part section with a reviewed per-part table or a separately approved appendix, maintaining all-source total/shift totals and exact part strings. Do not create one report per part without an approved scope policy. Template layout approval and sample native PDF acceptance are prerequisites, in addition to calculation tests.
4. **Photos and traceability pages.** Separate the complete reject traceability list from bounded representative photo pages. Generate deterministic page slots, relationships, anchors and internal hyperlinks for selected supported photos only. Preserve original bytes and exact record/source-field provenance, distinct missing-photo statements and explicit budgets. Test repeated bytes across different records, >11 photos, unavailable images, ties, all shifts and page breaks.
5. **Labor/inspectors and operational completion.** Inspect approved actual-source schema before defining these mappings. Distinguish row measurements from repeated shift totals; define aggregation/deduplication evidence without silently merging records. Obtain a reviewed layout/meaning contract. Do not infer hours, staff, actual scan occurrence, lunch or finished/all-submitted status from gaps, counts or Date-only fields.
6. **Resumable production capture.** Persist exact scope/schema/auth context, continuation token, raw page chain and durable attempt receipts under bounded per-request work. Preserve provider cooldown, uncertain request status, cancellation and concurrent claim safety. Then perform final consistent-source/version comparison, including newly added/deleted rows and changed metadata/photos. This is a separate execution/recovery feature, not solved by increasing page caps.

Each pass must keep the present holds until its own workbook semantics, native pagination, outer-store concurrency and post-generation current-source comparison tests pass. No credible exact completion date is established before this expanded design and actual-source fit are reviewed.

## Notification handoff readiness, not transport

The private receipt already provides exact scope/assignment, source and binding hashes, saved report SHA, verification result and current-source state; /source-review#<review_id> is the app-relative review destination. A parent may resolve the verified hosted origin after deployment. This code has no dispatch claim, scan-occurrence field, shift/lunch completion policy, timer, recipient routing, SMS provider or attachment delivery. Those must follow the separately approved notification contract and user-authorized destination. Do not send a public/private download URL as a native attachment or equate tool acceptance with delivery.

## Test and release gates

Install requirements.txt in an isolated environment, plus test-only pytest and pypdf. Run:

    .venv/bin/python -m unittest tests.test_source_first_daily -v
    CI_LOCAL_DEV=1 .venv/bin/python -m pytest -q --ignore=tests/test_site_timezones.py
    node --test tests/test_*ui.js

Native PG gate, serially on a separately marked disposable local cluster only:

    CI_PG_TEST_URL=<disposable URL> .venv/bin/python -m unittest tests.test_source_first_pg -v

The test uses tests/pg_safety.py before resetting the disposable database. Never point it at hosted/production data or another worker's cluster. It exercises actual PostgresWorkspace outer commits, private Files bytes, rollback, restart/readback, concurrent edits and freshness reopening. Cloud has no initdb, so these tests are supplied but not claimed passed.

Browser gate: install Playwright in the test environment and its official browser, then run tests/browser_source_first_daily.py. SOURCE_FIRST_CHROMIUM may specify an existing Chromium executable. The script uses a synthetic local HTTP server, real authentication, a verified synthetic registry, capture/mapping/review/generation, restart, mobile overflow and lost-response reconciliation. Cloud Chromium failed before loading a page with socket() Operation not permitted; no browser assertion or screenshot is claimed passed. Do not bypass that environment restriction.

Independent local safety review passed admin-only exact-byte download, assigned-member denial, no ordinary inbox/list/detail/download/aggregate/VIN exposure, source-version reopening, post-save comparison timing and bounded photo cooldown/attempt behavior. Required before any hosting: merge with the owner's latest lifecycle work, run native PG/browser gates, package/import proof including bundled assets, explicit deployment permission, authenticated hosted smoke test and one specifically coordinated representative real-source pilot. Dynamic row/multipart support is still a prerequisite for larger actual jobs. No production activation is part of this deliverable.

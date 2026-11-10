# Checked-copy completion

The report response computes `checked_copy` from the complete server finding inventory before pagination. This is read-only and is not approval, release, or sending.

A checked copy requires the exact requested current working leaf and current saved checker/check identity, valid saved check time, complete listed findings, no non-informational unresolved findings (including ignored, deferred, AI-set-aside and findings corrected only in a different version), no verification gaps or report/source holds, complete compatible part quantities, and the canonical current verified exact-source receipt. An explicitly selected valid branch leaf may qualify; an unresolved ancestor choice never selects a sibling. Historical corrections absent from the current check are not current errors.

Source evidence expires at the earlier of the canonical ten-minute capture deadline and any source-date metadata deadline. Browser expiry clears the card and header claim locally, with no network or provider call. Refresh/recheck is explicit. A missing or malformed timestamp fails closed.

The UI shows `0 remaining detected errors` and `Download checked copy` only for that qualified saved version. This covers available checks and current exact-source comparison, not every possible workbook defect. Zero detected errors with incomplete evidence remains `Verification not complete`. Informational gaps are not mislabeled workbook errors. Pending edits or changes to the current view remove the checked action. Ignoring a finding does not fix it.

The opt-in `checked=1&check=<saved check hash>` mode on the existing exact download route revalidates current access, graph, check and evidence before and after loading the original stored bytes. A stale result is refused. The browser shows refusal text and never saves an error JSON as a workbook. Successful downloads retain the response filename and exact bytes. Existing ordinary downloads, originals and version history remain available under existing access rules.

## Synthetic verification

- Actual completion predicates: missing/truncated coverage, 29 unknown rejection rows, informational gaps, quantities, time expiry, malformed metadata, old checks, ignored/deferred/kept/AI-set-aside cases, read-only/superseded/forked versions.
- Actual report-view method: full 1,201-finding count despite a 20-finding page.
- Actual download route: opaque denied access; exact requested identity; stale check/graph/expiry refusal; revalidation after byte retrieval; ordinary download preserved.
- Actual SQLite storage/parser/server methods: valid generated XLSX, exact bytes/MIME/filename, unchanged originals and saved history. Exact-source receipt and source assignment are synthetic fixtures, not live verification.
- Full production UI in loopback browser at 1200 and 390 pixels: valid synthetic XLSX bytes, filename/MIME, readable refusal/no file, replaced/mismatched receipt, expiry clearing every success claim without requests, original bytes preserved, no external or unexpected requests. Existing POST fixture reads for clients/sheet/fixes are allowlisted, not writes.

No live reports, source captures, provider, credentials, settings, grants, deployment, approvals or sending are exercised. Full native PostgreSQL aggregate and combined source-refresh integration are separate parent-owned checks.

Checked downloads return a versioned X-PPG-Checked-Copy receipt with the exact file/check identities, ready=true and the current proof expiry, plus private no-store caching. The browser requires that receipt before and after reading the body. Missing, malformed, old-protocol, mismatched or expired receipts refuse the checked download without retry, including ordinary 200/MIME responses from older servers that ignore checked query parameters. Ordinary downloads do not add this receipt.

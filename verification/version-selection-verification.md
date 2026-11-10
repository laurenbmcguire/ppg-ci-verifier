# Existing-version navigation and complete selection

Base: `0762cb78bd5cac4cce9fb4589d86641a1a8e1417`.

## Change contract

- Exact corrected bytes may reopen an authorized version with proven shared ancestry. Proof uses append-only stored parent records, rejects missing/conflicting/cyclic histories, and never uses filenames or root-hash hints. It does not choose the chronologically newest branch.
- Reuse does not modify any version, parent link, review decision, source metadata or audit history. Browser and service API/MCP paths skip mutation-capable source/baseline follow-up checks. The response distinguishes existing content from a new write and separates the requested transformation from historical corrections.
- Browser correction authorization refreshes the existing session, checks both current parent and candidate before storing bytes, and refreshes destination access before exposing the response. A denied fresh candidate creates no report or artifact. Postcommit access loss returns an explicit committed outcome without a destination hash or metadata. Existing service-key scope is unchanged.
- Finding pages bind file, check, run, decision revision and both counts. Missing, duplicate, oversized, wrong-offset/list and mixed-snapshot pages fail closed before exposing a combined inventory.
- Every problem is reachable in 25-item pages. Select-all and explicit exclusions keep their exact scope across all pages. Genuine unsaved manual text survives paging/redraw; untouched suggested prefills are not silently staged.
- Existing lost-response/no-auto-retry, committed-save receipt, original-byte preservation, same-cell conflict, atomic source changes and explicit-preview safeguards remain.

## Verification and limits

Before freeze: 191 Node tests; 31 focused Python/SQLite/native-Postgres cases plus 3 subtests; 14 actual-handler headless-browser flows passed. Browsers used 1200px and 390px viewports and synthetic workbook/provider data only. The 1,301-item flow inspected every key, excluded the final item, and sent exactly 1,300 selected IDs in 11 bounded requests. Manual drafts survived paging, collapsing and selection changes.

The final frozen-commit aggregate and exact SHA are retained separately in the sibling `version-selection-evidence-20261110` evidence directory, together with raw logs, historical failures, screenshots and the review handoff. Historical runs include an incorrect local-dev/TMPDIR invocation, a disposable-cluster role mismatch and the repaired domain-exception classification; they are not final pass evidence.

Full Python collection remains blocked by the inherited `tests/test_site_timezones.py` import of missing `hosting.site_timezones`. The documented aggregate excludes that one module rather than inventing timezone mappings. Native tests use only the existing marker-verified local disposable PostgreSQL socket, with no TCP listener. No live workbook/provider/email/settings/credentials/schema/deployment was modified, and no push was performed.

Reproduce with an isolated test HOME/TMPDIR, CI_LOCAL_DEV=1, VERCEL=0, no inherited credentials, the project's test Python environment and Node:

- `python -m pytest -q --ignore=tests/test_site_timezones.py` (set only the verified disposable CI_PG_TEST_URL for native tests).
- `node --test tests/test_*ui.js` and `node --check web/app.js`.
- `python tests/browser_ai_save_outcomes.py WIDTH MODE` for widths 1200/390 and modes success, refresh-fail, backend-reject, lost-response, preview, reuse.
- `python tests/browser_complete_selection.py WIDTH` for both widths.

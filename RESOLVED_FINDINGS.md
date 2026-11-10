# Saved corrections and open findings

Base: 5db010a2beb74e79cceed1e4326067c8b690bd84. Synthetic local validation only. No source retrieval, live report edits, approval, sending, push or deployment.

## Reproduction and repair

The real checker can flag a negative quantity that is also stored as text. Converting it to a number saves the cell correction but leaves the negative-quantity finding open. The previous completion dialog nevertheless said “Dex fixed everything you selected,” and the active corrections panel repeated saved cells as a struck-through list.

Saved cell changes are now distinct from resolved findings in the dialog, panel, counts and downloaded summary. Resolution requires the exact saved transformation, the exact child/check identity, a current working copy, and complete bound finding pages. A same-rule finding on the same cell is still unresolved even when its key or fingerprint changed or it was set aside. Missing, stale, truncated or malformed evidence cannot prove resolution. Source findings require current, unexpired source evidence without hard source holds. Applied standalone suggestions are counted separately from resolved detected findings.

The active list continues to use the newly loaded server check. No optimistic deletion, local dismissal, change to approval/holds, or rewrite of audit data is introduced. The repeated saved-cell list is removed from that panel. Full exact saved changes remain in collapsed completion details and paginated History. Saved responses survive read failures without retrying mutations. The existing navigation lock remains in place, and late completion cannot redirect a newer view or clear another report's selection. A confirmed scoped preview and its completed editor are removed synchronously before opening the saved child, so their already-saved inputs cannot trigger the unsaved-draft guard. Failed saves and canceled previews retain their inputs.

## Verification

- All 303 Node tests pass, including exact receipt, partial correction, changed key/fingerprint, dismissed/AI-set-aside finding, incomplete/stale/malformed check, source expiry/hold, unrelated version, existing-child reuse, unsupported row and standalone suggestion cases.
- Focused Python: 172 passed, 1 PostgreSQL-specific test skipped, 4 subtests passed. Independent review also included the PostgreSQL working-copy module: 172 passed, 4 PostgreSQL-only skips, 4 subtests. Selected mechanical corrections, selected AI proposals, working-copy lineage, finding accounting, checked-copy and correction-workflow modules.
- New actual-handler/Chromium suite at 1200 and 390 pixels: partial same-cell failure, complete correction, standalone suggestions, manual preview/cancel/save, failed saved-report refresh, navigation lock/return, reloaded open counts, and full saved history. Real workbook checks and fake isolated storage only. Originals unchanged, one saved child per correction, unrelated findings and source holds retained, zero external requests.
- Inherited supported-correction, direct/scoped AI and success/refresh-failure/lost-response/backend-refusal/provider-failure/preview browser scenarios pass at both widths.
- JavaScript syntax and diff whitespace checks pass. Screenshots inspected.

## Limits

The inherited browser_direct_review_actions harness times out while navigating after a decision, before its manual-save step. The same failure occurs on untouched 5db. The new independent manual scenario waits for completion and exercises the actual guarded save successfully. The inherited existing-child browser fixture remains unsuitable because it tries to create a branch from a superseded parent; reuse is covered by exact receipt/identity unit tests rather than claiming that fixture passes. No new PostgreSQL aggregate, hosted or live-provider validation is claimed.

## Reproduce locally

Use an isolated HOME and TMPDIR, CI_LOCAL_DEV=1, VERCEL=0, PYTHONDONTWRITEBYTECODE=1 and the test Python environment. Set PLAYWRIGHT_MODULE, CHROMIUM_EXECUTABLE and EVIDENCE_DIR to the local test installations/output folder. No database credentials or source credentials are needed.

- `node --check web/app.js` and `node --test tests/test_*.js`
- `python tests/browser_resolved_findings.py 1200` and `python tests/browser_resolved_findings.py 390`
- `python tests/browser_ai_save_outcomes.py WIDTH MODE`, where WIDTH is 1200 or 390 and MODE is success, refresh-fail, lost-response, backend-reject, provider-failure or preview.

The lifecycle harness blocks external sockets and browser requests, uses actual request handlers with isolated fake storage, and sets finding page size to two so full inventory loading is exercised. It does not replace correction validation, findings, working-copy checks, or source holds.

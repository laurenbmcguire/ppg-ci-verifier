# Selected supported corrections repair

Base: 611182b83523943cd8bc6fa5e531fa368bdc4219. Local synthetic validation only; no deployment, live report writes, or real provider calls.

## Behavior

Selected findings with fingerprints can use existing exact mechanical corrections without model inference. The server recomputes candidates from original bytes and current findings. Only sure Clean-up, Number as text, and case-only sure Spelling qualify; source-owned cells, decisions, guesses, coordinated or ambiguous proposals do not. Existing source-backed explicit correction behavior is unchanged. The same target, before/after, kind, provenance and finding identity are checked again during guarded save under the lock. Caller source/atomic metadata cannot be normalized away to evade this validation.

Mixed selections retain validated mechanical corrections if the fake provider omits an answer or fails. Existing unknown-save and no-retry handling remains unchanged. Unsupported row/report findings receive a precise preflight explanation without a provider call. No neighboring-row guess is elevated to verified data.

Corrected children now retain an earlier source receipt only as stale, unverified history, bound to the old workbook. This preserves outstanding hard source requirements without carrying verification to changed bytes. Review, source proposals, PDF receipt and approval fail closed; a later failed source check remains held. Parent evidence and bytes remain unchanged.

## Tests

- Focused Python: 104 passed, 2 PostgreSQL-specific tests skipped, 17 subtests passed. Modules: selected_mechanical_corrections, selected_ai_proposals, ai_proposal_guards, change_summary_binding, r8_pdf_transactions.
- All existing Node UI unit tests: 222 passed.
- New actual-handler browser suite: four scenarios at 1200 and 390 pixels passed. Multi-cell numeric/spacing mechanical corrections (zero provider calls), mixed mechanical/nonmechanical fake-provider correction, unsupported row, committed save followed by two failed read refreshes. Original bytes, exact diffs, one v2/no retry, stale source hold, no approval/send, zero external traffic verified. Screenshots inspected.
- Inherited direct and scoped apply browser tests plus success, refresh-fail, lost-response, backend-reject, provider-failure and preview outcome modes passed at both viewports. Provider-failure fixture now uses genuine nonmechanical comments so it still exercises a provider failure.
- Syntax and git diff whitespace checks passed.

## Limits

- Exact user's complaint report and transient skipped response were not identified; this repair addresses independently reproduced defects, not a proven attribution of that incident.
- Fingerprinted mechanical requests retain the existing AI-enabled UI gate. Provider-disabled UI support is not claimed.
- The inherited reuse browser fixture fails before browser launch because it creates a new branch from an already superseded parent. Same failure reproduced on untouched base611182b. Working-parent protection was not weakened. Reuse is not claimed passed.
- PostgreSQL combined integration gate and final combined checked-copy UI validation belong to integration; not performed here. Old base UI can still display FINAL wording despite source holds; the separately reviewed completion change replaces those labels.

Evidence outside the repository: ../dex-supported-python-focused.txt, ../dex-supported-node-all.txt, ../dex-supported-browser-regressions.txt, ../dex-supported-browser-reuse.txt, ../dex-supported-reuse-baseline.txt, ../dex-supported-evidence/.

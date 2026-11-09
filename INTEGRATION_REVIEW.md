Current status: local candidate, unmerged and not live. The chronological evidence below includes earlier checkpoints; the current checker is v14. Delivery remains disabled. The separately documented Outlook ledger is an offline foundation, not connected to production authentication or sending.

# Updated source and safety integration

This isolated integration branch merges updated source `7de92e9485738bb6a8988269d1b7b6cc1fac857c` with safety candidate `2c387d4f602656bc0efa6bb81956148fba1aabcd`, based on `1c95e05c112927895581ad40217c03c018fccbdc`. The two saved source branches were not rewritten. This is local integration evidence, not a release or deployment claim.

## Deliberate conflict resolutions

- Kept newer transactional `_store_new` and consistently ordered parent/child PostgreSQL writer locks. Combined them with required displayed `check_hash`, approval/sent guards, duplicate-cell/change-set validation, rejected/deferred data protection, full correction metadata and atomic lineage save.
- Kept newer classic comparison identity in `check_run` and source validation covering classic, embedded export and live reconciliation. Removed an overlapping older reconciliation-only check that incorrectly rejected a freshly reviewed classic proposal.
- Kept decision invalidation and assignment identity from the newer source. Added strict displayed approval revision/run, whole-state approval fingerprint and idempotent replay safety.
- Kept newer `seen`/`run` request names as compatible aliases. Every non-replay inbox decision requires an exact displayed run and revision; non-undo decisions also require every displayed finding fingerprint. Undo is explicitly covered by stale/missing run regression. UI sends immutable `expected_fps`/`expected_run` snapshots.
- Retained file-sniff navigation locks, scoped review forms and rendered correction-plan snapshots.

## Generic findings and observed values

The checker now attaches typed observed local data to every internal finding: one cell when precisely located, the observed row for a row finding, and conservatively the file hash when unlocated. Generic wording no longer makes changed numeric text look unchanged. Unknown data is not treated as a known blank. Checker version is `2026-11-08-observed-finding-v13` so old checks must refresh.

Full-workbook regressions demonstrate that a rejection of numeric text `072` does not hide changed `075`, while an unchanged typed cell on the same verified record can retain its decision across an unrelated new export. UI exposes available observed values and distinguishes unavailable evidence from blank cells.

## Capability decisions

Enabled: observational upload/checking, explicit scoped finding review, reviewed manual cell corrections into a separate version, internal approval with strict displayed-state binding, original/approved downloads.

Paused: automatic cleanup, automatic/AI workbook changes, automatic-change reverts, unbound legacy decision writes, row removal/relocation, API accept-all automatic changes, sending and draft creation. Delivery is also blocked at reservation and record entry points. Opening approval does not read a mailbox.

The new delivery implementation does not resolve provider-accepted/database-failed outcomes durably and expires reservations after 300 seconds. It is retained as unreachable historical implementation behind fail-closed guards, not endorsed as a complete send contract. Restoring delivery needs durable unknown-outcome reconciliation and independent acceptance tests.

## UX corrections

- Removed claims that automatic cleaning or sending are available, and the claim that every layout works.
- Approval details explain that sending/drafts are paused, rather than offering inactive recipient forms.
- Unlocated findings do not navigate to a misleading sheet/cell; unknown evidence is explicitly labeled.
- Preview wording does not promise source comparison when unavailable.
- Removed duplicate Reject suggestion/Keep as recorded actions and reveal the selected correction card.

## Evidence and limits

Intermediate failed runs are retained alongside final runs, rather than overwritten. The updated race tests retain 39 cases, adapting successful automatic/provider workflows to explicit no-write/no-provider barriers while preserving stale-source, lock-race and reviewed-manual success coverage. PostgreSQL concurrency coverage remains active against only the designated disposable local cluster.

No production data, live report, provider/model call, remote push, deployment, credential or account configuration change occurred in this integration task. Missing r8 reliability integration and isolated hosted staging/recovery remain separate release blockers. Final release gates belong to the parent review.

## Completed verification

- `verification/integration-2026-11-08/integration-full-final.log`: 900 passed, 166 subtests passed, one LibreOffice skip, 132.87 seconds. This run included the designated disposable PostgreSQL cluster, SQLite, API/MCP, race, atomic rollback, approval, request-guard and observed-value coverage.
- `integration-node-final-ui.log`: all 38 Node/UI guard tests pass on the final UI source.
- `integration-final-contracts.log`: 8 API/MCP/observed-value tests pass after the final tool-description and proposal-only UI clarification. Those final edits changed descriptions and UI presentation, not backend action logic.
- `integration-focused-pg.log`: all 32 focused PostgreSQL/API/MCP/send cases pass.
- Original failed integration runs are preserved in the same directory, including the first race/backend run, non-PG aggregate, and intermediate full PG aggregate. They are diagnostic history, not final release evidence.
- Independent browser UX review ran on synthetic loopback data. It confirmed corrected unknown-versus-blank labels, no misleading unlocated sheet jump, preview/cancel/select behavior, and required-reason deferral. Parent owns the final desktop/mobile browser evidence and release decisions.

Reproduction: use the existing test virtual environment in a fresh environment with `CI_LOCAL_DEV=1`, `VERCEL=0`, `PYTHONDONTWRITEBYTECODE=1`, `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1`, temporary directory set to the authorized test directory, and `CI_PG_TEST_URL` set only to the designated disposable local socket cluster. Run `python -m pytest -q -p no:cacheprovider` and `node --test tests/test_*ui.js tests/test_request_guards.js`. No dependency installation was needed.

## Follow-up independent review hardening (after 1db3fc6)

The bounded static review and independent frozen-head reproductions found additional gaps. These follow-up changes do not enable delivery or automatic workbook mutation:

- Historical `accepted_issues` cannot suppress findings unless they carry the exact current fingerprint and check run. Unbound personal records remain available as history/notes for fresh review; they do not settle even an unambiguous cell. AI acceptances never suppress a high-severity finding. Accepted records do not carry into corrected children.
- Source-backed corrections must match a server-recomputed local source proposal, including source snapshot/item/field, before/after and change-set metadata. An intentionally different value must be labeled as an explicit manual edit, not as SharePoint's value.
- Formula, hyperlink, aggregate and other potentially nonlocal findings conservatively bind the whole workbook SHA-256. Only missing/invalid/numeric-text/negative/fractional local cell findings retain typed-cell carryover, and formula cells in those categories still bind workbook bytes. This covers changed cached values and relationship targets without pretending to know all dependencies. Checker version advances to `2026-11-08-dependent-finding-v14`.
- Each decision request ID binds a digest of the normalized action, finding IDs, reason, actor, owner, evidence, displayed revision/run/fingerprints and undo target. Exact retries return the original result despite subsequent state changes; changed payloads and old unbound request IDs conflict instead of reporting a misleading success.
- New approval requires the current checker. Historical approvals from an older checker display stale even if internally coherent.
- Explicit AI proposal preparation requires authorized report access and a displayed check hash, current checker and unfrozen report. Those conditions are checked under the report lock before preparation and before recording asynchronous output. Stale output is discarded. Suggestions update only review state, never workbook bytes or a child version. App response includes a refreshed report; public API/MCP/direct preparation require the same binding. All tests use synthetic model/provider fakes.

The synthetic SL2200 accounting fixture now explicitly constructs exact-bound historical acceptances where preservation is intended. Separate tests retain and verify unbound records stay open, so old unsafe assertions were replaced with stricter safety assertions rather than skipped. Independent `test_static_review_regressions.py` demonstrated all seven cases fail on frozen 1db3fc6, then pass with the follow-up fixes.

Follow-up backend evidence:

- `review-followup-full-first.log`: 931 passed, 166 subtests passed, one LibreOffice skip, one failure. The failure was a stale-source refusal returning the new proposal-mismatch wording before the existing freshness wording; no stale correction was applied.
- Freshness validation was then moved before proposal matching. `review-followup-focused-final.log`: 38 passed, one LibreOffice skip, covering source corrections, forged source metadata, cached formula/hyperlink/aggregate fingerprints, request-ID payload conflicts, AI proposal access/races and all seven independent legacy/approval regressions.
- `review-followup-third.log`: 56 passed for accounting, decisions, observed-value, retry, source and independent review regressions.
- All original failed follow-up logs remain available. A first test parametrization printed synthetic workbook byte literals as pytest case IDs; later runs use readable named IDs. No real workbook data or credentials appear in these synthetic fixtures.

The parent will run the final aggregate from a detached worktree after the separate UI and standalone ledger work is committed, so concurrent edits cannot be mistaken for exact-head certification. The existing send/draft barriers remain unchanged.

## Final interaction and display follow-up

The review begins with AI prepares suggestions, Review manually, or Leave file unchanged. Suggestions are never selected or applied automatically. A correction has one inclusion control and an explicit value editor; source decisions remain separate. Removing a typed change clears both manual and proposal representations. Preview is a singleton, permits per-change cancellation, and shows sheet plus cell. Download retrieves the stored version rather than silently applying edits. Original versions are preserved.

User-facing assistant labels use Dex. Provider identity, connector instructions, internal model names, source provenance and historical decision records remain unchanged.

Independent frozen checkpoint ab0ab9354f835481625dcc3033b7d0d2fb475c37 passed 932 Python tests plus 166 subtests, with one LibreOffice skip; 42 Node tests; real Chromium at 1440 and 390 pixels; and synthetic two-process SQLite HTTP imports/restart. The later display-only update passed 44 Node tests and the same browser smoke. The offline Outlook foundation independently passed 17 tests plus 34 subtests, including spawned-process dispatch contention and both writer-lock orderings. The final combined source will be tested again from its own frozen checkout; final evidence and checksums are supplied with the handoff, rather than overwriting earlier failures.

## Final independent UX and transport findings

The independent browser audit found an inherited Ask Dex pane still offering bulk fixes, automatic chat selection, undo/redo and an enabled Send field while AI was unconfigured. Those alternate UI execution paths are removed. The pane explains configuration and points to the one guarded preparation/review workflow. Historical records and backend provider identity remain unchanged. Review-request and completion wording distinguishes internal recording/approval from paused email delivery.

Preview styles initially used inline attributes that the existing Content Security Policy correctly blocked. They now use app.css classes; the security policy is unchanged. Real Chromium checks computed sheet display, cell and Remove no-wrap, and visible sheet/cell at desktop and phone widths, not merely attribute presence. The focused UI suite passes 46 tests.

A frozen 3277110 aggregate had 948 passing tests, 200 passing subtests, one LibreOffice skip and one HTTP transport failure. The legacy local app/server.py returned 403 but omitted response framing, causing intermittent macOS resets with unread rejected POST bytes. A bounded original-source reproduction had 10 resets in 200 refused requests; the inbox stayed empty. The narrow fix adds exact Content-Length and connection-close framing without weakening token/host/origin policy or consuming rejected input. Patched stress: 400 of 400 complete 403 responses, no resets or writes. Fifteen focused tests plus two subtests pass, including immediate rejection of a declared 1 MiB body that is never sent. Failed and successful logs are preserved.

The full frozen f9b4ad0 aggregate passed 950 Python tests and 202 subtests, with one LibreOffice skip. Independent phone inspection then found the preview table squeezed values into letter stacks. The CSS-only follow-up gives the table and value columns sensible minimum widths inside the existing keyboard-focusable horizontal scroller. Browser regressions now check actual column widths, bounded row height and keyboard horizontal scrolling at 390 pixels. No security policy, provider path or data behavior changed.

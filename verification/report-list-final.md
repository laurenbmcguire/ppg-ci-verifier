# Report identification and selected review: integrated acceptance

## Status and source attribution

The report list, Needs review workflow, selected AI proposal preparation, and read-only Ask Dex are integrated. This is the final prepared project acceptance record. Earlier checkpoint documents retain their historical test results and limitations; they do not describe the current integration status.

- Original release base: `1740eda7436d38ab5a1618ecf1afa542f3be542c`.
- Full integrated functional gate: `9abffdd9ee59a8b6f70427a59e26e8085e6be7e1`.
- Reviewed runtime after singular-wording polish: `5581f9522325a61ef7394f4cf0b0b1ad1e04928a`. Its only product change from 9ab is “1 saved version matches” instead of “1 saved version match”; an inventory assertion checks the wording. This prepared-record commit preserves that runtime exactly.
- No live report or real provider was used in these acceptance tests. No remote push or deployment was performed here. Real-provider connectivity and publication are separate release gates.

## Delivered workflow

Report rows show wrapping file names, confirmed client or explicit ambiguity, job, report date, separately labeled upload time, file version, revision, short exact ID, original/corrected lineage, report quantities and separate source/check state. Unknown, partial and known-zero quantities remain different. Counts and search disclose the loaded-list limit. Same-name independent uploads are not merged.

To do includes unresolved source/client/check context. Client badges use the same predicate and count saved versions, not unique report families. An earlier version can retain its own open items even when a newer copy has no findings. Filtered earlier copies are explicitly labeled and provide exact links to every newer loaded same-family copy. Parallel branches are not declared superseded and their findings are not merged.

Needs review opens the loaded findings directly. Users can select by error type across groups/pages, by group, individually, all unresolved loaded items, or none. Selection is a review scope, not acceptance. Selected count and all eight actions remain accessible after deep phone scrolling. Missing coverage and zero selectable findings never imply readiness.

- Supported proposals: exact selected cells enter a preview, with removal and explicit Apply.
- Manual: selected-item navigation, typed edits, preview/cancel and explicit Apply.
- Ignore: a recorded keep-as-entered decision with reason; high-severity items require individual evidence. Defer records the source owner. These paths do not erase unknowns or source holds.
- AI: only explicitly selected finding IDs/fingerprints and relevant exact cell context are sent to Claude/Anthropic after the user chooses preparation. At most 120 findings are considered. Cell/request byte limits and source/hold guards fail closed. AI values are unverified suggestions, unchecked by default, with per-item inclusion, preview/removal and explicit Apply. No automatic broad-report fallback is used by these choices.
- Ask Dex: read-only questions using bounded relevant findings/cells/headers and qualified summary quantities. It cannot edit, approve or send a report. History and pending results are bound to report/check/context; unrelated navigation stays usable and drafts remain isolated.

Original workbooks stay byte-identical. Corrections create separate recorded versions. Passive opening, listing, fixes, extraction, uploads and ordinary rechecks do not call an AI provider merely because a key is configured. Unsupported layouts remain visibly unverified; no AI layout mapping button or automatic guessed correction is promised.

## Verification

Full gate on 9ab, clean before and after:

- Python: **1368 passed, 1 skipped, 289 subtests passed**. The sole skip is LibreOffice recalculation because LibreOffice is not installed. No software was installed.
- Node UI: **133 passed**, plus JavaScript syntax and diff-whitespace checks.
- Full-app inventory: 1200×900 and 390×900, including duplicate names, separate uploads, genuine lineage, missing date/client/version, partial/zero/unknown quantities, search/filter/Back focus, older-only attention/search and exact parallel-copy navigation.
- Chooser: 1200×850 and 390×850, two real error codes, cross-page/group/individual/none selection, sticky selected count and all eight actions after deep scroll, coordinate-click access, keyboard focus and Escape, stale-binding rejection.
- Actual-handler keep/defer/manual: 1200 and 390. Exact decision scope, remaining source hold, canceled preview without writes, then exactly one explicit changed cell in one corrected revision; originals and all other cells/formulas unchanged.
- Actual-handler selected AI with deterministic fake provider: 1200 and 390. Exact requested findings, unchecked proposals, first preview canceled without version creation, one proposal removed and exactly one explicit cell applied. No approval/send.
- Actual-handler Ask Dex: 1200 and 390, eight fake calls total, plain-text rendering, failure/retry, stale navigation/Back isolation, no external traffic and unchanged reports.
- Inherited request-guard browser: 1440 and 390. Inherited context/grid/draft/recheck browser: 1200 and 390.
- Async unit coverage includes preparation versus apply/recheck/decision in both orders, stale success/error, newer navigation, exact operation ownership, newly rendered controls, and preservation of unsent drafts.

The copy-only 5581 runtime then passed all **133 Node UI tests**, syntax checks and the affected full-app inventory browser at both sizes. The broader backend and workflow gates are attributed to unchanged 9ab code, not falsely claimed as rerun for a text-only change. Final independent source/usability review accepted that runtime with no scoped blocker.

PostgreSQL used only the already-marked project-disposable database, verified by read-only marker/identity checks before any test writes. It ran through its exact Unix socket on port 55439 with no TCP listener and no inherited production database/provider credentials. A stop trap and final status check confirmed it stopped. The first startup attempt failed before tests because macOS required a valid locale; retrying the same instance with LC_ALL=C succeeded. No marker/config change was made.

## Evidence and limits

Logs are in [report-list-final/](report-list-final/). Screenshots in its `screenshots` directory contain fabricated report data only. The original 321 checkpoint and failed earlier wrong-environment runs are historical, not final pass evidence. The one bounded Claude code/usability consultation reviewed its recorded earlier snapshot; independently verified issues were corrected and the final integrated source/flows were reviewed afterward. No raw private review prompts or customer data are included in this record.

This acceptance does not prove current production provider connectivity, production availability, unsupported-layout recognition, full source coverage for every report, or client readiness. Those states remain explicit in the product. Sending and approval safety rules are unchanged. The temporary connectivity diagnostic is not part of this core.

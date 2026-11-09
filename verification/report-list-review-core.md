# Report identification and review chooser: core verification

Runtime snapshot: 321c9967e897d49c89d2bc9b0f151a589045ba7a, based on 1740eda7436d38ab5a1618ecf1afa542f3be542c.

## Saved changes

- 7d6aeb7: wrapping report names, explicit report/client/job/day/version/file identity, truthful per-measure quantities, lineage-only grouping, loaded-list limitations, consistent To do contents, search and return focus.
- c35f98f: Needs review chooser with separate finding selection, error-type and group scopes, individual selection, all unresolved and none, supported-change preview, manual selected-item navigation, and guarded keep/defer decisions.
- 2d9b46c: actual full-app list-to-chooser regression.
- d1a2bd5: sticky modal actions and return-focus fallback.
- 321c996: explicit quantity units and completeness; unknown/incompatible rate suppression.

The list's Needs review action is a separate button beside the report-open button. It opens the selected file's loaded review items. Selecting findings does not accept changes. Only supported selected cell changes enter preview; high-severity keep decisions require individual evidence, unknown source holds remain, and original files remain unchanged.

## Passed checks

- 112 Node UI tests.
- 72 focused Python tests and 10 subtests using the sanitized project test environment: report list, report statistics, review summary, AI proposal guards and hosted auth boundaries.
- Full-app synthetic inventory and direct Needs review entry at 1200×900 and 390×900: readable wrapped identities, explicit unknown dates/values, partial vs zero distinction, separate independent uploads, true version grouping, keyboard focus return, retained search/filter, all/none review selection and Escape dismissal.
- Inherited full-app request-guard smoke at 1440 and 390 pixels: report open, finding selection, history/back, single preview, preview cancellation, manual cancellation and leave unchanged.
- Standalone synthetic chooser at 1200 and 390 pixels: error-type/group selection across pages, individual removal, none, guarded blocker keep, exact supported preview, stale binding, Escape/focus and manual scope.
- Synthetic test handlers allow only designated read-only routes, deny provider/network access, and assert the saved report and original bytes remain unchanged.

## Not established

This is not an integrated AI or release acceptance result. Ask Dex and selected-AI proposal code remain in separate branches, unmerged here. The chooser accurately states that AI changes are unavailable. Selected report findings/cell context must not be sent to Claude/Anthropic without the required approval.

No live report, live source, provider request, user browser, remote push or deployment was exercised by these checks. No PostgreSQL aggregate was run at this stage. Two provisional full Python invocations used the wrong macOS temporary-directory/auth fixture environment and hit inherited path/auth guards; they are retained as failed test setup runs, not a product regression result. Run the full aggregate once the integrated candidate is frozen using the established sanitized HOME/TMPDIR/CI_LOCAL_DEV/VERCEL configuration and separately coordinated disposable PostgreSQL fixture.

The independent visual review accepted the synthetic list readability at 1200/390. Final integrated usability review and code review remain required after any later integration.

## Post-snapshot attention-badge alignment

After the immutable 321c996 runtime review snapshot, a narrow follow-up changes only the client-tab/sidebar attention-count predicate to reuse `queueNeedsAttention`, exactly as To do does. Source/client uncertainty, missing reviews and incomplete finding coverage now count consistently; sent items and auto-hidden originals remain excluded. The original snapshot attribution above is unchanged.

A synthetic production-function test exercises both tab and sidebar badges for two clients plus Needs client, including source incomplete/stale, ambiguous/missing client, missing review, missing coverage, sent/clear/hidden records, and badge clearing after resolution. Eight list tests and all 113 Node UI tests passed; JavaScript syntax and diff-whitespace checks passed. No provider or report writes were invoked.

A separate Ask Dex owner confirmed a passive AI_LAYOUT path in report parsing/proposal preparation and is gating it independently with a default-off explicit parser permission. That privacy change is not integrated or verified in this branch. The final combined aggregate must include the passive-open/fixes/record-extraction no-provider regression, with provider configuration present in synthetic fakes, before release. No Ask Dex or selected-AI feature wiring was added by this badge follow-up.

## After-scroll chooser browser check

The existing sticky chooser implementation was exercised directly in headless Chromium at 1200×850 and 390×850 using 31 fabricated items with two distinct error codes. After scrolling the modal to its last group (173px desktop, 857px phone), the selected count and all seven actions were visible and hit-testable within the viewport. A direct mouse-coordinate click on Select none, without locator auto-scrolling, cleared the selection. Error-type/group/individual and cross-page selection, blocker evidence guarding, exact preview capture, manual scope, stale-binding rejection and Escape/focus also passed. No runtime change was needed for this check.

This remains a standalone production-chooser/synthetic-action test, not an integrated provider or persisted decision test. Final combined own-decision/manual/AI route verification remains pending the final integration and applicable sharing approval.

## Actual-handler keep, defer and manual correction

`tests/browser_review_choices_full_app.py` passed at 1200×900 and 390×900 against the complete app and real HTTP handlers backed only by an isolated fake workspace. The browser selects one finding to keep, records its reason, selects a second to defer to a named synthetic source owner, then separately selects one manual correction in another fabricated report. Preview cancellation saves nothing. Explicit Apply creates revision 2 with exactly one changed cell.

The wrapper verifies both recorded decision IDs/actions, the remaining awaiting-source-owner hold, original workbook byte identity, one corrected copy, every other cell/formula unchanged, and no approval/send. Mutating requests are restricted to those exact fabricated findings and the named synthetic cell. All non-loopback network requests and other provider/write routes are denied. Logs: report-choices-full-app-1200.txt and report-choices-full-app-390.txt beside the worktree.

Local Ask Dex and selected-proposal backend integration subsequently proceeded under explicit approval for relevant selected-report context to Claude/Anthropic. The privacy/context-minimization follow-up, scoped AI UI and final aggregate are still required before publication; this test did not call a provider.

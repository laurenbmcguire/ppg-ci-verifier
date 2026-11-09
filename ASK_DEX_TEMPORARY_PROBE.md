# Temporary Ask Dex connectivity acceptance test

Frozen core: `f44224bb783a0fda50d9c8ec8c307137c8164719`. This test-only change must be completely reverted after a single controlled synthetic connectivity request. It is not a permanent health check or a report-processing feature.

## Fixed request and safeguards

The only valid request is an authenticated, same-origin admin POST to `/api/admin/ask-dex-connection-test` with JSON `{"test":"fixed-synthetic-ask-dex-v1"}`. Other payloads, including report IDs or arbitrary questions, return 400 before any provider request. Ordinary browser origin, fetch-site, review-token and JSON-content-type checks apply. Non-admin and anonymous users cannot invoke it. Admin access is checked again before returning the result.

The server constructs a fixed fabricated CSV with ten inspected items and a blank rejected value. The question references only CSV!B2 and CSV!C2. The current minimized Ask Dex context builder sends those values and their headers, not the unreferenced identifier in A2 or any saved report. The probe does not read or write report storage.

It uses the existing Ask Dex provider transport, with a 128-output-token cap, no tools, and one outbound attempt without automatic retry. Normal chat retains its 2000-token budget and existing response shape. No credentials, access grants or provider settings are created or changed.

The response contains the returned model, a short synthetic-only answer and sanitized numeric input/output/cache token usage when supplied by the provider. Missing usage stays missing. The application does not calculate or claim a price. Provider errors are sanitized; raw response headers, exceptions and credentials are not returned.

## Release and removal procedure

1. Freeze and verify the final core commit, then add only this temporary commit for the controlled test deployment.
2. Invoke the valid marker exactly once using the existing authorized admin session. Record the model, returned usage, short answer and sanitized error/status. A timeout or uncertain outcome must not be automatically retried.
3. Revert the complete temporary commit, including this document, route, helper and tests. Confirm the resulting Git tree is byte-for-byte equal to the frozen core tree with `git diff --exit-code <core> <removal>` and matching tree IDs.
4. Deploy the removal commit. Confirm the final runtime corresponds to the frozen core.
5. Verify route absence with an authenticated, same-origin POST whose JSON body is **{}**, never the valid marker. The final core must return 404. An old edge still carrying the temporary route returns 400 before provider invocation, so this check cannot spend tokens. A 400 indicates removal is not yet verified; wait for rollout rather than sending the valid marker again.

## Offline tests

Focused tests use fabricated storage, fake providers or a fake HTTP transport only. They cover admin/auth/origin/content-type guards, invalid/empty payloads, no report storage, revocation, bounded minimized context, unknown blanks, sanitized usage/errors, single attempts on success and HTTP 401/403/429/529, and unchanged ordinary chat defaults. They do not establish live provider connectivity or deployment success.

Focused offline acceptance: 59 Python tests passed. The unchanged JavaScript aggregate also passed all 134 tests. No live request, push or deployment was performed by this preparation.

The required restored core tree ID is `52669b64cd1f1ae519e40b6801db758200ac929f`.


## Temporary normal-browser interface

Open `/api/admin/ask-dex-connection-test/page` on the same authorized verifier origin. The page and its external script/CSS require the existing admin session. The default CSP is unchanged: no inline script/style exception. It does not load the application bundle, report lists or report content. Authentication uses the existing session; the fixed POST uses the same-origin review-token convention, not a new credential.

The button explains the fabricated data and sends the same fixed request only after a click. It disables synchronously before sending, never enables a retry, and records only a non-secret attempted flag in sessionStorage so reloading the page cannot repeat the request in that browser session. The result is plain text. If browser session storage cannot record the flag, no request is sent. No answer or usage is persisted. Separate browser sessions can still invoke the route, so the controlled test procedure remains exactly one approved attempt.

Remove this entire UI commit and the earlier temporary route commit, newest first. The final tree must still be exactly the frozen core tree stated above. Page/script/CSS GETs must return 404 after removal; test POST route absence only with {} as described above.

UI acceptance uses the real loopback HTTP handler and headless Chromium at 1200 and 390 pixels, with a fabricated admin and fake provider. It covers no request on page load, unchanged CSP, one click, disabled pending/completed controls, plain-text output, reload prevention and uncertain network failure without retry. The Python route checks also deny anonymous/member requests for every UI asset. No live-provider call is part of these tests.

Keep the test page open through cleanup deployment. After the synthetic attempt finishes, its secondary “Verify test removal” button sends only `{}` once, under the same browser request guards. It cannot invoke the provider, including against the older temporary edge. It is disabled during provider work and after its own check. A 404 verifies absence, 400 means the old temporary edge remains, and 401 means sign-in expired; neither of the latter outcomes claims removal.

Final UI-focused checks: 62 Python tests passed, six dedicated one-shot/removal UI tests passed, and broader Node discovery passed 140 tests (the frozen 133-test list plus the existing standalone request-guard test and six temporary UI tests). Headless 1200/390 success and 390 network-failure cases passed, with exactly two fake provider calls, zero external calls, zero report-store access and no automatic retries. The removal checks on the old fake edge returned 400 without additional provider calls.

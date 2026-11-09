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

# Report list context update

Status note: This document records an earlier development checkpoint. The integrated result and final test attribution are in [report-list-final.md](report-list-final.md); historical limitations below are preserved for traceability.

Base: 1740eda7436d38ab5a1618ecf1afa542f3be542c. Isolated branch: fix/report-list-context.

## Changes

- Full-width, wrapping report filenames and separate confirmed client, job, report date, upload timestamp, version/revision and short file ID.
- Report quantities carry per-measure value, unit, completeness and partial status. Unknown, invalid, conflicting and out-of-range totals do not become zero. No source truth or readiness is inferred.
- Source/check status is separate from report values. Missing client/day/source coverage remains in To do. Sent items are consistently excluded from that filter.
- Only explicit root-file lineage collapses versions. Independent uploads with identical names/client/job/day remain separate.
- Search includes client, exact file identity and source label. Back navigation restores filter/search and last-opened keyboard focus. Review-request filter is distinct from Needs review actions.
- Bounded inventory and stale-list fetch failures are explicit. A separate sibling Needs review button avoids nested interactive controls.
- Existing access filtering, original preservation, report bindings, correction/release permissions and provider configuration are unchanged.

## Evidence at list-stage snapshot

- 3 new Python regression cases passed, including quantity edge cases and read-only list contract.
- 57 directly affected Python summary/statistics/list tests passed, with 7 subtests.
- 109 Node UI tests passed, including 7 list tests.
- Full production app with fake-only storage and synthetic inventory passed in isolated headless Chromium at 1200×900 and 390×900. Names wrap; no horizontal overflow; unknown dates differ from upload time; quantities/lineage/search/filter/keyboard-return assertions pass. Test wrapper denies non-loopback sockets and write/provider endpoints and verifies before/after saved report plus original bytes.
- Independent reviewer inspected synthetic screenshots and found names, identity and quantities readable at both sizes.
- Initial full pytest without the local-development fixture flag reached HTTP auth denials instead of expected test handlers. This is not a final aggregate pass. A correctly configured full aggregate is required after integration.

Reproduce browser QA with the existing test-venv Python and existing PLAYWRIGHT_MODULE / CHROMIUM_EXECUTABLE environment paths: `python tests/browser_report_list_context.py`.

No live reports, customer data, actual source access, deployment, remote push, user browser or provider calls were used in these checks. This document describes an intermediate list-stage candidate; integrated Needs review/AI/Ask Dex behavior has separate acceptance evidence.

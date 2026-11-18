# Narrow false-clear preview hotfix

Baseline: authenticated deployed preview commit `fb8366bf85f5bcf8a59e0eafbc5e76ab7d48f58f`.
Ported source: reviewed isolated `fb682f1`, selected changes only.

Runtime scope is four files: review_decisions, review_summary, public_api, web/app.js.
No draft bundles, freshness changes, recovery planner/executor, database migrations,
real-data fixtures, audit artifacts, source parser changes, or report mutation are included.

## Behavior

* Stop automatic Fix with AI rejects. Reject stale-client fallback requests.
* Exact historical fallback payloads on unprotected working versions remain actionable,
  including when earlier AI verdicts say they are fine. Genuine differently worded
  decisions remain applied; ambiguous canonical wording is visible without attribution.
* Raw approval (including stale/empty records), pending/in-progress delivery, or any
  delivery history protects historical projection. Historical status/counts, approval,
  delivery, and decision records stay unchanged. A separate `historical_integrity`
  assessment lists the exact fallback findings and unresolved count. It does not revoke,
  reopen, repair, or reapprove that snapshot. It is visible in list/detail/API and amber
  UI banners instead of a standalone green completion claim. Fallback records are not
  credited to a human in reconciliation.
* Async decision/evidence/approval/recheck results cannot overwrite another report.
  Lost approval responses remain unconfirmed; no automatic retry or inferred success.

## Deliberate exclusions and limits

The broad source branch contains unrelated fixes; it is not being deployed wholesale.
Two source-branch tests asserting newer checked-copy freshness behavior are not ported,
because that runtime behavior is deliberately excluded. Existing baseline tests remain.
No protected snapshot is durably altered. Exact fallback wording identifies a payload,
not its human author; a person who copied that exact payload requires review.
No actual live report, client delivery, AI paid call, or excluded customer system was touched.
Deployment and authenticated live readback require independent approval of this diff.

## Upload receipt follow-on

The baseline upload success path called an undefined receipt renderer after the server
could already have saved the file. This follow-on defines that renderer and validates
the returned content hash. A missing/wrong/failed finish response causes a read of the
exact content hash, never another upload or finish request. Positive readback establishes
only that the exact bytes exist, not that this particular request created them. Failed or
absent readback stays unconfirmed. Renderer failures after confirmation retain the saved
item and cannot claim nothing was saved. Tests use fictional filenames and stubbed bytes.


Converted PDF/Word uploads and server-formatted results use an authenticated transport
receipt: server-computed verified input SHA, initially stored workbook hash, and exact
returned report hash. This metadata is not saved into the historical report. Converted
lost responses remain unconfirmed because no original-document-digest lookup exists;
no original hash is presented as a converted-workbook ID. Raw Excel/CSV can still be
reconciled by exact-byte lookup. Tests include actual fictional PDF/Word conversion via
local HTTP and a stubbed server-format operation returning an actual local stored child.

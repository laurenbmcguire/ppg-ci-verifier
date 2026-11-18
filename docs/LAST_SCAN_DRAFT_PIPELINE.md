# One-hour SharePoint draft pipeline: isolated prototype

Status: NOT DEPLOYED. No live reads, source mutations, client delivery, approval,
production scheduler or credentials were enabled by this work.

## Implemented
- Explicit client/site/list/job/business-date/recorded-shift/timezone scope.
- Sealed complete capture adapter; explicit trusted actual-scan field, no Created
  or Date fallback. Unknown membership/date and partial captures fail without
  advancing persisted state.
- UTC elapsed one-hour window. New records, edits, deletes and photo changes
  reset quiet period; late arrivals reset a full hour from observation.
- Verified schedule can set earliest draft time. Missing schedule DOES NOT block
  indefinitely: quiet-window report is provisional, never shift completion.
- Atomic SQLite claims for local harness; stable operation IDs, receipts,
  concurrent duplicate suppression, uncertain outcomes do not retry blindly.
- Original native Excel template, scoped source rows, image hash verification,
  sidecar issue checklist and source binding. Missing quantities stay blank and
  prominently UNKNOWN, affected aggregate withheld, known totals retained.
  In incomplete drafts formulas are materialized so Excel cannot coerce unknowns
  to zero. Source ledgers preserve the typed missing values.
- Private output bundle only. No email/SMS/client release transport.

## Deployment prerequisites (not completed)
1. Authoritative active-job registry and exact client/site/list mappings; paging
   across ALL active jobs has not been implemented here.
2. Validate actual scan timestamps, timezone and shift field against every live
   list. Business Date and Created are not interchangeable with actual scan.
   Resolve overnight shift/business-date policy; verify schedules when available.
3. Validate app-only read permissions and bounded Graph capture, pagination and
   photo access on real tenant. Do not widen permissions merely to run this.
4. Integrate hosted transactional database and distributed leases. Local SQLite
   is not a cross-instance hosted lock. Existing app-open autopilot is not usable.
5. Add authenticated durable scheduled invocation and retry/alert/reconciliation
   worker. Current uncertain receipt needs explicit reconciliation; renderer
   lookup verifies an existing artifact before replay. No auto recovery yet.
6. Recheck source before delivery and supersede earlier revisions when subsequent
   capture changes. Current exact snapshot draft is not a live-freshness claim.
   Capture and polling order races need hosted fencing; no guaranteed final scan.
7. Approve/configure per-client native template metadata and representative photo
   selection. Existing supported-photo selection remains explicit; missing photos
   stay in checklist. Test real template, direct-photo exports and tablet source.
8. Wire Lauren-only authenticated artifact delivery and phone notification, with
   durable deduplicated acknowledgment; current local bundle is NOT sent.
9. Validate full regression, failure injection, source-change races, service load
   and real-job dry run before activation. Never infer final/client approval.

## Operational limits
No guaranteed exact-to-second delivery: frequency, capture and rendering latency
must be disclosed. Historical backfill is not discovery. Partial captures must
produce an operational issue notification, not silently disappear. Independent
complete jobs should continue if another scope fails. Drafts remain held but
must be provided to Lauren with issues, not blocked solely for missing values.

## Follow-on adapters and read-only proof
last_scan_service.py adds terminal Graph list discovery for explicitly configured
sites. Only verified explicit active assignments become scopes; unmapped and absent
lists are issues, not guessed clients. Source adapter invokes sealed Graph capture,
requiring separately verified timestamp semantics. The caller supplies business
dates/shifts from operational configuration, not a fixed default.

ScheduledWorker supports an injected clock, persistent polling lease/token fencing,
post-render source recapture, stale draft suppression and a durable Lauren-only
test outbox. Expired leases can restart. Uncertain sends cannot automatically
repeat; successful acknowledgments are retained even when delivery was superseded
in flight. This cannot retract an already-sent stale draft. No real sender or
hosted scheduler installed. Lease refresh beyond300s not implemented; workers
expire safely and need reconciliation. SQLite remains single-host only.

Live delegated connector read-only observation on 2026-11-18: SL Alabama2207
list32a42ee0-4592-4d73-b893-1b98970b3d4b, canonical site successfully resolved.
Two historical rows exposed Date,Date0,Shift,QuantityInspected,QuantityOK,
QuantityNOK,PictureLabel,PictureDefect. Date0 equaled Created in both examples.
This is NOT proof Date0 represents physical scan time or offline/backfill behavior.
Only a two-row bounded sample was read; no complete-capture/current-job claim.
Background app credentials and actual-scan semantics remain unverified.

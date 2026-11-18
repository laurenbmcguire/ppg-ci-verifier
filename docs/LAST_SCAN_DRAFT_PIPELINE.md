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

## Durable runner (branch `claude/durable-report-runner-20261118`)

Status: local code and tests only. Not deployed, not scheduled, not connected to any live source. Off by default.

### What was added

| File | Purpose |
|---|---|
| `hosting/last_scan_store.py` | One durable store interface over SQLite (local) and PostgreSQL (hosted): `atomic(keys)` commits several records in one transaction; content-addressed blobs; leases with a fence number. PostgreSQL tables are created only by an explicit `install()`. |
| `hosting/last_scan_service.py` | `ScheduledWorker` rewritten on that store (same constructor and helper names). |
| `hosting/last_scan_runner.py` | `run_once(...)`: bounded, off-by-default pass over explicitly verified assignments that also match the verified job registry. |
| `scripts/last_scan_synthetic_demo.py` | Runnable synthetic end-to-end example. |
| `scripts/last-scan-pg-test.sh` | Starts a throwaway PostgreSQL inside `.tmp/pg` (unix socket only), runs the real-database tests, deletes it. |

### Guarantees and where they are enforced

- **Lease and current operation are fenced in the same transaction as the outbox insert.** `_finish` takes a fresh
  capture, then in ONE `fenced(...)` transaction locks the lease, state and outbox rows, checks the lease token and
  fence, re-observes, records the render receipt and inserts the outbox entry only if the rendered operation is
  still current. A replaced or expired worker raises `LeaseLost` and commits nothing.
- **Unknown render outcomes are reconciled, not repeated.** A due operation is marked `uncertain` before rendering.
  After a crash the publisher is asked by operation ID (`lookup`). Rendering the same operation again happens only
  when the publisher declares `lookup_is_authoritative` and reports nothing written.
- **Delivery outcome is stored.** `dispatch_test` writes `delivery_status='uncertain'` before the attempt and keeps
  it (with the error type) when the sender fails, also when the entry was superseded mid-attempt. Such an entry is
  never re-queued automatically.
- **State rows stay small.** Photo bytes and the captured snapshot are blobs addressed by SHA-256; the state row
  keeps `{captured_at, ref, rows, ...}` only. Identical photos are stored once.
- **No guessed mappings.** An assignment needs `verified`, `active`, `timestamp_semantics_verified`, client, job,
  site id, list id, timezone, actual-scan field, shift field and recorded shifts. The verified registry must say
  that this exact site id + list id belongs to this exact client key and job: exact strings, no name matching, no
  shared-number matching, no aliases. Two verified entries that disagree about a list make it ambiguous and it is
  skipped. Every mismatch is reported and causes no read and no render.
- **One renderer per exact client/site/list/job.** `run_once` takes a `RendererRouter`, never a single publisher.
  `NativeDraftRouter` builds one bound `NativeDraftPublisher` from each assignment's own `mapping` and `header`,
  in its own folder. An assignment without both has no renderer and is skipped before any read. A bound publisher
  refuses another scope before writing. Mapping, header, template and binding are in `render_version`, which is
  part of the operation ID, and a receipt from another binding or version is refused.
- **The outbox follows the latest readiness.** Each poll updates the outbox in the same transaction as the timer.
  If the scope is no longer due (new content, or the same rows with a later verified shift end), a pending entry
  becomes `held` and cannot be claimed; it returns to `pending` only when a poll finds the scope ready again.
  `dispatch_test` claims an entry only while the stored state is `draft_ready` for that same operation, under the
  same row locks a poll takes. A delivery already in flight keeps its status and acknowledgment and is marked
  `readiness_revoked_at`; it is never re-opened or sent twice.
- **Dispatch recaptures the source.** Stored readiness is never enough to send. `dispatch_test` takes the scope
  lease, recaptures the exact scope, and in one transaction fenced by that lease re-observes the fresh capture and
  claims the entry only if it yields the same operation (rows, photos, mapping/header/template version), the scope
  is due on the latest schedule, and the capture is complete and fresh. Otherwise nothing is sent and the timer and
  outbox are updated from what was seen. A failed, partial, stale or wrong-scope capture holds the entry; a hold
  from a failed capture may be tried again because each try recaptures. Drafts and receipts are never touched.
  `reconcile_test` settles an unknown delivery by asking the test transport; it never sends.
- **Attempts are fenced by attempt ID.** Each claim creates an attempt (`attempt_id`, number, deadline) in an
  append-only `attempts` list on the entry. The sender receives `idempotency_key` (the operation ID, the same for
  every attempt) and `attempt_id`. A success, failure or reconciliation result is applied only to its own attempt;
  one that arrives after the entry moved on is stored on the old attempt (`late_ack`, `late_error_type`) and, for a
  late success, flagged as `duplicate_delivery_evidence`. Nothing is deleted.
- **No automatic retry by default.** `hosting/last_scan_transport.py` holds the registry of verified provider
  contracts, and it is empty. Without a contract listed there, `reconcile_test` does not query anything and changes
  nothing, `deliver` refuses to send, and no flag, lookup answer or deadline can alter that at run time. The proof
  contract below applies only under a contract that is listed there and also permits a retry.
  See `docs/LAST_SCAN_ACTIVATION_CHECKLIST.md` for the hosted entry point (prepared, not connected).
- **Transport proof contract.** An unknown outcome leads to another attempt only if the transport answers, for
  this exact attempt: `operation_id` and `attempt_id` matching, `delivered: false`, `terminal: true`,
  `attempt_closed: true` (it will reject this attempt if it arrives later) and a non-empty `proof_id`. If this
  process's own sender call may still be running (attempt in flight and its deadline, 15 minutes by default, not
  passed) even that is not acted on. The entry then goes on hold and needs a poll that finds the scope ready and
  a fresh recapture before a new attempt. `delivered: false` alone, "not found", another attempt's proof, no
  answer or an error all stay uncertain. `delivered: true` naming the attempt is recorded as sent.
  A real transport must (1) deliver at most once per `idempotency_key`, (2) be able to give that terminal answer
  per attempt, (3) honour `attempt_closed`. A transport that cannot do (2) and (3) never gets a second attempt.
  Not closed: the source can change after the recapture and before the provider receives the message, and the
  claim and the send are not one transaction. Those cases end as `readiness_revoked_at` or `uncertain`.
- **A stored draft is a historical record.** The checklist and receipt carry `evidence_as_of` (the capture they
  were built from) and a `HISTORICAL RECORD` notice. When the same draft is reused for a later, content-identical
  observation the result says `artifact_state: historical_reused` with the original `evidence_as_of` and the new
  `observed_at`; only the run that rendered it says `built_this_run`. `worker.provenance(scope, op)` compares the
  draft with the latest observation (for example photo status 403 in the draft, 404 now) without changing either,
  and `StoredDraftPublisher.export(op)` returns the stored bytes with a historical label. Nothing is rewritten.
- **Rows in no configured shift are reported.** A row of the day whose recorded shift value is not among the
  assignment's `recorded_shifts` belongs to no draft. It is counted in `unassigned_shifts` on every result,
  checklist and text checklist for that day; it is never guessed into a shift and never dropped silently.
- **Adapter contract tests** (`tests/test_last_scan_adapter_contract.py`) run the real capture and adapter code
  against a synthetic read-only fake: pagination completeness, exact list/shift/mapping/timezone, the scan-time
  contract, photo authorisation, unknown quantities, throttling, and GET-only transports. They do not make any
  real list ready.
- **A cached draft never shortens the hour.** If content returns to an earlier version, the hour restarts like any
  other change. The earlier receipt and outbox entry are used again only when the renewed hour is over. `Date`, `Created`, `Modified` and their Graph names are refused as scan fields.
  Anything else is reported in the run receipt under `issues` and skipped.
- **Bounded and polite.** Time budget, maximum scopes per pass, rotating start point, per-scope failure isolation,
  provider cooldown held per site across runs (default 15 minutes when the provider gives no time).
- **Run receipts** (`run:<hash of run_id>`) are internal records. The same `run_id` is replayed without any read.

### Timing rules (unchanged, now tested end to end)

The hour restarts when scanning resumes and when a late row, edit, delete or photo for this shift is first seen.
A quiet hour is never called shift end or lunch end: the draft says `provisional_quiet_window_schedule_unknown`,
`day_completion: not_established`, `client_release: held`. When a verified scheduled end (`not_before`) is supplied
the draft waits for it and says `verified_schedule`. Without one, a provisional draft is still produced.

### How to run

```sh
export PY=/path/to/test-venv/bin/python PYTHONPATH=.:tests TMPDIR=$PWD/.tmp
$PY -m pytest -q tests/test_last_scan_drafts.py tests/test_last_scan_local.py tests/test_last_scan_service.py \
    tests/test_last_scan_durable.py tests/test_last_scan_runner.py
PY=$PY sh scripts/last-scan-pg-test.sh          # real PostgreSQL, disposable, removed afterwards
$PY scripts/last_scan_synthetic_demo.py $PWD/.tmp/demo
```

`tests/test_last_scan_store_pg.py` is SKIPPED unless that script provides the disposable server. A skip means hosted
concurrency is unverified, not passed.

### Not done here: blockers before any activation

1. No route, cron or scheduled task exists. A host entry point needs authentication, a unique `run_id` per firing,
   and a budget below the host's time limit.
2. `PostgresStore.install()` has not been run anywhere. Creating the two tables in the hosted schema is a migration
   that needs approval; the role's connection limit also matters (each record transaction opens one connection).
3. Proven on local PostgreSQL 17 only, not on the hosted database or through its connection pooler.
4. No live assignment exists. Each client list needs a person-verified actual-scan field, shift field, timezone
   and recorded shift values. The registry seed entries carry site names, not Graph site ids, so they do not match
   until a verified entry with the site id is saved.
5. An existing, separately scheduled reporting task already builds reports for a fixed list of jobs. Until that is
   migrated, give those jobs `externally_built: true` here (they are skipped and reported) so nothing is built
   twice. Drafts made outside this runner are unknown to its operation IDs.
6. The 5-minute capture age limit (`MAX_AGE`) can be shorter than a large capture with many photos.
7. The base commit still contains the Fix-with-AI regression in `web/app.js`; the audit branch fix is separate.
8. `NativeDraftPublisher` writes to a local folder. A hosted publisher must store drafts in private storage and
   provide `lookup`. Old blobs, receipts and outbox entries are never pruned yet.
9. `dispatch_test` is a test transport. No sender exists and none should be added without approval.

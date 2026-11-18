# One-hour reporting rehearsal (fully synthetic)

`tests/rehearsal_harness.py` and `tests/test_one_hour_rehearsal.py` run the whole path on the real code against
invented data held in memory. Run it with:

```
PYTHONPATH=.:tests python -m pytest -q -p no:cacheprovider tests/test_one_hour_rehearsal.py
```

It makes no network call (the tests fail if a socket is opened), reads no token, uses no customer data, sends
nothing outside a list in memory, and deploys nothing.

## What it traverses

1. **Schema candidate capture** through `hosting/last_scan_schema_capture.py` and a mocked transport.
2. **Fake capture authority** records the receipt candidate; the test fixture reviewer pins and attests the mapping;
   a **fake approver** approves the exact snapshot a dry run reports.
3. **Hosted preflight and run** (`hosting/last_scan_hosted.handle`) with a fake identity. The live-schema probe is
   a test class backed by the same capture adapter.
4. **The real source capture**, quiet-hour timers and durable state in a SQLite file that is reopened to imitate
   restarts.
5. **The real native Excel draft and checklist** for each client, read back and inspected cell by cell.
6. **A fake outbox** whose single recipient is a fictitious address, behind a fake provider contract that exists
   only inside the tests that install it.

Two invented clients: ALPHA-REHEARSAL / RH-7001 (three shifts, shift ends configured, reject counts never
recorded, photos refused then available) and BRAVO-REHEARSAL / RH-7002 (two shifts, no shift ends configured,
reject counts recorded).

## What it shows, on synthetic data only

| Situation | Outcome asserted |
|---|---|
| Scanning resumes before the hour is up | A new hour starts; nothing is drafted or dispatchable at the original time |
| Lunch pause longer than an hour, shift end configured | No draft until the configured shift end; scanning after the pause pushes it later |
| Late row from an offline tablet, with an old scan time | The ready draft is superseded; the hour counts from when the row arrived |
| A row arrives between the poll and the dispatch | The dispatch re-reads the source and sends nothing |
| Out-of-order or too-old capture | Refused; state and timers unchanged; the other client unaffected |
| Source outage (503 with a wait) or permission loss | Error, not "no scans"; the site is held; no further item reads; a ready draft is not sent on stale knowledge |
| The same firing replayed, overlapping firings, a restart | One draft, one outbox entry, byte-identical stored files |
| No verified provider contract | Nothing can be sent at all |
| Fake contract installed | One message to the fake inbox; repeats, restarts and six concurrent dispatchers send no second one |
| Sender times out | Outcome recorded as uncertain and never retried, across restarts and hours |
| Live column changed after approval | The run stops before any item is read, for both clients |
| Reject count never recorded | Blank in the workbook, reported per record; never zero |
| Photo refused, later available | New draft built; the earlier draft kept, labelled historical, never rewritten |
| Two clients in one run | No header, identifier, part number or name of one client anywhere in the other's files |

This rehearsal does not claim exactly-once delivery. With the fake contract the assertion is only that no second
send was made in the situations listed.

## What it does not show

Every authority in the rehearsal is a fake that says yes. It therefore proves nothing about: a real list's
columns or rows, what any real column means, a real collector or its identity, a real approver's review, a real
delivery provider's behaviour, a real scheduler, or PostgreSQL (the rehearsal runs on SQLite).

Clock mismatch found through the rehearsal and fixed afterwards: a relative `Retry-After` (seconds) was turned into
a time using the wall clock inside the capture code and then compared with the runner's own clock. With the capture
clock two minutes behind, a 60-second wait had already "expired" and all three shifts of the same site were read in
one poll. The runner now counts a relative wait on its own clock from the moment it saw the answer
(`last_scan_drafts.hold_until`), keeps an absolute date as stated, waits 15 minutes when nothing usable was stated,
never less than 60 seconds and never more than 7 days (a longer stated wait is held at 7 days and flagged). Those
three figures are policy awaiting an owner's decision. See `tests/test_last_scan_cooldown_clock.py`.

Three things the rehearsal does not cover or fix, stated here and in the harness:

- **Dispatch bypasses the hosted checks.** `Rehearsal.dispatch` goes through the worker and the one delivery door,
  not through the hosted entry point, so no identity, approval, capture-receipt or live schema probe check runs
  there. A test pins this as a known gap. The hosted entry point has no dispatch step.
- **A restart reopens only the durable store.** The fake source, approval and capture records, outbox and clock
  stand for outside systems and are not reset.
- **A throttled re-read at dispatch sets no site hold.** If the source answers 429/503 while a dispatch re-reads it,
  the outbox entry is held, but the runner's per-site wait is not recorded, so another dispatch could ask again at
  once. Not changed here; it belongs with the design of a hosted dispatch step.

## Activation gap matrix

| Prerequisite | What exists | Code seam | What is missing | Owner |
|---|---|---|---|---|
| Scan-time meaning | Validator refuses Created/Modified/date columns, computed, read-only and defaulted columns; requires a named attestation bound to the exact column | `last_scan_readiness.validate`, assignment `scan_field` + `attestations['scan_time_meaning']` | Evidence, from the floor, of which real column records the actual scan and how the tablet fills it in. None identified on any real list | PPG operations + root |
| Graph compatibility | Capture adapter and existing data capture, both tested against mocked answers only | `last_scan_schema_capture.capture_schema(fetch=…)`, `source_first_capture.graph_fetch` | One real read-only capture: whether next links use `$skiptoken` literally, whether `parentReference.siteId` is returned, which column properties real lists carry, list-id casing | Root |
| Trusted collector and capture authority | Interfaces and a frozen, approval-bound accepted-collector list | `last_scan_hosted.CaptureAuthority`, `Deps.collectors`, `SchemaProbe` | A real authority that records receipts outside the runner's reach; a real collector identity; a real read-only probe; owner decision on the 24 h / 5 min / 60 s bounds | Root + security review |
| Identity and approval | Interfaces only | `last_scan_hosted.Authorizer`, `ApprovalAuthority`, `Quota` | A real identity system and a separately protected approval record; who may approve | Root |
| Scheduler host | One bounded, replay-safe call per firing; nonce, quota and receipts | `last_scan_hosted.handle` (no route, no schedule reaches it) | A reviewed route and schedule; the store migration; a decision on firing interval. The existing twice-daily reporting task is untouched and unrelated | Root |
| Durable store | SQLite and PostgreSQL stores; PostgreSQL proven on a disposable cluster by the reviewer | `last_scan_store.PostgresStore.install()` | An approved migration on the hosted database; the pending connection-limit decision | Root |
| Verified delivery contract | Registry of verified provider contracts, empty; uncertain outcomes never retried without one | `last_scan_transport.VERIFIED_CONTRACTS`, `deliver` | A real provider whose behaviour (at most once per key, terminal status per attempt) is evidenced and reviewed. The hosted entry point has no dispatch step at all; one would have to be designed and reviewed | Root + provider |
| Client release | Held everywhere; drafts go to an internal audience only | checklist `client_release: held` | Unchanged by this work; release stays a separate human decision | Lauren |

## Exact next step for a real read-only proof (root-owned)

Follow `docs/LAST_SCAN_SCHEMA_CAPTURE_RUNBOOK.md` for **one** list, by hand, on the approved machine with the
existing read-only app registration:

1. Run the schema capture for one list. It makes about six GET requests to the list and its columns, reads no
   rows and writes nothing.
2. Keep the artifact outside the repository. Record, for the reviewers, only these facts: whether the capture
   completed or where it refused; whether next links were accepted; whether the site was echoed;
   the list of column properties flagged under `columns_needing_review`.
3. Do not attach the artifact to the hosted entry point, do not create a receipt for it, and do not change any
   configuration. The outcome is input to the Graph-compatibility row above and nothing else.

Everything after that (who attests the scan column, the capture authority, the scheduler, delivery) stays blocked
on its own row.

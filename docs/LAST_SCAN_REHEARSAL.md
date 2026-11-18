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
(`last_scan_drafts.hold_until`) and keeps an absolute date as stated. The policy:

| What the source said | Hold |
|---|---|
| No `Retry-After` header at all | 15 minutes (default) |
| A whole number of seconds, or an HTTP date | Kept in full, however long; never under 60 seconds; over 7 days also flagged |
| Something stated that cannot be safely read (words, negative or fractional number, empty value, a date with no zone or beyond the calendar, a number too large to be a time) | No end time; a named manual release is required. The default is never substituted, because it could be shorter than what the provider meant |
| Several different values (for example under differently cased header names) | If any one needs a person, the whole answer does; otherwise the one that ends latest. Never first or last as they arrived |

The header name is matched without regard to case. Because of the first row, this code does not claim that it
never reads earlier than a provider intended: when a provider sends no header at all, the 15-minute default is a
guess. The 15 minutes and 60 seconds are placeholders awaiting an owner's decision. The hold is one record per site, merged so
that a later answer can only extend it, and it is honoured before any request to that site: polls, the hosted
schema probes, and dispatch re-reads, across workers and restarts. See `tests/test_last_scan_cooldown_clock.py`.

What the rehearsal does not cover or fix, stated here and in the harness:

- **`Rehearsal.dispatch` is a test-only direct seam.** It calls the worker's test transport (`dispatch_test`)
  directly and authorizes nothing: no identity, approval, recipient policy, capture-receipt or live schema probe
  check runs there. It exists to exercise the worker's own claim and outbox behaviour. The production door
  (`last_scan_transport.deliver`) refuses the same call for want of a dispatch grant.
- **The authorized path is `hosting/last_scan_dispatch.py`**, prepared and connected to nothing. It repeats the
  hosted run preflight for the caller, then requires a verified provider contract, a server-side recipient policy,
  a dispatch approval covering snapshot, recipients and contract, the exact stored artifact, and a single-use
  nonce, quota, intent and grant, before the worker probes the schema and re-reads the source under its lease. A
  drifted column stops it before any item is read (`tests/test_last_scan_hosted_dispatch.py`). Every authority,
  contract, recipient and sender behind it in the tests is a fake; none exists for production.
- **Races that stay open at dispatch.** The source can change between the schema probe, the re-read, the claim and
  the provider receiving the message, and an approval can be withdrawn after the last re-check. The dispatch grant
  is a record in the runner's own store: code that can write the store can write one. `dispatch_test` stays
  callable by tests.
- **A restart reopens only the durable store.** The fake source, approval and capture records, outbox and clock
  stand for outside systems and are not reset.
- **Dispatch is still not a production path.** The boundary above has only been run against fakes. No real
  identity system, approval authority, capture authority, schema probe, recipient policy store, provider or sender
  exists, the provider registry is empty, and nothing routes to it. That remains an unreleased blocker pending
  independent review.

## Activation gap matrix

| Prerequisite | What exists | Code seam | What is missing | Owner |
|---|---|---|---|---|
| Scan-time meaning | Validator refuses Created/Modified/date columns, computed, read-only and defaulted columns; requires a named attestation bound to the exact column | `last_scan_readiness.validate`, assignment `scan_field` + `attestations['scan_time_meaning']` | Evidence, from the floor, of which real column records the actual scan and how the tablet fills it in. None identified on any real list | PPG operations + root |
| Graph compatibility | Capture adapter and existing data capture, both tested against mocked answers only | `last_scan_schema_capture.capture_schema(fetch=…)`, `source_first_capture.graph_fetch` | One real read-only capture: whether next links use `$skiptoken` literally, whether `parentReference.siteId` is returned, which column properties real lists carry, list-id casing | Root |
| Trusted collector and capture authority | Interfaces, a frozen approval-bound accepted-collector list, and the version 1 receipt contract below | `last_scan_hosted.CaptureAuthority`, `Deps.collectors`, `Deps.issuers`, `SchemaProbe`, `last_scan_authority` | A real authority that records receipts outside the runner's reach and answers in exactly the contract's shape; a real collector identity; a real read-only probe; owner decision on the 24 h / 5 min / 60 s bounds | Root + security review |
| Identity and approval | Interfaces, and the version 1 approval contract below | `last_scan_hosted.Authorizer`, `ApprovalAuthority`, `Quota`, `Deps.issuers`, `last_scan_authority` | A real identity system and a separately protected approval record that answers in exactly the contract's shape; who may approve; owner decision on the 7 day maximum validity | Root |
| Host clock | Validity is judged only on the host's clock; a clock more than 5 min behind the last time the store saw is refused | `last_scan_hosted.clock_problem`, `Deps.clock` | A host whose clock is synchronised and monitored. A clock that runs fast, or slow by less than the skew, is not detected | Root |
| Scheduler host | One bounded, replay-safe call per firing; nonce, quota and receipts | `last_scan_hosted.handle` (no route, no schedule reaches it) | A reviewed route and schedule; the store migration; a decision on firing interval. The existing twice-daily reporting task is untouched and unrelated | Root |
| Durable store | SQLite and PostgreSQL stores; PostgreSQL proven on a disposable cluster by the reviewer | `last_scan_store.PostgresStore.install()` | An approved migration on the hosted database; the pending connection-limit decision | Root |
| Verified delivery contract | Registry of verified provider contracts, empty; a version 1 outcome contract for what an adapter answers; uncertain outcomes never retried | `last_scan_transport.VERIFIED_CONTRACTS`, `ProviderContract`, `judge_outcome`, `deliver`; `last_scan_dispatch` | A real provider and adapter, with the evidence listed under "Provider adapter contract" below, reviewed and approved outside this work. Pending external approval; nothing is registered | Root + provider |
| Client release | Held everywhere; drafts go to an internal audience only | checklist `client_release: held` | Unchanged by this work; release stays a separate human decision | Lauren |

## Authority adapter contract (version 1)

An adapter for a real approval or capture authority must return **exactly** this plain JSON, or nothing. The code
that judges it is `hosting/last_scan_authority.py`; it implements no authority. Any missing field, unknown field,
unsupported version, issuer the host does not accept, status other than `active`, set revocation field, or time
that is missing, malformed, without an offset, not yet valid or expired is refused. Nothing is defaulted and nothing
is ignored, so an adapter that marks a withdrawn record with a field of its own (`revoked: true`, `expires: …`) is
refused instead of being read as valid.

| Field | Approval (`contract: last_scan.approval`) | Capture receipt (`contract: last_scan.capture_receipt`) |
|---|---|---|
| `version` | integer `1` | integer `1` |
| `record_id` | the authority's own id for this record; a renewal is a new id | same |
| `issuer` | which authority wrote it; must be in the host's accepted list for approvals | same, for capture authorities |
| `status` | exactly `active` | exactly `active` |
| what it covers | `snapshot_digest`, `principals`, and `action: last_scan.dispatch` on a dispatch approval only | `snapshot_identity`, `site_id`, `list_id`, `schema_digest`, `collector` |
| who and when | `approved_by`, `approved_at` | `captured_at` |
| validity | `not_before`, `expires_at`, at most 7 days apart | `expires_at`, at most 24 h after `captured_at` |
| withdrawal | optional `revoked_at`, `revoked_by`, `revocation_reason`; any of them set means revoked | same |

What the runner does with it, and what follows for whoever operates the authority:

- **One record per check.** Each check reads the authority once and judges that one record whole.
- **Host clock.** `expires_at` and `not_before` are compared with the host's clock, to the second. The end instant
  itself is already expired.
- **Re-asked before every source read** in a poll, and before the read, after the read and (clock only) before the
  send in a dispatch. The record must be the *same* record as when the call was authorized.
- **Renewal is a new authorization.** A renewed capture receipt changes the run snapshot, so the run needs a new
  approval. A renewed run approval needs a new dispatch approval. Extending a record in place counts as a renewal.
- **Dispatch needs time left.** An approval or receipt that expires within 60 seconds is refused up front, and a
  dispatch that takes longer than 60 seconds stops.
- **Placeholders, pending an owner's decision:** 7 days, 24 hours, 5 minutes of clock skew, 60 seconds.

Not provided by this contract: any proof that a record is genuine. Signing, transport security and who may write to
the authority belong to the real authority and its adapter, and none exists.

## Provider adapter contract (version 1)

No provider adapter exists and none is registered. An adapter would be called as
`sender(idempotency_key, attempt_id, audience, payload, deadline_seconds)` and must answer with **exactly** this plain
JSON (`hosting/last_scan_transport.judge_outcome`); anything else is recorded as unknown:

`contract: last_scan.transport_outcome`, `version: 1`, `provider`, `provider_contract` (hash of the reviewed
`ProviderContract`), `provider_record_id`, `operation_id`, `attempt_id`, `idempotency_key`, `recipient`,
`workbook_sha256`, `checklist_sha256`, `state`, `terminal`, `attempt_closed`, `observed_at`,
`request_deadline_seconds`, `elapsed_seconds`, optional `detail`.

| `state` | Means | Recorded as |
|---|---|---|
| `delivered` (terminal, attempt closed) | the provider states this attempt of this operation reached this recipient with this artifact | sent |
| `accepted` (not terminal, attempt open) | taken by the provider, not yet delivered | uncertain; never sent again |
| `not_delivered` (terminal, attempt closed) | the provider's final statement that this attempt was not delivered | uncertain, with the proof kept on the attempt; never sent again by this path |
| `unknown`, any other state, any mismatch, any unknown field, no answer, an exception | nothing is established | uncertain; never sent again |

`observed_at` must fall between the host clock just before the sender was called and when it returned, give or
take 5 minutes (a placeholder skew, pending an owner's decision). An answer dated outside that window is recorded
as unknown. What is acknowledged is built from the judged record id and time; the answer itself is kept whole only
up to 4000 bytes and as its hash beyond that.

Evidence a real provider would have to supply before a `ProviderContract` could be reviewed and listed, none of
which exists today:

1. **Idempotency that survives.** At most one delivery per idempotency key, across the provider's own retries,
   restarts and regions, for at least as long as an entry can stay uncertain; and what it answers to a repeated key.
2. **Per-attempt terminal status.** A lookup, by operation and attempt, that distinguishes "not seen yet" from a
   final not-delivered, and a guarantee that a closed attempt is rejected if it arrives later.
3. **A request deadline the adapter enforces itself.** `sender_timeout_seconds` is told to the adapter and must be
   reported back. This code does not and cannot cancel a request that is already with a provider: a slow call, a
   disconnect or no answer is not a failure, and an adapter that overruns is only recorded as having done so.
4. **What "accepted" becomes.** Whether, how and how late an accepted message is delivered, and how that is learned.
5. **Recipient and artifact binding.** Proof in the answer of which recipient and which exact bytes were delivered.
6. **Review validity.** `valid_until` on the contract: the review lapses, and a dispatch needs at least 60 seconds
   of it left. Removing the contract from the registry stops the next send.

## Reconciling an uncertain delivery

`hosting/last_scan_reconcile.py` is prepared and connected to nothing. It lets one verified caller, under a
**reconcile approval for exactly one attempt**, ask a provider **status adapter** about that attempt and record what
the provider states. It never sends, never re-queues and never makes an entry deliverable again.

| Provider states (version 1 outcome, for exactly this attempt) | Recorded |
|---|---|
| terminal, closed `delivered` | the entry becomes `sent` |
| terminal, closed `not_delivered` | the entry becomes `not_delivered`, a final state no poll, timer or dispatch reopens |
| the same, while the original sender call may still be running (attempt in flight, its deadline not passed) | nothing; kept in the audit trail |
| `accepted`, `unknown`, a bare `delivered: false`, any mismatch, an error, no answer, a passed deadline | nothing; kept in the audit trail |

Only the provider contract an attempt went out through can attest it. The worker records the provider and the
contract's hash on the attempt before the sender is called; a reconciliation through another provider, version or
set of reviewed terms is refused even with its own approval, and an attempt with no such record stays unresolved.
Inside the recording transaction the entry is checked again against everything the approval covers, and the time
limit, validity and clock watermark are judged again after the lock is held.

Each reconciliation that reaches the provider appends one audit entry to the operation (at most 50): who, when,
under which approval record, which provider contract and provider record, the answer's hash, the decision, and the
entry before and after. A late acknowledgment from the original sender is still recorded afterwards and, if it
contradicts a recorded non-delivery, says so (`proof_contradicted_by_late_ack`).

Needed before it could be used, none of which exists: a status adapter for a real provider; the provider evidence
listed above (in particular a per-attempt terminal status that distinguishes "not seen yet" from final, and a closed
attempt that is rejected if it arrives later); an approval authority that can issue per-attempt reconcile approvals;
and an owner's decision on who may reconcile. There is no operator screen. Sending a report again after a recorded
non-delivery is not offered by any code path: it is a person's decision and a new operation.

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

## Readiness inventory (offline, read-only)

`scripts/last_scan_readiness_inventory.py` answers one question for an operator: **what still blocks a pilot, and what
exactly do I ask or supply next?** It reads one JSON file (the *readiness packet*: non-secret configuration, schema and
contract metadata), runs the existing validators over it (`last_scan_readiness.validate`, the real
`last_scan_hosted.preflight`, `last_scan_authority.problems`, `last_scan_transport.verified`) and prints a report. It
opens no store, calls no source, provider or network, reads no key, sends nothing and writes nothing.

    PYTHONPATH=. python scripts/last_scan_readiness_inventory.py docs/examples/last_scan_readiness_packet.synthetic.json \
        --now 2026-11-10T19:00:00+00:00                 # human-readable
    PYTHONPATH=. python scripts/last_scan_readiness_inventory.py PACKET.json --format json    # or: both

Exit code is **1** whenever a report was produced (the command never reports readiness, so it never exits 0) and **2**
when the packet file could not be read or `--now` is not an offset-aware time. `--now` defaults to the current time;
the synthetic example's approval is valid 2026-11-10 to 2026-11-13, so pass the `--now` above to see it accepted on
packet data.

**What the five levels mean.** Every item reports all five; this command can only ever raise the first two.

| Level | Meaning | Can this command show it? |
| --- | --- | --- |
| `code_tested` | the code path exists and passes synthetic tests | yes (always "synthetic tests only") |
| `configured` | present in the packet and accepted by the validators *as supplied* | yes |
| `connected` | a real system answered with real credentials | never: no call is made |
| `independently_verified` | someone other than the configurer checked the real source | only "claimed in packet, not verified" |
| `production_authorized` | a real authority approved it and a reviewed provider is registered | never |

A hash, a `verified` flag, a signature field or a self-attestation in a packet proves nothing about where the packet
came from; the report says "presented", never "authenticated". Evidence entries are echoed with who, when (and age) and
kind (`independent`, `self_attested`, `synthetic`, otherwise `unstated`).

**Packet shape** (`"packet": "last_scan.readiness_packet", "version": 1`): `synthetic` (anything but `false` is treated
as synthetic), `prepared_by`, `prepared_at`, `pilot_principal`, `config` (the runner configuration with `assignments`),
`job_registry`, `schemas`, `capture_receipts`, `approvals`, `accepted_collectors`, `accepted_issuers`,
`provider_contract` (the contract's fields; never registers it), `recipient_policy` (client id to internal audience),
`evidence` (`topic` of `scan_time_meaning`, `shift_schedule` or `report_template`, with `client_id`, `job`, `shift`,
`kind`, `by`, `at`, `claim`, `reference`) and `operations` (`hosting`, `scheduler`, `durable_store`, `recovery`,
`operator_workflow`, `approvals_process`, `recipient`, `retention`, each `{decided, by, at, reference}`).
**Never put a key, token, password or connection string in a packet.** Anything that looks like one is replaced before
the report is built, its location (not its value) is listed, and it becomes a blocker.

**What the report lists.** Per client / job: source mapping and scan-time meaning, registry entry, internal recipient,
report header/template/photos; per shift: shift end, quiet-time, lunch and offline-capture questions; then identity,
approval authority, capture authority, provider/sender/status adapter, credentials (always UNKNOWN: nothing is read),
and hosting, scheduler, store, recovery, operator workflow, approvals process, recipient and retention decisions; the
hosted preflight result on packet data; the residual limits that remain even when everything is answered (including
that a withdrawal at an authority after the last re-check is not atomic with the read or send).

The example packet is **synthetic** (invented clients, `example.sharepoint.com`), built from the rehearsal fixtures. It
demonstrates the format only; a passing check on it is not evidence about any real system.

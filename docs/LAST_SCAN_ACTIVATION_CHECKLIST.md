# Last-scan draft runner: activation checklist

Status on branch `claude/durable-report-runner-20261118`: **prepared, disabled, no production wiring in the reviewed
branch.** That means: no route, cron entry, scheduled task or production call that creates its tables, no secret, and
no real sender, status adapter, identity system or authority, in this branch's code. It is **not a statement about
any live environment**: no deployment, hosted environment or live schedule was inspected for this document. Inside
the branch the modules do import each other (the send and reconciliation boundaries, the readiness inventory script
and the tests import the entry-point module), and the tests create the tables in disposable databases.
No real-source validation evidence is recorded for this isolated runner branch; separate reporting work is outside
this document. A draft is due one hour after the last scan: that is Lauren's explicit requirement, not a placeholder. Nothing in this document has been done. Each step below needs its own approval.
Stop at the first step that cannot be completed.

For the operator's view (what to supply first, and the manual draft comparison that does not wait for any of this),
read `docs/LAST_SCAN_PILOT_HANDOFF.md`. This checklist covers the automated runner and approved automated delivery to Lauren only.

## What exists (code only)

| Piece | File | State |
|---|---|---|
| Entry point `handle(method, headers, body, deps)` | `hosting/last_scan_hosted.py` | No production wiring in this branch: no route, cron or app module calls it. Imported by the send and reconciliation boundaries, the readiness inventory script and tests. |
| Runner, worker, timer | `hosting/last_scan_runner.py`, `last_scan_service.py`, `last_scan_drafts.py` | Off unless `enabled` is exactly `true`. |
| Durable store | `hosting/last_scan_store.py` | PostgreSQL tables are created only by an explicit `install()`; no production path in this branch calls it. Tests call it against disposable databases. |
| Draft storage | `BlobArtifacts`, `StoredDraftPublisher`, `StoredDraftRouter` in `hosting/last_scan_local.py` | Drafts as private blobs in the same store. |
| Delivery gate and outcome contract | `hosting/last_scan_transport.py` | `VERIFIED_CONTRACTS` is empty: no sending, no retry. `deliver` also needs a single-use grant stored by the dispatch boundary. Provider answers are judged against a version 1 outcome contract; only a stated delivery counts. |
| Dispatch boundary (the send door) | `hosting/last_scan_dispatch.py` | No production wiring in this branch; imported by the reconciliation module and tests. It calls a sender **supplied by the host**; no sender is implemented. Every dependency defaults to deny; tested against fakes only. |
| Reconciliation boundary | `hosting/last_scan_reconcile.py` | No production wiring in this branch; imported by tests only. Records a provider's stated outcome for one uncertain attempt, with its own approval and audit trail; never resends. Needs a status adapter supplied by the host; none is implemented. |
| Authority record contract | `hosting/last_scan_authority.py` | Version 1 shape and lifecycle rules for approvals and capture receipts. Defines the contract; is not an authority. |
| Source-mapping readiness | `hosting/last_scan_readiness.py` | Offline check; a mandatory preflight. Never marks a mapping verified. |
| Schema capture | `hosting/last_scan_schema_capture.py` | Manual, GET-only, columns only. No default transport; run only by hand per its runbook. |
| Readiness inventory | `scripts/last_scan_readiness_inventory.py` | Offline blocker list from a non-secret packet. Never reports readiness. |

## Order of activation (none of it done)

1. **Merge prerequisites.** The Fix-with-AI audit repair (separate branch) and this branch are reviewed and merged
   by the owner. `tests/test_ai_proposal_guards.py::test_http_cross_report_access_never_calls_model_or_saves`
   is **still open** and belongs to the main auditor, not to this branch: observed here on 2026-11-18, it passes
   when the suite runs with `CI_LOCAL_DEV=1` (as the full-suite runs for this branch do) and fails without it
   (the server answers 403 where the test expects 404). Which answer is right is the auditor's decision.
2. **Database migration (needs approval).** Run `PostgresStore(connect, schema='ci_verifier').install()` once. It
   creates `scan_draft_state` and `scan_draft_blob`. Check the role's connection limit first: every record
   transaction opens one connection. Re-run the real-PostgreSQL tests against a disposable copy of the hosted
   version and through the hosted pooler; today they are proven on local PostgreSQL 17 only.
3. **Configuration under `last_scan_runner`.** Written by a person, per client list:
   `verified`, `active`, `timestamp_semantics_verified`, `client_id`, `job`, `site_id`, `list_id`, `zone`,
   `scan_field` (a verified actual-scan time; never Date, Date0, Created or Modified), `shift_field`,
   `recorded_shifts`, optional `business_dates` and `not_before`, and this list's own `mapping` and `header`.
4. **Job registry.** A verified registry entry with the exact `client_key`, `site_id`, `list_id` and `job`.
   Seed entries carry site names, not site ids, and do not match.
5. **Existing scheduled reporting.** Confirm the current scheduled reporting and the jobs it covers with the root
   owner and the report owner; it was not inspected for this document. Mark each confirmed covered job
   `externally_built: true` here. Do not modify that schedule here.
6. **External systems this code only has interfaces for.** None is implemented, and none should be faked in
   production. For a run: the three below, plus `CaptureAuthority`, the accepted collector and issuer lists and a
   `SchemaProbe` (see the mapping and provenance sections further down). For a send: also a recipient policy, a
   dispatch quota, a sender and a reviewed provider contract. For reconciliation: also a status adapter and its
   own quota.
   - `Authorizer`: verifies the caller and returns a `Principal`. It is given the hash of the exact request body
     so a signature can cover the timestamp and nonce. A shared static token is not accepted anywhere.
   - `ApprovalAuthority`: stored where the runner and the configuration editor cannot write. It answers for one
     `snapshot_digest` with exactly one version 1 record (issuer, record id, status, validity bounds, optional
     revocation fields): see "Authority adapter contract" in `docs/LAST_SCAN_REHEARSAL.md`. The host also supplies
     the accepted issuer identities (`Deps.issuers`); without them every record is refused.
   - `Quota`: runs per hour for one principal and one approved snapshot.
7. **Dry run.** Call the entry point with `mode: "dry_run"` (the default). It reads no source and writes nothing.
   It returns `snapshot_digest`: one hash over the configuration, each routed assignment's renderer version
   (mapping, header, pinned template, generator code, binding) and the job registry as supplied.
8. **Approval.** A person records that `snapshot_digest` and the allowed principals in the approval authority.
   Any change to configuration, header, mapping, template, generator code or registry changes the digest, and
   runs are refused until it is approved again. An `activation` block inside the configuration is ignored.
9. **Host wiring (a reviewed code change).** One route that builds `Deps` and calls `handle`. Renderers are built
   inside `handle` from the configuration; a caller cannot supply them. Budget is capped at 45 seconds.
10. **Schedule (a separate decision).** A scheduled caller with a unique `run_id`, a timestamp and a single-use
    nonce per firing. Not created here.
11. **First live run** on one list, watched by a person; compare the draft with the report built the usual way.
    The available draft and its checklist go to Lauren even when the comparison finds gaps; she decides hold or release.

## What a run request must pass, in order

1. An `Authorizer` is configured, the headers are well formed, and it returns a `Principal`. Otherwise 503 or 401.
2. POST, a plain JSON body with only the allowed fields, finite numbers, `max_scopes` a whole number.
3. `timestamp` within 5 minutes of the host clock.
4. For a run: a `nonce` of 16 to 200 characters.
5. Every preflight check (18): `configuration_present`, `enabled_is_exactly_true`, `job_registry_available`,
   `schema_snapshots_supplied`, `capture_authority_configured`, `accepted_collectors_configured`,
   `accepted_authority_issuers_configured`, `live_schema_probe_configured`,
   `source_mapping_ready_for_every_assignment`, `renderers_built_from_configuration`, `source_adapter_configured`,
   `durable_store_ready` (read-only), `host_clock_not_behind_the_last_seen_time`, `at_least_one_runnable_scope`,
   `no_assignment_issues`, `approval_attested_for_this_snapshot_and_principal`, `quota_configured`,
   `delivery_transport_disabled`.
6. The nonce and the hourly quota are taken in one transaction. A used nonce gives 409, an exhausted quota 429.
   Only after this is any source read.

Known boundaries: nonces are never pruned; the quota is a simple hourly counter; nothing limits dry runs; a
compromised authorizer or approval authority defeats everything above; replay protection depends on the host clock.

## Approved automated delivery to Lauren: disabled, and what would be needed

The only recipient this code allows is Lauren, the user-authorized private recipient. It never delivers to a client;
client release is her decision, made outside the runner.

There is **no real provider, sender or status adapter**, and none should be invented. The code path for a send
exists and is closed: the dispatch boundary would call a host-supplied sender, but only after every run gate above
and its own checks (`recipient_policy_configured`, `recipient_is_the_approved_one_for_this_client`,
`scope_is_exactly_one_approved_planned_scope`, `dispatch_approved_for_this_snapshot_recipients_contract_and_principal`,
`authorizations_stay_valid_long_enough_to_send`, `provider_contract_verified`,
`provider_contract_states_outcome_contract_deadline_and_validity`, `dispatch_sender_configured`,
`dispatch_quota_configured`), and `provider_contract_verified` cannot pass while the registry is empty. Drafts are
built, held, and readable through `StoredDraftPublisher.read(op, name)`; an internal outbox entry is recorded and
never attempted. The recipient is Lauren only; a client is never a recipient.

Before any transport could be enabled, an independent reviewer would need written evidence from the provider that:

1. it delivers at most once per idempotency key (the operation ID);
2. it can state, for one attempt ID, a **final** not-delivered status, distinct from "not seen yet";
3. once it reports an attempt closed, it rejects that attempt if it arrives later;
4. its send call is cut off within a known time, shorter than the runner's `attempt_seconds`;
5. how each of those was tested, by whom, and when.

Only then would a `ProviderContract` with those facts be added, by reviewed code change, to
`VERIFIED_CONTRACTS` in `hosting/last_scan_transport.py`. Without it: `deliver` refuses, an uncertain delivery is
never retried, and a passed deadline changes nothing. A reconciliation boundary exists
(`hosting/last_scan_reconcile.py`, "Reconciling an uncertain delivery" in `docs/LAST_SCAN_REHEARSAL.md`): given a
verified principal, its own approval, a listed provider contract and a host-supplied status adapter, it records what
the attempt's own provider states, keeps an audit trail and never resends. With no listed contract and no status
adapter it refuses every request, so today it queries nothing. A withdrawal at an external authority after the last
re-check and before the read, send or recording is not atomic with it. Even with a listed contract, the
claim is not exactly-once: the source can change between the recapture and the provider receiving the message, the
claim and the send are separate steps, and a provider that breaks its contract can cause a second delivery (which is
recorded, not prevented).

## Independent review

```sh
cd /Users/lauren/projects/ppg-claude-report-runner-20261118
export PY=/Users/lauren/projects/ppg-verifier-claude-pilot-7Nepp5hs/test-venv/bin/python
export PYTHONPATH=.:tests TMPDIR=$PWD/.tmp PYTHONDONTWRITEBYTECODE=1
$PY -m pytest -q -p no:cacheprovider tests/test_last_scan_drafts.py tests/test_last_scan_local.py \
    tests/test_last_scan_service.py tests/test_last_scan_durable.py tests/test_last_scan_runner.py \
    tests/test_last_scan_hosted.py tests/test_last_scan_hosted_dispatch.py tests/test_last_scan_reconcile.py \
    tests/test_last_scan_authority_lifecycle.py tests/test_last_scan_transport_outcome.py \
    tests/test_last_scan_readiness.py tests/test_last_scan_readiness_inventory.py \
    tests/test_last_scan_schema_capture.py tests/test_one_hour_rehearsal.py tests/test_explain.py
PY=$PY sh scripts/last-scan-pg-test.sh       # disposable PostgreSQL in .tmp/pg, unix socket only, deleted afterwards
$PY scripts/last_scan_synthetic_demo.py $PWD/.tmp/demo
```

Things worth trying to break:
- enable a retry without editing `VERIFIED_CONTRACTS` (`NoRetryByDefault` in `tests/test_last_scan_durable.py`);
- reach a source read or a store write through `handle` without a verified principal, an attested approval for the
  exact snapshot, a fresh single-use nonce, quota, or a passing preflight (`tests/test_last_scan_hosted.py`);
- send or retry by handing the worker a registry of your own (`NoRetryByDefault`);
- find any import of `last_scan_*` from `api/`, `web/`, `vercel.json` or another `hosting/` module (`NotConnected`);
- reach the sender without a stored single-use grant, or with a provider contract that is not listed
  (`tests/test_last_scan_hosted_dispatch.py`);
- record a delivery outcome for another provider's attempt, or after an approval lapses
  (`tests/test_last_scan_reconcile.py`).


### Hosted authorization snapshot isolation

`handle` takes private deep copies of the request, stored configuration and job registry before calling
identity or approval dependencies. The copied inputs are retained through hashing, planning, approval and
execution; a concurrent edit of the caller-owned objects cannot replace the approved scope mid-run.
Hosted run receipts are separately keyed by verified principal, approved snapshot digest and caller run ID.
Reusing a run ID with a different principal or approved snapshot never returns another run's receipt.
Existing unscoped local receipts are not reused by hosted calls; source operation idempotency remains intact.
These boundaries are regression-tested with synthetic identity/approval systems only. They do not configure
an actual identity provider, approval authority, delivery provider, route or schedule.

## Source-mapping readiness (mandatory preflight, added 2026-11-18)

The hosted entry point refuses every request unless, for each configured assignment, exactly one captured schema
snapshot is supplied and the offline readiness check passes against it
(see `docs/LAST_SCAN_SOURCE_MAPPING_READINESS.md`). Before activation, still open:

- [ ] A real capture authority and collector. The code only defines the interface (`CaptureAuthority`) and a
      server-side list of accepted collector identities (`Deps.collectors`). Neither exists for production; the
      only implementations are test fakes. Until one exists every request is refused. The entry point reports a
      receipt as "reported by capture authority"; it cannot verify the authority itself.
- [ ] A real read-only schema probe (`SchemaProbe`). Interface only; test fake only. Without one, no run.
- [ ] Owner decision on the bounds, which are placeholders: capture age 24 hours, future skew 5 minutes, probe
      answer age 60 seconds. The one-hour wait after the last scan is **not** one of them: it is Lauren's explicit
      requirement. What needs verification around it is the actual-scan column, the shift and lunch policy, and how
      late-arriving rows are handled.
- [ ] Named people to attest each business meaning per list, and the approver's review of those attestations.
      An approval covers the exact snapshot and contract by hash; it does not show the approver examined them.
- [ ] Runtime source proof. Readiness says the configuration and a captured schema agree. It does not show what
      the live source returns, and nothing here establishes what the scan field means.

## Schema capture provenance and live drift (added 2026-11-18)

What a run now requires, in addition to everything above:

1. For each supplied snapshot, the capture authority returns a receipt for exactly that snapshot (by hash of the
   whole snapshot), naming the same site and list, the same column-definition digest, an accepted collector, and
   a valid offset-aware capture time. Fields inside the snapshot or the configuration are never read as proof.
2. The receipt's capture time is at most 24 hours old and at most 5 minutes in the future, and the snapshot's own
   capture time is the same instant. This is checked on every request, so an approved snapshot expires.
3. The collector and capture time are part of the approved snapshot digest.
4. After identity, approval, nonce and quota, and before any source read, the probe is asked for each list's
   current column definitions. They must be well formed, for that list, observed within 60 seconds, and identical
   to the approved definitions. Otherwise the request ends with `schema_drift` and nothing is read.
5. The same look is repeated immediately before every individual source read. A read whose look fails is not
   made; that scope is reported as an error (`SchemaDrift`).

### The race that remains

The look and the read are two separate calls to the source. A column can change in the moment between them, and
this code cannot close that gap from the outside: it would need the source to return the schema version with the
rows, or to accept a conditional read. Until then the window is one probe-to-read interval per read, and the
sealed capture taken by the read is the only record of what was actually returned. A failed look also spends the
request's nonce and one unit of quota, by design, so the probe cannot be used as a free polling channel.

### Accepted-collector policy (added 2026-11-18)

The list of accepted collectors is copied into a private immutable set before any callback runs, used unchanged
for the whole request, and is part of the approved snapshot digest. Adding a collector while a request is in
flight cannot let it through; removing one cannot change a request already being judged; the next request sees
the new list, and a changed list needs a new approval. This is a wiring boundary. Supplying these objects to the
entry point does not make them authentic, and nothing in this code proves a collector or authority is genuine.

## Before working through this checklist

Run the offline readiness inventory (`docs/LAST_SCAN_REHEARSAL.md`, "Readiness inventory") on a non-secret packet for the
pilot list. It lists the open blockers and the exact questions to ask; it cannot tick any item on this checklist, and
its output is not an approval.

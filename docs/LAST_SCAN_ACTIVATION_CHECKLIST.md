# Last-scan draft runner: activation checklist

Status on branch `claude/durable-report-runner-20261118`: **prepared, disabled, not connected.** No route, cron,
scheduled task, database table, secret or sender exists for it. Nothing in this document has been done.
Each step below needs its own approval. Stop at the first step that cannot be completed.

## What exists (code only)

| Piece | File | State |
|---|---|---|
| Entry point `handle(method, headers, body, deps)` | `hosting/last_scan_hosted.py` | Not imported by anything. |
| Runner, worker, timer | `hosting/last_scan_runner.py`, `last_scan_service.py`, `last_scan_drafts.py` | Off unless `enabled` is exactly `true`. |
| Durable store | `hosting/last_scan_store.py` | PostgreSQL tables are created only by an explicit `install()`; never called. |
| Draft storage | `BlobArtifacts`, `StoredDraftPublisher`, `StoredDraftRouter` in `hosting/last_scan_local.py` | Drafts as private blobs in the same store. |
| Delivery gate | `hosting/last_scan_transport.py` | `VERIFIED_CONTRACTS` is empty: no sending, no retry. |

## Order of activation (none of it done)

1. **Merge prerequisites.** The Fix-with-AI audit repair (separate branch) and this branch are reviewed and merged
   by the owner. The known `test_ai_proposal_guards` 403/404 failure is resolved by the main auditor.
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
5. **Existing scheduled reporting task.** It already builds reports for a fixed list of jobs. For each such job
   either set `externally_built: true` here or retire it there first. Do not change that schedule as part of this.
6. **Three external systems this code only has interfaces for.** None is implemented, and none should be faked in
   production:
   - `Authorizer`: verifies the caller and returns a `Principal`. It is given the hash of the exact request body
     so a signature can cover the timestamp and nonce. A shared static token is not accepted anywhere.
   - `ApprovalAuthority`: stored where the runner and the configuration editor cannot write. It answers for one
     `snapshot_digest` with `{snapshot_digest, approved_by, approved_at, principals}`.
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
11. **First live run** on one list, watched by a person; compare the draft with a hand-built report.

## What a run request must pass, in order

1. An `Authorizer` is configured, the headers are well formed, and it returns a `Principal`. Otherwise 503 or 401.
2. POST, a plain JSON body with only the allowed fields, finite numbers, `max_scopes` a whole number.
3. `timestamp` within 5 minutes of the host clock.
4. For a run: a `nonce` of 16 to 200 characters.
5. Every preflight check: `configuration_present`, `enabled_is_exactly_true`, `job_registry_available`,
   `renderers_built_from_configuration`, `source_adapter_configured`, `durable_store_ready` (read-only),
   `at_least_one_runnable_scope`, `no_assignment_issues`, `approval_attested_for_this_snapshot_and_principal`,
   `quota_configured`, `delivery_transport_disabled`.
6. The nonce and the hourly quota are taken in one transaction. A used nonce gives 409, an exhausted quota 429.
   Only after this is any source read.

Known boundaries: nonces are never pruned; the quota is a simple hourly counter; nothing limits dry runs; a
compromised authorizer or approval authority defeats everything above; replay protection depends on the host clock.

## Delivery: disabled, and what would be needed

There is no provider, so there is no sender, and none should be invented. Drafts are built, held, and readable
through `StoredDraftPublisher.read(op, name)`; an internal outbox entry is recorded and never attempted.

Before any transport could be enabled, an independent reviewer would need written evidence from the provider that:

1. it delivers at most once per idempotency key (the operation ID);
2. it can state, for one attempt ID, a **final** not-delivered status, distinct from "not seen yet";
3. once it reports an attempt closed, it rejects that attempt if it arrives later;
4. its send call is cut off within a known time, shorter than the runner's `attempt_seconds`;
5. how each of those was tested, by whom, and when.

Only then would a `ProviderContract` with those facts be added, by reviewed code change, to
`VERIFIED_CONTRACTS` in `hosting/last_scan_transport.py`. Without it: `deliver` refuses, an uncertain delivery is
never retried, reconciliation does not query anything, and a passed deadline changes nothing. Even with it, the
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
    tests/test_last_scan_hosted.py tests/test_explain.py
PY=$PY sh scripts/last-scan-pg-test.sh       # disposable PostgreSQL in .tmp/pg, unix socket only, deleted afterwards
$PY scripts/last_scan_synthetic_demo.py $PWD/.tmp/demo
```

Things worth trying to break:
- enable a retry without editing `VERIFIED_CONTRACTS` (`NoRetryByDefault` in `tests/test_last_scan_durable.py`);
- reach a source read or a store write through `handle` without a verified principal, an attested approval for the
  exact snapshot, a fresh single-use nonce, quota, or a passing preflight (`tests/test_last_scan_hosted.py`);
- send or retry by handing the worker a registry of your own (`NoRetryByDefault`);
- find any import of `last_scan_*` from `api/`, `web/`, `vercel.json` or another `hosting/` module (`NotConnected`).


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

- [ ] Who captures schema snapshots read-only, and how a snapshot is shown to come from the real list. Today the
      entry point trusts whatever the host supplies; it has only been exercised with synthetic snapshots.
- [ ] How old a snapshot may be. No maximum age is enforced.
- [ ] Named people to attest each business meaning per list, and the approver's review of those attestations.
      An approval covers the exact snapshot and contract by hash; it does not show the approver examined them.
- [ ] Runtime source proof. Readiness says the configuration and a captured schema agree. It does not show what
      the live source returns, and nothing here establishes what the scan field means.

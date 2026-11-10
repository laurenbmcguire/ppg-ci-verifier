# Audited isolated self-cycle recovery

This admin-only action restores review controls for a provably isolated legacy
self-link. It never changes uploaded bytes, invents a parent, resets revision
numbers, clears findings or source holds, approves, or sends a report.

Preview verifies original byte integrity, clean base and first check, all prior
checks, no approval/send history, stable self-parent and one self-correction,
and no other base/history referencing this file. The token binds the full saved
inventory and exact current check hash. Explicit confirmation recomputes this
proof under locks and appends one audited check. Old checks remain unchanged.

The receipt seals the prior prefix, actor, previous check and ignored self-link.
Only validated sealed-prefix self-edges are excluded from the effective graph.
Future ancestry retains reciprocal-edge and monotonic-history guards. Invalid
receipts stay unavailable and are labeled unverified.

Authentication is refreshed at the action boundary. Native database recovery
writes commit only with the outer workspace, with no global PgSession change.
Failed, cancelled, stale and uncertain saves never optimistically unlock the UI.
Normal AI/manual/decision controls return after the saved version is read.
History retains earlier self-link claims as unverified, never proven changes.

## Synthetic gates
- 46 focused Python tests plus 11 subtests.
- 19 native PostgreSQL tests including restart, outer rollback, concurrent
  confirmations, stale inventory, explicit consent and held-cell enforcement.
- 303 Node tests.
- Real-handler Chromium at 1200/390: cancel, refusal, successful recovery,
  restored manual review, source/decision/history retention and stale rejection.
- Independent real-cookie HTTP: admin preview, member/anonymous denial, missing
  confirmation, session revocation/demotion after initial gate and stale checks.
- Full native aggregate evidence is retained separately.

Live recovery remains a separate explicitly confirmed operation. Publishing
this code does not recover any report, refresh source metadata, or verify data.

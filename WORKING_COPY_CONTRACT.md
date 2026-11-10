# Working-copy and historical-evidence contract

Workbook bytes are immutable. Normal browser opens resolve the authoritative stored correction graph before editing. Historical browser views are read-only and retain exact-version downloads. Existing exact-hash APIs remain exact; this change does not silently redirect a mutation or rewrite its requested hash.

## Enforcement

- `apply_fixes` checks the parent under its writer lock before storing a new child. Superseded or unverifiable history cannot create new working content. Approved, sending and sent copies cannot be corrected. A proven exact-output match within the same stored ancestry remains a read-only reuse outcome, distinct from current-copy resolution.
- AI correction proposal preparation and persistence require a verified current parent. This check occurs before provider preparation and again before recording proposals.
- The resolver loads complete stored ancestry rather than the bounded UI inventory. Missing, conflicting, cyclic or nonreciprocal links fail closed. Destination and ancestor access are checked; inaccessible competing branches cannot be filtered away to invent a unique target.
- Downloads and exact report reads preserve their requested hash. A legacy self-cycle is explained as inconsistent saved history, never silently repaired.

## Deliberately retained exact-version metadata behavior

This release does **not** promise that all historical metadata is immutable through every API.

- Finding decisions remain exact-version, revision/run-bound metadata. Existing approval/sending/sent guards apply, and request replay stays idempotent. Superseded status alone does not prohibit an authorized historical decision.
- Source capture/comparison, baseline and advisory review results may append evidence to the exact historical version. They do not alter workbook bytes or create a corrected child. Existing source freshness, identity, assignment and capture bindings continue to apply.
- Explicit checker revalidation may append a check revision to a historical version. It retains workflow/lineage/sending state and now refuses if the version changed while checks were prepared.
- Legacy structured-source reconciliation now preserves approval, sent/sending, decisions and correction ancestry instead of discarding them while rebuilding checks. An older separate source reconciliation is marked stale rather than treated as current evidence.
- Assignment remains an authorized exact-hash settings/audit operation. Its existing optional remembered mapping behavior is unchanged. Assignment changes can affect current access and source/approval applicability; they are not correction ancestry.
- Existing approval policy explicitly permits an authorized, reviewed override of a superseded version, subject to hard holds and its required reason. This change does not remove that override or automatically withdraw approval. Existing explicit withdrawal and sent/sending protections remain in place.
- Internal `save_check` and `_update` are not globally blocked: they also implement correction lineage bookkeeping and preserved historical evidence. Blanket blocking would break these supported operations.

Browser historical read-only controls are therefore a navigation/editing contract, backed by server-side correction/proposal guards, not a new universal metadata retention policy. Changing the remaining evidence or approval policy requires a separate explicit decision.

## Recovery boundary

A self-cycle can be produced by legacy duplicate-output fallthrough. Review the append-only check sequence and original stored upload identity before any recovery. No history migration, reparenting, deletion, approval withdrawal or workbook transformation is part of this repair.

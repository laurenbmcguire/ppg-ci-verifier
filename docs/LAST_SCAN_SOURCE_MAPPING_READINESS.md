# Source-mapping readiness check (dry run only)

`hosting/last_scan_readiness.py` answers one question for a reviewer: *is this assignment's mapping pinned and
evidenced well enough to try a dry run?* It reads nothing live. It is given a schema snapshot someone captured
read-only, the assignment, the trusted registry entries and, optionally, sample rows.

## Two kinds of fact, never mixed

| Basis | What it is | Where it comes from |
|---|---|---|
| `discovered` | Column IDs, types, date format, choices, read-only flags, shared display names | The schema snapshot |
| `configured` | Site/list IDs, field mapping, pinned column IDs and types, shifts, zone, header | The assignment |
| `attested` | What a column *means* for the business | A named person, with evidence, bound to the exact contract |

Metadata never proves meaning. A date-and-time column is not thereby the scan time. The tool never suggests a
scan field, never infers one from Created, Modified or the report date, and never writes an attestation or a
`verified` flag. Every report says `marked_verified_by_this_tool: false`.

## What blocks

- Schema missing, or for another site or list; site or list given as a name or URL instead of an exact ID.
- No single exact registry entry for the client and job.
- A mapped column missing, replaced (column ID changed), retyped, or sharing a display name with another column.
- A snapshot that contradicts itself about which column is which: an entry whose own internal name is not the key
  it is filed under, a missing or shared column ID, or keys that differ only by case or padding. Re-pinning and
  attesting cannot repair this; it needs a clean capture.
- Any mapped column that carries a `calculated` facet, whatever its value and whatever other facet sits beside it.
- Any mapped column with more than one type facet, an empty or malformed facet, a non-boolean flag, or a property
  the validator does not recognise. It does not guess that an unknown property leaves the type alone.
- Any mapped column whose full definition (every facet and its contents, read-only, hidden, required, indexed,
  default value, validation, names) differs from the hash pinned in `field_contract[...].definition`. Re-pinning
  changes the contract, so the attestations bound to it go stale and must be given again.
- A scan column with any default value (a default of "now" would stamp creation time).
- Malformed input at any depth: damaged column entries, a field contract or sample rows of the wrong shape, a
  malformed list identity or registry. These become named blocked reasons, not exceptions.
- A report field unmapped, unpinned, or one column mapped to two fields.
- Scan field absent, or Created/Modified/the report date, date-only, computed, read-only or not a date-and-time.
- Shift values not listed exactly once, not mapped to a template shift, missing from the column's choices, or a
  column choice nobody accounted for.
- Zone that is not a real IANA zone.
- Header field the template does not have.
- Any of the seven business meanings without an attestation naming who, when and what evidence, bound to the
  current contract. Changing the contract makes the attestation stale.
- `verified` or `timestamp_semantics_verified` set without those attestations.
- Sample rows (if supplied) whose scan value is the same instant as Created or Modified in every row, compared as
  instants whatever offset or fractional form each is written in. Values with no valid offset are not compared
  and no timezone is assumed for them; the report counts them as not comparable.
- Sample rows (if supplied) whose scan value equals Created, is missing or has no offset, or whose shift is
  blank or not a recorded shift.

Blank or absent quantities in sample rows are counted and reported, not judged and never treated as zero.

## Output

`validate(...)` returns `blocked_reasons`, `missing_evidence` (what a person must supply, including the binding
digest to attest against), every check with its basis, and `status` of `blocked` or `ready_for_dry_run_only`.
`report_text(report)` renders it for a reviewer.

## Errors

Malformed input never raises. There is no catch-all: a bug in the validator or the registry rule, or a broken
pinned template, raises instead of being reported as a blocked reason.

## Limits

- The list of recognised column properties was written from documentation, not from a real capture. A real
  capture may be blocked by a property not on the list; that is the safe direction, and the list then needs review.

- Exercised on synthetic schemas and rows only. No real list has been checked.
- It is a standalone check. The runner and hosted entry point do not call it yet; their existing gates
  (off by default, exact registry binding, `verified` flags, scan-field refusal) are unchanged.
- "Ready for a dry run" is not production readiness and authorises no read, run, send or deployment.
- An attestation is a record that a person vouched; the tool cannot check that the person was right.

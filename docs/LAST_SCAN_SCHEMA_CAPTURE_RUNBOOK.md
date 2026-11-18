# Capturing one list's schema, read-only (manual, root-owned)

`hosting/last_scan_schema_capture.py` reads one list's identity and column definitions and returns a **candidate
evidence artifact**. It is not connected to anything, has no default transport or clock, and has only ever been run
against a mocked transport with synthetic columns. This runbook is for the root owner to inspect one real list by
hand. Nobody else, and no scheduled job, should run it.

## What it does and does not do

- GET requests only, to exactly two addresses: the list itself (id, name, web address, eTag, last-modified,
  parent) and its `columns` collection. It never requests list items, never expands, never writes.
- Reads the columns **twice** and refuses unless both reads are identical.
- Refuses on any answer other than HTTP 200: redirects (never followed), 401/403, 404, 429/503 (the wait the
  source asked for is recorded and no retry is made), server errors, unreachable.
- Refuses a next-page link that leaves this list's `columns` collection on `graph.microsoft.com/v1.0`, a repeated
  cursor, a malformed page, an answer for another list or site, duplicate or unidentified columns.
- Keeps every column definition exactly as returned, including properties it does not recognise, and lists those
  under `columns_needing_review`.
- Dates the capture from the host clock you give it. Source timestamps are kept only as quoted text.
- Does **not** read rows (`samples.status` is `not_performed`), decide which column is the scan time
  (`field_candidates.scan_field` is `not_determined`), mark anything verified, or grant any authority.

## Before you start

1. You are the root owner, on the approved machine, using the existing read-only app registration. Do not create
   or change any credential, permission or setting for this.
2. You have the exact site id (`host,site-guid,web-guid`) and list GUID from the verified job registry. Names and
   URLs are refused.
3. Decide where the artifact will be kept. It contains the real list's column names. Do not commit it to the
   repository, paste it into chat, or send it to anyone outside the review.

## The read (one list, by hand)

From the repository root, in a Python session started by you:

```python
import json
from hosting import source_first as sf
from hosting.source_first_capture import graph_fetch          # existing: one GET, no redirects, no retries
from hosting.last_scan_schema_capture import capture_schema, SchemaCaptureFailure

SITE = '<host,site-guid,web-guid>'
LIST = '<list-guid>'
try:
    artifact = capture_schema(SITE, LIST, fetch=graph_fetch, clock=sf.now)
except SchemaCaptureFailure as failure:
    print(failure, failure.evidence)          # nothing was accepted; if it names a cooldown, wait that long
else:
    with open('/path/outside/the/repo/schema-capture.json', 'x') as out:
        json.dump(artifact, out, indent=1)
```

This exact snippet has **not** been run against a real list. `graph_fetch` uses the app's existing sign-in; the
adapter itself never sees a token. Expect about six requests for a typical list.

## What to check in the artifact, by eye

- `snapshot.site_id` and `snapshot.list_id` are the ones you asked for; `snapshot.list.displayName` is the list
  you expected.
- `observed.complete` and `observed.passes_agree` are true; `observed.column_count` is plausible.
- `observed.site_identity_echoed_by_source`: if false, the source did not confirm the site in its answer. Note it.
- `columns_needing_review`: every entry needs a human decision. In particular, properties the validator does not
  recognise will block any mapping onto that column until the recognised-property list is reviewed.
- `field_candidates.by_discovered_type` is a list to read, not advice.

## What a real capture still has to establish (the artifact cannot)

1. That the transport was the authorised read-only reader, and which identity it ran as.
2. That the answers came from the real tenant.
3. A receipt recorded by a real capture authority. `receipt_candidate(artifact, '<collector identity>')` returns
   only the content such an authority would be asked to record; it is unsigned and is not a receipt. The hosted
   entry point reads receipts only from the authority, never from the artifact.
4. Which column, if any, is the actual scan time, and what every mapped column means. Named people must attest.
5. Whether each unrecognised column property matters.
6. Anything about rows. None were read.

## Limits you should know

- A change made **between the two passes** is caught. A change made before the first pass or after the second is
  not, and a change that is made and reverted between passes is not.
- The list's eTag and last-modified usually move when rows are added, so they are reported
  (`list_items_changed_during_capture`) and not treated as a schema change.
- Whether the source returns `parentReference.siteId` for this request, and which column properties a real list
  carries, are both unconfirmed until the first real capture.
- The capture is a point in time. The hosted entry point separately requires a fresh look before every read.

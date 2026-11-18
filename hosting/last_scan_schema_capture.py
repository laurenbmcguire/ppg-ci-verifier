"""Read-only schema snapshot collection for one SharePoint list. PREPARED, NOT CONNECTED, NOT A SOURCE OF TRUST.

``capture_schema`` makes GET requests only, through a transport the CALLER supplies, and returns a CANDIDATE
EVIDENCE ARTIFACT: the list's column definitions exactly as returned, normalised into the snapshot shape the
readiness validator reads, with a record of how complete the capture was.

What this is not:
  * It is not a collector identity and not a capture authority. It grants nothing. The artifact it returns is
    unsigned and unreviewed, and ``receipt_candidate`` is only the content a real capture authority would be asked
    to record. Until an authority records it, the hosted entry point refuses the snapshot.
  * It does not decide what any column means. It lists columns by discovered type so a person can review them. It
    never names a scan field, never suggests one and never marks anything verified.
  * It has no default transport and no default clock. Nothing imports it; using it against a real list is a manual,
    root-owned, read-only step described in docs/LAST_SCAN_SCHEMA_CAPTURE_RUNBOOK.md.

Fail closed: any non-200 answer (redirects, throttling, permission and server errors included), a malformed or
missing page, a next-page link that leaves the exact collection, a repeated cursor, an identity that does not match
the request, duplicate or unidentified columns, or two capture passes that disagree all raise
``SchemaCaptureFailure``. A partial snapshot is never returned.

Times recorded as observed come from the caller's clock. Timestamps in the source's own text are kept only as
quoted source text and are never used as the capture time.
"""
import re
from copy import deepcopy
from urllib.parse import quote

from hosting import source_first as sf
from hosting import last_scan_readiness as readiness
from hosting.last_scan_drafts import instant
from hosting.source_first_capture import CaptureFailure, collection, cooldown

KIND, FORMAT = 'candidate_schema_capture', 'schema-capture-v1'
MAX_COLUMNS = 2000
PASSES = 2                                   # the columns are read twice; the two reads must agree exactly
LIST_SELECT = 'id,displayName,webUrl,eTag,lastModifiedDateTime,parentReference'

MUST_BE_ESTABLISHED = (
    'That the transport used was the authorised read-only collector, and which identity it ran as. This code cannot see that.',
    'That the answers came from the real tenant. This code only sees what the transport returned.',
    'A receipt recorded by a real capture authority for exactly this snapshot. The receipt candidate here is unsigned.',
    'Which column, if any, records the actual scan time. Nothing here determines or suggests that.',
    'What each mapped column means for the business (shift values, business date, quantities, timezone). Needs a named person.',
    'Whether any column property this code does not recognise changes how the column behaves.',
    'Row-level behaviour. No list items were read; sample checks were not performed.',
)


class SchemaCaptureFailure(sf.ReviewError):
    """The capture did not complete. ``evidence`` says where it stopped; there is no partial snapshot."""
    def __init__(self, message, evidence):
        super().__init__(message)
        self.evidence = deepcopy(evidence)


def _fail(message, stage, **evidence):
    raise SchemaCaptureFailure(message, dict(stage=stage, accepted=False, **evidence))


def _status_message(status):
    if status in (401, 403):
        return f'The reader is not permitted to read this list (HTTP {status}).'
    if status == 404:
        return 'The exact site and list were not found (HTTP 404).'
    if status in (429, 503):
        return f'The source asked the reader to wait (HTTP {status}). No early retry is made.'
    if isinstance(status, int) and 300 <= status < 400:
        return f'The source answered with a redirect (HTTP {status}). Redirects are never followed.'
    if status == 0:
        return 'The source could not be reached.'
    return f'The source answered HTTP {status}.'


def _call(fetch, url):
    """One GET through the caller's transport, normalised. A transport that raises or answers in another shape is
    treated as "could not be reached"; either way nothing is accepted."""
    try:
        result = fetch(url)
    except Exception:                                                        # the transport is the caller's code
        return 0, None, {}
    if not isinstance(result, tuple) or len(result) not in (2, 3):
        return None, None, {}
    status, page = result[0], result[1]
    headers = result[2] if len(result) == 3 and isinstance(result[2], dict) else {}
    return (status if isinstance(status, int) and not isinstance(status, bool) else None), page, headers


def _now(clock, stage):
    value = clock()
    try:
        return value, instant(value)
    except (ValueError, OverflowError):
        _fail('The host clock did not return an offset-aware time, so the capture cannot be dated.', stage)


def _list_metadata(fetch, base, site_id, list_id, stage):
    url = base + '?$select=' + LIST_SELECT
    status, page, headers = _call(fetch, url)
    if status != 200:
        _fail(_status_message(status), stage, status=status, request_hash=sf.digest(url),
              cooldown=cooldown(headers) if status in (429, 503) else None)
    if not isinstance(page, dict) or page.get('id') != list_id:
        _fail('The answer is not for the exact list that was asked for.', stage, status=status, request_hash=sf.digest(url),
              returned_id=page.get('id') if isinstance(page, dict) else None)
    parent = page.get('parentReference')
    echoed = parent.get('siteId') if isinstance(parent, dict) else None
    if echoed is not None and echoed != site_id:
        _fail('The list answered as belonging to another site.', stage, status=status, request_hash=sf.digest(url), returned_site_id=echoed)
    if 'displayName' in page and not isinstance(page['displayName'], str):
        _fail('The list name in the answer is malformed.', stage, status=status, request_hash=sf.digest(url))
    return page, echoed


def _columns(fetch, base, pass_number):
    pages = []
    try:
        raw = collection(base + '/columns?$top=500', 'columns', pages, lambda url: _call(fetch, url), [])
    except CaptureFailure as error:
        e = error.evidence if isinstance(error.evidence, dict) else {}
        _fail(str(error), 'columns', capture_pass=pass_number, status=e.get('status'), cooldown=e.get('cooldown'),
              failed_request_hash=e.get('failed_request_hash'), pages=e.get('pages'))
    except (ValueError, TypeError):                                          # a page that is not plain JSON data
        _fail('A column page is not plain data.', 'columns', capture_pass=pass_number, status=None, pages=pages)
    if not pages or not pages[-1].get('terminal'):
        _fail('The column pages did not reach a terminal page.', 'columns', capture_pass=pass_number, pages=pages)
    if not raw:
        _fail('The source returned no columns at all, which no list has.', 'columns', capture_pass=pass_number, pages=pages)
    if len(raw) > MAX_COLUMNS:
        _fail(f'The list has more than {MAX_COLUMNS} columns; nothing was truncated and nothing was accepted.', 'columns', capture_pass=pass_number, pages=pages)
    by_name, ids, folded = {}, {}, {}
    for position, column in enumerate(raw):
        if not isinstance(column, dict):
            _fail('A column entry is not an object.', 'columns', capture_pass=pass_number, position=position, pages=pages)
        name, cid = column.get('name'), column.get('id')
        if not isinstance(name, str) or not name.strip() or name != name.strip() or not isinstance(cid, str) or not cid.strip():
            _fail('A column has no usable internal name or ID.', 'columns', capture_pass=pass_number, position=position, pages=pages)
        if name in by_name or cid in ids or name.casefold() in folded:
            _fail('Two columns share an internal name (or differ only by case) or an ID. The capture is ambiguous.', 'columns',
                  capture_pass=pass_number, position=position, duplicate_of=by_name.get(name, {}).get('id') or ids.get(cid) or folded.get(name.casefold()), pages=pages)
        if not readiness._is_plain(column):
            _fail('A column contains values that are not plain data.', 'columns', capture_pass=pass_number, position=position, pages=pages)
        by_name[name], ids[cid], folded[name.casefold()] = deepcopy(column), name, name
    return by_name, pages


def capture_schema(site_id, list_id, *, fetch, clock):
    """Read one list's identity and column definitions, twice, and return a candidate evidence artifact.

    ``fetch(url) -> (status, json, headers)`` is the caller's transport. It must make a single GET with no redirects
    and no retries. ``clock()`` is the caller's trusted host clock. Both are required; there are no defaults."""
    if not callable(fetch) or not callable(clock):
        _fail('A transport and a host clock must be supplied explicitly. There are no defaults.', 'setup')
    parts = site_id.split(',') if isinstance(site_id, str) else []
    if not (len(parts) == 3 and parts[0].endswith('.sharepoint.com') and all(re.fullmatch(readiness.GUID, p) for p in parts[1:])
            and isinstance(list_id, str) and re.fullmatch(readiness.GUID, list_id)):
        _fail('The site id must be host,site-guid,web-guid and the list id a GUID. Names and URLs are not identifiers.', 'setup')
    started_text, started = _now(clock, 'setup')
    base = '/sites/' + quote(site_id, safe='') + '/lists/' + quote(list_id, safe='')

    before, echoed_site = _list_metadata(fetch, base, site_id, list_id, 'list_identity')
    passes, requests = [], []
    for n in range(1, PASSES + 1):
        columns, pages = _columns(fetch, base, n)
        passes.append(columns)
        requests += [dict(capture_pass=n, kind='columns', request_hash=p['request_hash'], page_hash=p['page_hash'], status=p['status'],
                          count=p['count'], terminal=p['terminal']) for p in pages]
    after, _ = _list_metadata(fetch, base, site_id, list_id, 'list_identity_recheck')
    digests = [sf.digest({k: readiness.column_evidence(v) for k, v in p.items()}) for p in passes]
    if len(set(digests)) != 1:
        changed = sorted(k for k in set(passes[0]) | set(passes[-1]) if passes[0].get(k) != passes[-1].get(k))
        _fail('The column definitions changed while they were being captured. Nothing was accepted; capture again.', 'consistency',
              changed_columns=changed, pass_digests=digests)
    if before.get('displayName') != after.get('displayName'):
        _fail('The list was renamed while it was being captured. Nothing was accepted; capture again.', 'consistency')
    completed_text, completed = _now(clock, 'completion')
    if completed < started:
        _fail('The host clock went backwards during the capture.', 'completion')

    snapshot = dict(site_id=site_id, list_id=list_id, list=dict(id=list_id, displayName=before.get('displayName')),
                    columns=passes[0], captured_at=completed.isoformat())
    inventory = readiness.inventory(snapshot)
    review = [dict(column=c['name'], problems=c['problems']) for c in inventory['columns'] if c['problems']]
    by_type = {}
    for c in inventory['columns']:
        by_type.setdefault(c['type'], []).append(c['name'])
    return dict(
        kind=KIND, format=FORMAT, status='candidate_unreviewed', marked_verified=False, grants_authority=False,
        snapshot=snapshot, snapshot_identity=readiness.snapshot_identity(snapshot), schema_digest=readiness.schema_digest(snapshot),
        observed=dict(started_at=started.isoformat(), completed_at=completed.isoformat(), clock='host clock supplied by the caller',
                      capture_passes=PASSES, passes_agree=True, complete=True, column_count=len(passes[0]), requests=requests,
                      site_identity_echoed_by_source=echoed_site is not None,
                      source_text_not_used_as_time=dict(list_last_modified_before=before.get('lastModifiedDateTime'), list_last_modified_after=after.get('lastModifiedDateTime'),
                                                        list_etag_before=before.get('eTag'), list_etag_after=after.get('eTag')),
                      list_items_changed_during_capture=(before.get('eTag'), before.get('lastModifiedDateTime')) != (after.get('eTag'), after.get('lastModifiedDateTime'))),
        inventory=inventory,
        columns_needing_review=review,
        field_candidates=dict(
            note='Columns grouped by discovered type, in name order, for a person to review. No ranking, no suggestion. '
                 'A date-and-time column is not thereby the scan time; nothing here says what any column means.',
            by_discovered_type={k: sorted(v) for k, v in sorted(by_type.items())},
            scan_field='not_determined', mapping='not_determined'),
        samples=dict(status='not_performed', reason='No list items were read. Row-level checks are a separate, optional step.'),
        provenance=dict(collector='not_established_by_this_code', transport='supplied_by_caller_not_identified_by_this_code',
                        origin='unverified_candidate', signed=False),
        must_be_established=list(MUST_BE_ESTABLISHED))


def receipt_candidate(artifact, collector):
    """The content a capture authority would be asked to record for this snapshot. It is NOT a receipt and proves
    nothing: it is unsigned, and it only becomes a receipt when a real capture authority records it in its own store
    after its own checks. The hosted entry point reads receipts only from that authority."""
    if not isinstance(artifact, dict) or artifact.get('kind') != KIND or not isinstance(artifact.get('snapshot'), dict):
        raise ValueError('A candidate schema capture artifact is required.')
    if not isinstance(collector, str) or not collector.strip() or collector != collector.strip():
        raise ValueError('The collector identity must be stated by whoever submits this; it is not known to this code.')
    snapshot = artifact['snapshot']
    if readiness.snapshot_identity(snapshot) != artifact.get('snapshot_identity') or readiness.schema_digest(snapshot) != artifact.get('schema_digest'):
        raise ValueError('The artifact was altered after capture: its snapshot no longer matches its recorded hashes.')
    return dict(snapshot_identity=artifact['snapshot_identity'], site_id=snapshot['site_id'], list_id=snapshot['list_id'],
                schema_digest=artifact['schema_digest'], collector=collector, captured_at=snapshot['captured_at'],
                status='unsigned_candidate_not_a_receipt')

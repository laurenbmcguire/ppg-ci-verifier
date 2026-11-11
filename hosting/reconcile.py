"""Reconcile a report to the live SharePoint list, item by item. Read-only toward SharePoint.

Two steps, as the operations owner asked:
  1. the workbook's captured export (its Source Records sheet) against the underlying SharePoint list now
  2. the workbook's record rows against that same snapshot

A snapshot keeps its capture time, every item's version and modified time, and how complete it is: pages read,
access denied, throttling, a page that failed, columns the verifier does not understand. Anything short of full
coverage is "incomplete" and can never produce a verified result. Differences are listed row by row with a link to
the SharePoint item. Proposed corrections are kept apart from the workbook; nothing is applied automatically.

Business Date takes precedence over Date0/Created. Timestamp dates require an
exact verified source contract and matching column metadata; otherwise the day
remains unknown and verification stays held. Raw evidence is never rewritten.
"""
import json
import re
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

CHICAGO = ZoneInfo('America/Chicago')
PAGE_SIZE = 999
MAX_ITEMS = 200000
QTY = ('inspected', 'certified', 'rejected', 'reworked')
BUSINESS_DATE_FIELDS = ('Date', 'BusinessDate', 'ReportDate', 'InspectionDate')
SUBMISSION_FIELDS = ('Date0', 'Created')
VERIFICATION_GAPS = frozenset(('source_day_unconfirmed', 'source_item_unconfirmed'))


def now():
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------- dates
def business_day(value, date_only=None):
    """A SharePoint business-date value -> 'YYYY-MM-DD' in America/Chicago, or None.
    date_only: presentation metadata, not a UTC calendar-date contract. Timestamp values
    inconsistent with the existing local-midnight encoding are unavailable. The Chicago
    compatibility assumption still requires a verified source timezone before live release."""
    if value in (None, ''):
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        text = str(value).strip()
        if len(text) == 10 and text[4] == '-' and text[7] == '-':
            try:
                return datetime.strptime(text, '%Y-%m-%d').date().isoformat()
            except ValueError:
                return None
        try:
            dt = datetime.fromisoformat(text.replace('Z', '+00:00'))
        except ValueError:
            return None
    local = dt if dt.tzinfo is None else dt.astimezone(CHICAGO)
    # Graph dateOnly is presentation metadata, not a promise that UTC midnight
    # encodes a calendar date. Retain known local-midnight encodings only; an
    # inconsistent timestamp is unavailable evidence, never yesterday's date.
    if date_only is True and (local.hour or local.minute or local.second or local.microsecond):
        return None
    return local.date().isoformat()


def is_local_midnight(value):
    try:
        dt = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    except ValueError:
        return len(str(value)) == 10
    if dt.tzinfo is None:
        return dt.hour == dt.minute == dt.second == 0
    loc = dt.astimezone(CHICAGO)
    return loc.hour == loc.minute == loc.second == 0


def excel_serial_day(value):
    """An Excel serial date (1900 system) -> 'YYYY-MM-DD'. Serial 60 (the fictitious 1900-02-29) is refused."""
    try:
        n = float(value)
    except (TypeError, ValueError):
        return None
    if not 1 <= n < 2958466 or int(n) == 60:
        return None
    from datetime import date, timedelta
    base = date(1899, 12, 31) if n < 60 else date(1899, 12, 30)
    return (base + timedelta(days=int(n))).isoformat()


def item_day(fields, columns, schedule=None, *, site_id=None, list_id=None, date_contract=None):
    """Resolve calendar dates, or aware local-midnight timestamps under an exact contract."""
    from hosting.source_date_contracts import contract_matches
    contract = date_contract if contract_matches(date_contract, site_id, list_id, columns) else None
    for name in BUSINESS_DATE_FIELDS:
        if fields.get(name) not in (None, ''):
            value = fields[name]
            if isinstance(value, str) and re.fullmatch(r'\d{4}-\d{2}-\d{2}', value.strip()):
                return business_day(value.strip()), 'business_date'
            if contract and name == contract['field']:
                # datetime truncates sub-microsecond precision; never silently round into midnight.
                fraction = re.search(r'T\d{2}:\d{2}:\d{2}[.,](\d+)', str(value))
                if fraction and any(c != '0' for c in fraction.group(1)):
                    return None, 'timezone_unconfirmed'
                try:
                    dt = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace('Z', '+00:00'))
                    if dt.tzinfo is not None and dt.utcoffset() is not None:
                        local = dt.astimezone(ZoneInfo(contract['zone']))
                        if not (local.hour or local.minute or local.second or local.microsecond):
                            return local.date().isoformat(), 'business_date'
                except (ValueError, TypeError, OverflowError):
                    pass
            return None, 'timezone_unconfirmed'
    if any(fields.get(name) not in (None, '') for name in SUBMISSION_FIELDS):
        return None, 'submission_time_unconfirmed'
    return None, None


# ---------------------------------------------------------------- reading the list (read-only)
class Coverage(dict):
    def incomplete(self):
        return bool(self.get('denied') or self.get('throttled') or self.get('failed_pages') or self.get('truncated') or self.get('unknown_schema')
                    or self.get('undated') or self.get('date_fallbacks') or not self.get('complete'))


def _fetch(url, opener=None, retries=2):
    """(status, json) for one Graph GET. 429/503 are retried after Retry-After (at most 5 s), then reported."""
    from hosting import sharepoint as sp
    full = url if url.startswith('https://') else sp.GRAPH + url
    for attempt in range(retries + 1):
        req = urllib.request.Request(full, headers={'Authorization': 'Bearer ' + sp._token_value(opener)})
        try:
            with sp._open(req, opener) as resp:
                return 200, json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            if exc.code in (429, 503) and attempt < retries:
                try:
                    wait = min(5.0, float(exc.headers.get('Retry-After') or 1))
                except (TypeError, ValueError, AttributeError):
                    wait = 1.0
                time.sleep(wait)
                continue
            return exc.code, None
        except (urllib.error.URLError, TimeoutError, OSError):
            if attempt < retries:
                continue
            return 0, None
    return 0, None


def site_id_for(site_name, opener=None):
    from hosting import sharepoint as sp
    matches = {sid for name, sid, _ in sp.client_sites(opener) if name == site_name}
    if len(matches) > 1:
        raise sp.SharePointError('Ambiguous site name; exact site ID required.')
    return next(iter(matches), None)


def _pages(url, cov, opener=None, *, kind='items', cap=None):
    """Read all pages, preserving coverage failures rather than blessing partial data."""
    from hosting.sharepoint import pagination_collection, SharePointError
    seen, records = set(), []
    try:
        collection = pagination_collection(url)
    except SharePointError:
        cov['failed_pages'] += 1; cov['notes'].append(f'{kind}: invalid collection path.')
        cov[kind + '_terminal'] = False
        return records
    while url:
        if not isinstance(url, str) or url in seen:
            cov['failed_pages'] += 1; cov['notes'].append(f'{kind}: invalid or repeated cursor.'); break
        try:
            if pagination_collection(url) != collection:
                raise SharePointError('Different collection')
        except SharePointError:
            cov['failed_pages'] += 1; cov['notes'].append(f'{kind}: pagination changed or obscured the requested collection.'); break
        seen.add(url)
        status, page = _fetch(url, opener)
        if status in (401,403): cov['denied'] = True
        elif status in (429,503): cov['throttled'] = True
        if status != 200 or not isinstance(page,dict) or not isinstance(page.get('value'),list):
            cov['failed_pages'] += 1; cov['notes'].append(f'{kind}: page unavailable or malformed (HTTP {status}).'); break
        cov['pages' if kind == 'items' else 'column_pages'] += 1
        records.extend(page['value'])
        url = page.get('@odata.nextLink')
        if url is not None and (not isinstance(url,str) or not url):
            cov['failed_pages'] += 1; cov['notes'].append(f'{kind}: invalid next cursor.'); break
        if cap is not None and (len(records) > cap or (len(records) == cap and url)):
            cov['truncated'] = True; cov['notes'].append(f'{kind}: safety limit {cap} reached before complete capture.'); break
    cov[kind + '_terminal'] = url is None
    return records


def display_item_url(site_name, site_id, list_id, list_metadata, item_id):
    """Canonical item form from verified list metadata, never from a row URL."""
    from hosting.sheet_roles import source_url_identity
    from urllib.parse import quote, urlsplit
    if not isinstance(item_id, str) or not re.fullmatch(r'[0-9]+', item_id):
        return None
    if not isinstance(list_metadata, dict) or list_metadata.get('id') != list_id:
        return None
    url = list_metadata.get('webUrl')
    identity = source_url_identity(url, list_only=True)
    if not identity or identity.get('item') is not None or urlsplit(url).query:
        return None
    host = str(site_id or '').split(',')[0].lower()
    if (not host.endswith('.sharepoint.com') or identity['origin'] != 'https://' + host
            or not site_name or identity['site'].casefold() != str(site_name).casefold()):
        return None
    return (identity['origin'] + '/sites/' + quote(identity['site'], safe='') + '/Lists/' +
            quote(identity['list'], safe='') + '/DispForm.aspx?ID=' + item_id)


def snapshot(site_name, list_id, opener=None, schedule=None, *, site_id=None):
    """Every item of one SharePoint list, with coverage. Never writes to SharePoint."""
    from hosting.adaptive_reports import field_for_header, header_text
    cov = Coverage(pages=0, column_pages=0, items=0, complete=False, denied=False, throttled=False, failed_pages=0, truncated=False,
                   unknown_schema=False, undated=0, notes=[])
    snap = dict(site=site_name, list_id=list_id, captured_at=now(), coverage=cov, items=[], list=None)
    site_id = site_id or site_id_for(site_name, opener)
    snap['site_id'] = site_id
    if not site_id:
        cov['notes'].append(f'SharePoint site {site_name} was not found or is not readable.')
        cov['denied'] = True
        return snap
    status, lst = _fetch(f'/sites/{site_id}/lists/{list_id}?$select=id,displayName,webUrl,lastModifiedDateTime', opener)
    if status in (401, 403):
        cov['denied'] = True; cov['notes'].append('Access to the list was denied.'); return snap
    if status in (429, 503):
        cov['throttled'] = True; cov['notes'].append('SharePoint throttled the request.'); return snap
    if status != 200 or not lst:
        cov['notes'].append(f'The list {list_id} could not be read (HTTP {status or "no answer"}).'); cov['failed_pages'] += 1; return snap
    if lst.get('id') != list_id:
        cov['failed_pages'] += 1; cov['notes'].append('Returned list identity differs from assignment.'); return snap
    snap['list'] = dict(id=lst.get('id'), name=lst.get('displayName'), url=lst.get('webUrl'), modified=lst.get('lastModifiedDateTime'))
    cols = _pages(f'/sites/{site_id}/lists/{list_id}/columns?$top=500', cov, opener, kind='columns')
    columns = {c['name']: c for c in cols if isinstance(c,dict) and c.get('name')}
    snap['columns'] = columns
    from hosting.source_date_contracts import capture_contract
    snap['date_contract'] = capture_contract(site_id, list_id, columns, opener)
    if len(columns) != len(cols):
        cov['unknown_schema'] = True; cov['notes'].append('Missing or duplicate column identities.')
    # Lists often keep old, unused columns next to the ones the tablets fill (QtyInspected and Quantity, QtyRejected and
    # NOKQty). Every candidate is read, and the column that actually holds values is used for each quantity.
    candidates = {}
    for name, c in columns.items():
        if c.get('hidden'):
            continue
        f = field_for_header(c.get('displayName') or '') or field_for_header(header_text(name))
        if not f and _bare_quantity(c.get('displayName') or name):
            f = 'inspected'
        if f:
            candidates.setdefault(f, []).append(name)
    url = f'/sites/{site_id}/lists/{list_id}/items?$expand=fields&$top={PAGE_SIZE}'
    base = (lst.get('webUrl') or '').rstrip('/')
    raw_items = _pages(url, cov, opener, cap=MAX_ITEMS)
    seen_items = set()
    for it in raw_items:
        if not isinstance(it,dict) or not it.get('id') or str(it['id']) in seen_items:
            cov['failed_pages'] += 1; cov['notes'].append('Missing or repeated item identity across pages.'); continue
        seen_items.add(str(it['id']))
        f = it.get('fields') or {}
        if not isinstance(f,dict):
            cov['unknown_schema'] = True; cov['notes'].append('Malformed item fields.'); continue
        day, basis = item_day(f, columns, schedule, site_id=site_id, list_id=list_id, date_contract=snap['date_contract'])
        snap['items'].append(dict(
            site_id=site_id, list_id=list_id, item=str(it['id']),
            version=f.get('_UIVersionString') or (it.get('eTag') or '').split(',')[-1].strip('"') or None,
            modified=it.get('lastModifiedDateTime'), created=it.get('createdDateTime'),
            url=display_item_url(site_name, site_id, list_id, lst, str(it['id'])),
            day=day, day_basis=basis, identifier=f.get('Identifier') or f.get('Title'),
            raw=dict(f), original=it))
    cov['items'] = len(snap['items'])
    cov['complete'] = bool(cov.get('items_terminal') and cov.get('columns_terminal') and not
                          (cov['denied'] or cov['throttled'] or cov['failed_pages'] or cov['truncated'] or cov['unknown_schema']))
    snap['quantity_candidates'] = candidates
    undated = [it['item'] for it in snap['items'] if not it.get('day')]
    cov['undated'] = len(undated)
    cov['timezone_unconfirmed'] = sum(it.get('day_basis') == 'timezone_unconfirmed' for it in snap['items'])
    if cov['timezone_unconfirmed']:
        cov['notes'].append('Business-date timestamps require a verified timezone/date contract for this exact site and list. Preserve raw values and confirm source date settings; Chicago-like times are not confirmation.')
    cov['date_fallbacks'] = sum(str(it.get('day_basis') or '').startswith('submission_time') for it in snap['items'])
    if cov['date_fallbacks']:
        cov['notes'].append('Some items have only submission timestamps; business-date mapping needs confirmation.')
    if undated:
        cov['notes'].append(f'{len(undated)} SharePoint item(s) have an unconfirmed business day, so it is unknown which day they belong to '
                            f'(items {", ".join(undated[:10])}{"…" if len(undated) > 10 else ""}).')
    if 'inspected' not in candidates:
        cov['unknown_schema'] = True
        cov['notes'].append('No inspected-quantity column was recognized in this list.')
    return snap


def _bare_quantity(name):
    from hosting.adaptive_reports import header_text
    return re.sub(r'[^a-z]', '', str(header_text(name)).lower()) in ('quantity', 'qty')


def _num(v):
    if v in (None, '') or isinstance(v, bool):
        return None
    try:
        value = Decimal(str(v))
        return value if value.is_finite() else None
    except (InvalidOperation, ValueError):
        return None


def _show(v):
    return 'blank' if v is None else (str(int(v)) if isinstance(v, Decimal) and v == v.to_integral_value() else str(v))


# ---------------------------------------------------------------- comparing
def choose_columns(snap, day_items, confirmed=None):
    """Which SharePoint column holds each quantity. Never decided by which column is fuller in general:
    one candidate -> used; a person's confirmed mapping -> used; several candidates -> the only one filled for this
    day's items is used; several filled ones need confirmation, and ones that disagree are a conflict.
    Returns (columns {field: name}, mapping {field: dict(status, column, candidates, evidence)})."""
    cols, mapping = {}, {}
    for field, cands in (snap.get('quantity_candidates') or {}).items():
        pick = (confirmed or {}).get(field)
        if pick in cands:
            cols[field] = pick
            mapping[field] = dict(status='confirmed', column=pick, candidates=cands, evidence=f'{pick} was confirmed for this list.')
            continue
        if len(cands) == 1:
            cols[field] = cands[0]
            mapping[field] = dict(status='single', column=cands[0], candidates=cands, evidence=f'{cands[0]} is the only {field} column.')
            continue
        filled = [c for c in cands if any(it['raw'].get(c) not in (None, '') for it in day_items)]
        if len(filled) == 1:
            cols[field] = filled[0]
            others = ', '.join(c for c in cands if c != filled[0])
            # Exactly one candidate holds values on this day's records and every other one is empty on all of them:
            # an empty column cannot be the quantity, so nobody is asked to confirm it.
            mapping[field] = dict(status='single', column=filled[0], candidates=cands,
                                  evidence=f'{filled[0]} is the only {field} column with values for this day; {others} {"is" if len(cands) == 2 else "are"} empty.')
        elif len(filled) > 1:
            agree = all(len({_num(it['raw'].get(c)) for c in filled if it['raw'].get(c) not in (None, '')}) <= 1 for it in day_items)
            cols[field] = filled[0]
            mapping[field] = dict(status='needs_confirmation' if agree else 'conflict', column=filled[0], candidates=cands,
                                  evidence=f'{", ".join(filled)} are all filled for this day' + ('' if agree else ' and disagree') + '. Confirm which is the real one.')
        else:
            cols[field] = cands[0]
            mapping[field] = dict(status='empty', column=cands[0], candidates=cands, evidence=f'None of {", ".join(cands)} has a value for this day.')
    return cols, mapping


def _list_segment(url):
    from urllib.parse import unquote
    m = re.search(r'/Lists/([^/?#]+)', url or '')
    return unquote(m.group(1)) if m else None


def evidence_verified(rec):
    return bool(rec and rec.get('status') == 'verified' and rec.get('verified') is True and
                counts_verified(rec) and not Coverage(rec.get('coverage') or {}).incomplete() and
                not rec.get('rows') and not rec.get('rows_truncated') and not rec.get('not_verified_because') and
                {'inspected', 'rejected'} <= set(rec.get('fields_checked') or []))


def counts_verified(rec):
    """Do not trust a saved success label without complete count evidence."""
    if rec.get('date_contract') is not None:
        from hosting.source_date_contracts import receipt_contract_current
        if not receipt_contract_current(rec['date_contract']):
            return False
    c = rec.get('count_reconciliation') or {}
    counts = c.get('record_counts') or {}
    expected = rec.get('report_rows')
    if c.get('status') != 'verified' or not isinstance(expected, int) or isinstance(expected, bool) or expected <= 0:
        return False
    if any(counts.get(k) != expected for k in ('report_rows','source_rows','matched_unique_items')) or rec.get('sharepoint_items_for_day') != expected:
        return False
    if any(counts.get(k) != 0 for k in ('duplicate_report_rows','missing_source_items','unmatched_report_rows')):
        return False
    for pair in [(c.get('quantities') or {}).get(k) or {} for k in ('inspected','rejected')] + [c.get('rejected_records') or {}]:
        if pair.get('status') != 'matched':
            return False
        totals = []
        for side in ('report','source'):
            value = pair.get(side) or {}
            total = _num(value.get('total'))
            if value.get('known_rows') != expected or value.get('unknown_rows') != 0 or total is None or total < 0:
                return False
            totals.append(total)
        if totals[0] != totals[1]:
            return False
    return True


def _count_reconciliation(day, records, items, snap, rows, incomplete):
    """Evidence totals only: never infer quantities, discard duplicates, or edit rows."""
    def aggregate(values, rejected_records=False):
        parsed = [_num(v) for v in values]
        known = [v for v in parsed if v is not None]
        total = sum((int(v > 0) if rejected_records else v for v in known), Decimal(0))
        return dict(total=_show(total) if parsed and len(known) == len(parsed) else None,
                    known_rows=len(known), unknown_rows=len(parsed) - len(known))
    def pair(field, rejected_records=False):
        report = aggregate([r.get('values', {}).get(field) for r in records], rejected_records)
        source = aggregate([i.get('values', {}).get(field) for i in items], rejected_records)
        invalid = any((_num(v) is not None and _num(v) < 0) for v in [r.get('values', {}).get(field) for r in records] + [i.get('values', {}).get(field) for i in items])
        status = ('unavailable' if incomplete or invalid or report['unknown_rows'] or source['unknown_rows'] or not records or not items
                  else 'matched' if report['total'] == source['total'] else 'differs')
        return dict(report=report, source=source, status=status)
    quantities = {f: pair(f) for f in QTY}
    duplicates = [dict(item=r['item'], sheet=r['sheet'], row=r['row'],
                       evidence='The same exact source item is referenced by another report row; both rows remain unchanged.')
                  for r in rows if r['status'] == 'item_listed_twice']
    missing = [r for r in rows if r['status'] == 'missing_from_report']
    unmatched = [r for r in rows if r['status'] in ('not_on_sharepoint', 'not_on_sharepoint_for_day', 'wrong_list', 'report_row_without_item_id', 'source_day_unconfirmed', 'source_item_unconfirmed')]
    exclusions = [dict(item=i.get('item'), day=i.get('day'), day_basis=i.get('day_basis'),
                       reason='Outside the requested business date', url=i.get('url'))
                  for i in snap.get('items') or [] if i.get('day') and i['day'] != day and
                  i.get('day_basis') in ('business_date', 'business_date_with_time')]
    excluded_locations = {(r.get('sheet'), r.get('row')) for r in unmatched}
    matched = {str(r.get('source', {}).get('item')) for r in records if (r.get('sheet'), r.get('row')) not in excluded_locations} & {str(i['item']) for i in items}
    return dict(status='held' if incomplete or rows or any(quantities[f]['status'] != 'matched' for f in ('inspected','rejected')) else 'verified',
                record_counts=dict(report_rows=len(records), source_rows=len(items), matched_unique_items=len(matched),
                                   duplicate_report_rows=len(duplicates), missing_source_items=len(missing), unmatched_report_rows=len(unmatched)),
                quantities=quantities, rejected_records=pair('rejected', True), duplicates=duplicates,
                exclusions=exclusions, exclusions_applied=False,
                note='Exclusions describe source records on other confirmed business dates. No report rows were removed or changed.')


def compare(day, records, export_records, snap, confirmed=None, report=None):
    """Item-level comparison. Returns the reconciliation dict saved on the report (no file is changed).
    report: dict(file_hash, checker_version) the result is bound to; a later version makes it stale."""
    from copy import deepcopy
    snap = deepcopy(snap)  # computed values must not mutate the captured source evidence
    cov = snap.get('coverage') or {}
    items = [i for i in snap.get('items') or [] if i.get('day') == day]
    cols, mapping = choose_columns(snap, items, confirmed)
    for it in snap.get('items') or []:   # values through the chosen columns (raw kept for the evidence above)
        raw = it.get('raw') or {}
        it['values'] = {q: _num(raw.get(c)) for q, c in cols.items()}
    by_item = {i['item']: i for i in items}
    all_items = {i['item']: i for i in snap.get('items') or []}
    site, list_seg = snap.get('site'), _list_segment((snap.get('list') or {}).get('url'))
    rows, proposals = [], []

    def row(status, item=None, rec=None, field=None, report_value=None, sharepoint=None, note=None):
        sp_item = all_items.get(item) if item else None
        rows.append(dict(status=status, item=item, field=field, report=report_value, sharepoint=sharepoint, note=note,
                         sheet=rec['sheet'] if rec else None, row=rec['row'] if rec else None, url=(sp_item or {}).get('url')))

    def same_list(src):
        """Exact IDs, otherwise full origin/list URL + agreeing item identity."""
        if src.get('site_id') or src.get('list_id'):
            return bool(src.get('site_id') and src.get('list_id') and
                        src['site_id'] == snap.get('site_id') and src['list_id'] == snap.get('list_id'))
        from hosting.sheet_roles import source_url_identity
        linked = source_url_identity(src.get('url'))
        expected = source_url_identity((snap.get('list') or {}).get('url'), list_only=True)
        if not linked or not expected:
            return None
        return (linked['origin'] == expected['origin'] and linked['list_path'] == expected['list_path'] and
                str(linked['item']) == str(src.get('item')) and
                (not src.get('site') or src['site'].casefold() == linked['site'].casefold()) and
                (not src.get('list') or src['list'].casefold() == linked['list'].casefold()))

    # 1. captured export (inside the workbook) against the live list
    export_summary = None
    if export_records:
        changed = missing = 0
        for e in export_records:
            src = e.get('source') or {}
            if same_list(src) is None:
                missing += 1
                row('export_source_unconfirmed', src.get('item'), e, note='The captured export has no complete matching source URL or stable source IDs.')
                continue
            it = all_items.get(src.get('item')) if same_list(src) is True else None
            if not it:
                missing += 1
                row('export_item_not_on_sharepoint' if cov.get('complete') else 'source_item_unconfirmed', src.get('item'), e,
                    note='The captured export lists an item this SharePoint list does not have.' if cov.get('complete') else 'The incomplete source capture cannot establish whether this exported item exists. Retry the source check; keep the report row unchanged.')
                continue
            if src.get('version') and it.get('version') and str(src['version']) != str(it['version']):
                changed += 1
                row('changed_since_export', it['item'], e, field='version', report_value=src['version'], sharepoint=it['version'],
                    note='The SharePoint item was edited after the export was captured.')
            for f in QTY:
                if f in e['values'] and f in it['values'] and e['values'][f] != it['values'][f]:
                    changed += 1
                    row('export_value_differs', it['item'], e, f, _show(e['values'][f]), _show(it['values'][f]))
        export_summary = dict(rows=len(export_records), changed=changed, missing=missing)

    # 2. the record rows against the snapshot: identity is site + list + item
    seen, unidentified, unconfirmed = set(), 0, 0
    report_fields = sorted({f for r in records for f in QTY if r['values'].get(f) is not None})
    unsupported = [f for f in report_fields if f not in cols]
    for f in unsupported:
        row('field_not_in_source', None, None, f, note=f'The report has {f} values but this SharePoint list has no {f} column, so they could not be checked.')
    for r in records:
        src = r.get('source') or {}
        key = src.get('item')
        if not key:
            unidentified += 1
            row('report_row_without_item_id', None, r, note='This row has no SharePoint item ID, so it cannot be matched one to one.')
            continue
        where = same_list(src)
        if where is False:
            row('wrong_list', key, r, note=f'This row comes from {src.get("site")} / {src.get("list")}, not {site} / {list_seg or "this list"}.')
            continue
        if where is None:
            unconfirmed += 1
        ident = (snap.get('site_id') or site, snap.get('list_id') or list_seg, key)
        if ident in seen:
            row('item_listed_twice', key, r)
            continue
        seen.add(ident)
        it = by_item.get(key)
        if not it:
            other = all_items.get(key)
            basis = (other or {}).get('day_basis') or 'no business date'
            if not other and not cov.get('complete'):
                row('source_item_unconfirmed', key, r, note='The source capture is incomplete, so this item could not be confirmed. Retry the source check; keep the report row unchanged.')
            elif other and not other.get('day'):
                raw = other.get('raw') or {}
                name = next((n for n in BUSINESS_DATE_FIELDS if raw.get(n) not in (None, '')), None)
                evidence = (f'SharePoint {name} contains {raw[name]!s}; its business day could not be confirmed.'
                            if name else 'No usable business-date value was captured for this SharePoint item.')
                row('source_day_unconfirmed', key, r,
                    note=evidence + ' Confirm the source date settings before comparing days. Keep this report row unchanged.')
            else:
                row('not_on_sharepoint_for_day' if other else 'not_on_sharepoint', key, r,
                    note=(f'On SharePoint the item is dated {other["day"]} ({basis.replace("_", " ")}).' if other else 'No such item in the list.'))
            continue
        for f in QTY:
            if f not in cols:
                continue
            mine, theirs = r['values'].get(f), it['values'].get(f)
            if f in r['values'] and mine != theirs:
                row('value_differs', key, r, f, _show(mine), _show(theirs))
                if where is True and theirs is not None and mapping.get(f, {}).get('status') in ('single', 'confirmed'):   # only from a column known to be right
                    proposals.append(dict(identity_verified=True, item=key, sheet=r['sheet'], row=r['row'], column=r['columns'].get(f), field=f,
                                          before=_show(mine), after=_show(theirs), url=it.get('url'), applied=False))
    for key, it in by_item.items():
        if not any(k == key for _, _, k in seen):
            row('missing_from_report', key, None, note='This SharePoint item for the day is not in the report.')

    incomplete = Coverage(cov).incomplete() if cov else True
    doubtful = [f for f in report_fields if mapping.get(f, {}).get('status') not in ('single', 'confirmed') and f in cols]
    status = ('incomplete' if incomplete else 'discrepancies' if rows else
              'unverified' if (not records or unidentified or unconfirmed or doubtful) else 'verified')
    import hashlib, json as _json
    snap_id = hashlib.sha256(_json.dumps(dict(site_id=snap.get('site_id'), list_id=snap.get('list_id'), columns=snap.get('columns'), date_contract=snap.get('date_contract'), items=snap.get('items'), coverage=cov, confirmed_mapping=confirmed), sort_keys=True, default=str).encode()).hexdigest()[:16]
    why = []
    if cov.get('timezone_unconfirmed'):
        why.append('Source business-date timestamps have no verified site/list timezone contract. Confirm source date settings before comparing or excluding these records.')
    if unconfirmed:
        why.append(f'{unconfirmed} row(s) do not say which SharePoint site and list they came from.')
    for f in doubtful:
        why.append(mapping[f]['evidence'])
    count_reconciliation = _count_reconciliation(day, records, items, snap, rows, incomplete)
    if count_reconciliation['status'] != 'verified':
        if status == 'verified':
            status = 'unverified'
        for field in ('inspected', 'rejected'):
            if count_reconciliation['quantities'][field]['status'] != 'matched':
                why.append(f'{field.capitalize()} quantity comparison is {count_reconciliation["quantities"][field]["status"]}; explicit values for every report and source record are required.')
    mapping_binding = dict(site_id=snap.get('site_id'), list_id=snap.get('list_id'), mapping=dict(confirmed or {}), revision=None, legacy_requires_confirmation=False)
    from hosting.source_patterns import capture
    pattern_evidence = capture(records, snap, day, same_list, snap_id, dict(report or {}))
    return dict(identifier_pattern_evidence=pattern_evidence, mapping_binding=mapping_binding, count_reconciliation=count_reconciliation, status=status, day=day, captured_at=snap.get('captured_at'), list=snap.get('list'), site=snap.get('site'), site_id=snap.get('site_id'), list_id=snap.get('list_id'),
                snapshot_id=snap_id, date_contract=snap.get('date_contract'), bound_to=dict(report or {}),
                coverage={k: cov.get(k) for k in ('pages', 'items', 'complete', 'denied', 'throttled', 'failed_pages', 'truncated', 'unknown_schema', 'undated', 'date_fallbacks', 'timezone_unconfirmed', 'notes')},
                quantity_columns=cols, column_mapping=mapping, fields_checked=[f for f in report_fields if f in cols], fields_not_checked=unsupported,
                day_basis=sorted({i.get('day_basis') for i in items if i.get('day_basis')}),
                sharepoint_items_for_day=len(items), report_rows=len(records), unidentified_rows=unidentified, unconfirmed_rows=unconfirmed,
                not_verified_because=why, rows=rows[:2000], rows_truncated=len(rows) > 2000, export=export_summary,
                verification_gap_count=sum(r['status'] in VERIFICATION_GAPS for r in rows),
                difference_count=sum(r['status'] not in VERIFICATION_GAPS for r in rows),
                proposed_corrections=proposals[:2000], corrections_applied=False, verified=status == 'verified')


def presentation_row(row):
    """Read-only correction of the exact historical unknown-date wording bug."""
    if (row.get('status') == 'not_on_sharepoint_for_day' and
            row.get('note') == 'On SharePoint the item is dated nothing (timezone unconfirmed).'):
        return dict(row, status='source_day_unconfirmed',
                    note='This item exists in SharePoint, but its business day was unconfirmed in the recorded capture. Recheck SharePoint; keep the report row unchanged.')
    return row


def current(result):
    """The reconciliation that belongs to exactly this report version and checker, or None (stale or missing)."""
    rec = result.get('source_reconciliation') or None
    if not rec or rec.get('stale'):
        return None
    if rec.get('date_contract') is not None:
        from hosting.source_date_contracts import receipt_contract_current
        if not receipt_contract_current(rec['date_contract']):
            return None
    bound = rec.get('bound_to') or {}
    if not bound.get('file_hash') or bound.get('file_hash') != result.get('file_hash'):
        return None
    if not bound.get('checker_version') or bound.get('checker_version') != result.get('checker_version'):
        return None
    if not bound:   # made before results were bound to a report version
        return None
    # Saved evidence is historical, not a fresh parse. A complete receipt for the
    # same immutable workbook remains usable, but a partial authoritative row
    # scope (including an older AI layout) must not look like full verification.
    from hosting.row_scope import saved_detail_count
    try:
        expected = saved_detail_count(result)
    except ValueError:
        return None
    if expected is not None and (type(rec.get('report_rows')) is not int or rec['report_rows'] != expected):
        return None
    return rec

"""Reconcile a report to the live SharePoint list, item by item. Read-only toward SharePoint.

Two steps, as the operations owner asked:
  1. the workbook's captured export (its Source Records sheet) against the underlying SharePoint list now
  2. the workbook's record rows against that same snapshot

A snapshot keeps its capture time, every item's version and modified time, and how complete it is: pages read,
access denied, throttling, a page that failed, columns the verifier does not understand. Anything short of full
coverage is "incomplete" and can never produce a verified result. Differences are listed row by row with a link to
the SharePoint item. Proposed corrections are kept apart from the workbook; nothing is applied automatically.

Business day: the list's date-only "Date" column is the business date. SharePoint stores a date-only value as
midnight in the site's time zone expressed in UTC (06:00Z in winter, 05:00Z in summer for America/Chicago), so it
is converted to America/Chicago before the date is taken, never cut from the UTC text. Submission (Date0/Created)
and Modified times are only used when a list has no business date, and the result says so.
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


def now():
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------- dates
def business_day(value, date_only=None):
    """A SharePoint business-date value -> 'YYYY-MM-DD' in America/Chicago, or None.
    date_only: True when the column is defined as date-only. Without column metadata a value that is exactly
    local midnight is treated as date-only too."""
    if value in (None, ''):
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        text = str(value).strip()
        if len(text) == 10 and text[4] == '-' and text[7] == '-':
            return text   # already a plain date: never shifted
        try:
            dt = datetime.fromisoformat(text.replace('Z', '+00:00'))
        except ValueError:
            return None
    if dt.tzinfo is None:
        return dt.date().isoformat()
    return dt.astimezone(CHICAGO).date().isoformat()


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


def item_day(fields, columns, schedule=None):
    """(day, basis). basis is 'business_date' or 'submission_time' (with shift times when given)."""
    for name in BUSINESS_DATE_FIELDS:
        if fields.get(name) not in (None, ''):
            col = columns.get(name) or {}
            fmt = ((col.get('dateTime') or {}).get('format') or '').lower()
            if fmt == 'dateonly' or (not fmt and is_local_midnight(fields[name])):
                return business_day(fields[name], True), 'business_date'
            if fmt == 'datetime' or not fmt:   # the list's Date column, stored with a time: still the business date, read in Chicago
                return business_day(fields[name]), 'business_date_with_time'
    for name in SUBMISSION_FIELDS:
        stamp = fields.get(name)
        if stamp:
            if schedule:
                from hosting.shifts import local_time, assign
                local = local_time(stamp)
                if local is not None:
                    return assign(local, schedule)[1], 'submission_time_with_shift_times'
            return business_day(stamp), 'submission_time'
    return None, None


# ---------------------------------------------------------------- reading the list (read-only)
class Coverage(dict):
    def incomplete(self):
        return bool(self.get('denied') or self.get('throttled') or self.get('failed_pages') or self.get('truncated') or self.get('unknown_schema')
                    or self.get('undated') or not self.get('complete'))


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
    for name, site_id, _ in sp.client_sites(opener):
        if name == site_name:
            return site_id
    return None


def snapshot(site_name, list_id, opener=None, schedule=None):
    """Every item of one SharePoint list, with coverage. Never writes to SharePoint."""
    from hosting.adaptive_reports import field_for_header, header_text
    cov = Coverage(pages=0, items=0, complete=False, denied=False, throttled=False, failed_pages=0, truncated=False,
                   unknown_schema=False, undated=0, notes=[])
    snap = dict(site=site_name, list_id=list_id, captured_at=now(), coverage=cov, items=[], list=None)
    site_id = site_id_for(site_name, opener)
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
    snap['list'] = dict(id=lst.get('id'), name=lst.get('displayName'), url=lst.get('webUrl'), modified=lst.get('lastModifiedDateTime'))
    status, cols = _fetch(f'/sites/{site_id}/lists/{list_id}/columns?$select=name,displayName,hidden,readOnly,dateTime&$top=500', opener)
    columns = {c['name']: c for c in (cols or {}).get('value', [])} if status == 200 else {}
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
    while url:
        status, page = _fetch(url, opener)
        if status in (401, 403):
            cov['denied'] = True; cov['notes'].append(f'Access denied on page {cov["pages"] + 1}.'); break
        if status in (429, 503):
            cov['throttled'] = True; cov['notes'].append(f'Throttled on page {cov["pages"] + 1}; the snapshot stops there.'); break
        if status != 200 or page is None:
            cov['failed_pages'] += 1; cov['notes'].append(f'Page {cov["pages"] + 1} could not be read.'); break
        cov['pages'] += 1
        for it in page.get('value', []):
            f = it.get('fields') or {}
            day, basis = item_day(f, columns, schedule)
            snap['items'].append(dict(
                item=str(it.get('id')), version=f.get('_UIVersionString') or (it.get('eTag') or '').split(',')[-1].strip('"') or None,
                modified=it.get('lastModifiedDateTime'), created=it.get('createdDateTime'),
                url=it.get('webUrl') or (f'{base}/DispForm.aspx?ID={it.get("id")}' if base else None),
                day=day, day_basis=basis, identifier=f.get('Identifier') or f.get('Title'),
                raw={col: f.get(col) for cols in candidates.values() for col in cols}))
        cov['items'] = len(snap['items'])
        if len(snap['items']) >= MAX_ITEMS:
            cov['truncated'] = True; cov['notes'].append(f'Stopped at {MAX_ITEMS} items.'); break
        url = page.get('@odata.nextLink')
    if url is None and not (cov['denied'] or cov['throttled'] or cov['failed_pages'] or cov['truncated']):
        cov['complete'] = True
    snap['quantity_candidates'] = candidates
    undated = [it['item'] for it in snap['items'] if not it.get('day')]
    cov['undated'] = len(undated)
    if undated:
        cov['notes'].append(f'{len(undated)} SharePoint item(s) have no business date, so it is unknown which day they belong to '
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
        return Decimal(str(v))
    except (InvalidOperation, ValueError):
        return None


def _show(v):
    return 'blank' if v is None else (str(int(v)) if isinstance(v, Decimal) and v == v.to_integral_value() else str(v))


# ---------------------------------------------------------------- comparing
def choose_columns(snap, day_items, confirmed=None):
    """Which SharePoint column holds each quantity. Never decided by which column is fuller in general:
    one candidate -> used; a person's confirmed mapping -> used; several candidates -> the one filled for this day's
    items is shown as needing confirmation, and several filled ones that disagree are a conflict.
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
            mapping[field] = dict(status='needs_confirmation', column=filled[0], candidates=cands,
                                  evidence=f'{filled[0]} is filled for this day; {others} {"is" if len(cands) == 2 else "are"} empty. Confirm {filled[0]} is the {field} column.')
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


def compare(day, records, export_records, snap, confirmed=None, report=None):
    """Item-level comparison. Returns the reconciliation dict saved on the report (no file is changed).
    report: dict(file_hash, checker_version) the result is bound to; a later version makes it stale."""
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
        """True / False / None (the row does not say which site and list it came from)."""
        if not src.get('site') and not src.get('list'):
            return None
        return (src.get('site') or '').lower() == (site or '').lower() and (not list_seg or (src.get('list') or '').lower() == list_seg.lower())

    # 1. captured export (inside the workbook) against the live list
    export_summary = None
    if export_records:
        changed = missing = 0
        for e in export_records:
            src = e.get('source') or {}
            it = all_items.get(src.get('item')) if same_list(src) is not False else None
            if not it:
                missing += 1
                row('export_item_not_on_sharepoint', src.get('item'), e, note='The captured export lists an item this SharePoint list does not have.')
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
        ident = (src.get('site'), src.get('list'), key)
        if ident in seen:
            row('item_listed_twice', key, r)
            continue
        seen.add(ident)
        it = by_item.get(key)
        if not it:
            other = all_items.get(key)
            basis = (other or {}).get('day_basis') or 'no business date'
            row('not_on_sharepoint_for_day' if other else 'not_on_sharepoint', key, r,
                note=(f'On SharePoint the item is dated {other.get("day") or "nothing"} ({basis.replace("_", " ")}).' if other else 'No such item in the list.'))
            continue
        for f in QTY:
            if f not in cols:
                continue
            mine, theirs = r['values'].get(f), it['values'].get(f)
            if f in r['values'] and mine != theirs:
                row('value_differs', key, r, f, _show(mine), _show(theirs))
                if theirs is not None and mapping.get(f, {}).get('status') in ('single', 'confirmed'):   # only from a column known to be right
                    proposals.append(dict(item=key, sheet=r['sheet'], row=r['row'], column=r['columns'].get(f), field=f,
                                          before=_show(mine), after=_show(theirs), url=it.get('url'), applied=False))
    for key, it in by_item.items():
        if not any(k == key for _, _, k in seen):
            row('missing_from_report', key, None, note='This SharePoint item for the day is not in the report.')

    incomplete = Coverage(cov).incomplete() if cov else True
    doubtful = [f for f in report_fields if mapping.get(f, {}).get('status') not in ('single', 'confirmed') and f in cols]
    status = ('incomplete' if incomplete else 'discrepancies' if rows else
              'unverified' if (not records or unidentified or unconfirmed or doubtful) else 'verified')
    import hashlib, json as _json
    snap_id = hashlib.sha256(_json.dumps([(i['item'], i.get('version'), i.get('modified')) for i in snap.get('items') or []], sort_keys=True).encode()).hexdigest()[:16]
    why = []
    if unconfirmed:
        why.append(f'{unconfirmed} row(s) do not say which SharePoint site and list they came from.')
    for f in doubtful:
        why.append(mapping[f]['evidence'])
    return dict(status=status, day=day, captured_at=snap.get('captured_at'), list=snap.get('list'), site=snap.get('site'),
                snapshot_id=snap_id, bound_to=dict(report or {}),
                coverage={k: cov.get(k) for k in ('pages', 'items', 'complete', 'denied', 'throttled', 'failed_pages', 'truncated', 'unknown_schema', 'undated', 'notes')},
                quantity_columns=cols, column_mapping=mapping, fields_checked=[f for f in report_fields if f in cols], fields_not_checked=unsupported,
                day_basis=sorted({i.get('day_basis') for i in items if i.get('day_basis')}),
                sharepoint_items_for_day=len(items), report_rows=len(records), unidentified_rows=unidentified, unconfirmed_rows=unconfirmed,
                not_verified_because=why, rows=rows[:2000], rows_truncated=len(rows) > 2000, export=export_summary,
                proposed_corrections=proposals[:2000], corrections_applied=False, verified=status == 'verified')


def current(result):
    """The reconciliation that belongs to exactly this report version and checker, or None (stale or missing)."""
    rec = result.get('source_reconciliation') or None
    if not rec or rec.get('stale'):
        return None
    bound = rec.get('bound_to') or {}
    if bound.get('file_hash') and bound.get('file_hash') != result.get('file_hash'):
        return None
    if bound.get('checker_version') and bound.get('checker_version') != result.get('checker_version'):
        return None
    if not bound:   # made before results were bound to a report version
        return None
    return rec

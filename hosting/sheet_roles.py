"""Sheet roles and the one authoritative record set per workbook.

A daily report often shows the same records more than once: a per-part summary, the record detail,
a raw SharePoint export of the same list items, and a photo gallery. Adding them together counts
production two or three times (the November 6 SL Alabama 2214 report showed 12 rows / 1,200 units for
5 records / 400 units). Here every sheet gets a role first, then exactly one record-level dataset is
chosen and the other presentations are compared against it instead of being added.

Roles
  photos         picture galleries: never production
  summary        per-part or per-day totals: compared, never counted when record rows exist
  source_export  a raw SharePoint export (system columns such as @odata.etag, ContentType, _UIVersionString)
  records        record-level rows (one row per inspection record)

Record identity is the SharePoint (site, list, item) taken from a source record URL / item ID column when
the workbook has one. Separate records whose values happen to match are never merged or removed: they
stay separate whenever their item IDs differ. When the choice of dataset is uncertain the result says so
and the report stays on hold.
"""
import re
from collections import Counter, defaultdict
from decimal import Decimal

PHOTO_WORDS = ('photo', 'photos', 'picture', 'pictures', 'image', 'images', 'gallery')
SUMMARY_WORDS = ('summary', 'dashboard', 'overview', 'recap', 'totals')
SOURCE_SHEET_NAMES = ('source records', 'source record', 'source export', 'sharepoint export', 'sharepoint records', 'raw export', 'raw data export')
SYSTEM_COLUMNS = {'odata etag', 'odata type', 'odata id', 'contenttype', 'content type', 'content type id', 'ui version string',
                  'link title', 'linktitle', 'attachments', 'created', 'modified', 'author id', 'editor id', 'compliance asset id',
                  'file system object type', 'server redirected embed url'}
RECORD_SHEET_WORDS = ('detail', 'details', 'records', 'inspection', 'data', 'log', 'shift')
_URL_ITEM = re.compile(r'/sites/([^/]+)/Lists/([^/]+)/(\d+)_\.000', re.I)
_URL_DISPFORM = re.compile(r'/sites/([^/]+)/Lists/([^/]+)/DispForm\.aspx\?ID=(\d+)', re.I)


def _norm(value):
    return re.sub(r'\s+', ' ', re.sub(r'[^a-z0-9]+', ' ', str(value).lower())).strip()


def parse_source_url(url):
    """https://…/sites/<site>/Lists/<list>/<item>_.000 -> (site, list, item)."""
    if not isinstance(url, str):
        return None
    m = _URL_ITEM.search(url) or _URL_DISPFORM.search(url)
    if not m:
        return None
    from urllib.parse import unquote
    return unquote(m.group(1)), unquote(m.group(2)), m.group(3)


def _item_id(value):
    if value is None or isinstance(value, bool):
        return None
    text = str(value).strip()
    if text.endswith('.0'):
        text = text[:-2]
    return text if text.isdigit() else None


def _headers(rows, header_row):
    from hosting.adaptive_reports import header_text
    if not header_row or header_row > len(rows):
        return []
    return [(_norm(header_text(v)) if isinstance(v, str) else '') for v in rows[header_row - 1]]


def sheet_role(name, rows, header_row, has_detail):
    words = _norm(name).split()
    joined = ' '.join(words)
    if any(w in PHOTO_WORDS for w in words):
        return 'photos'
    if joined in SOURCE_SHEET_NAMES:
        return 'source_export'
    heads = set(_headers(rows, header_row))
    if len(heads & SYSTEM_COLUMNS) >= 2:
        return 'source_export'
    if any(w in SUMMARY_WORDS for w in words):
        return 'summary'
    return 'records' if has_detail else 'other'


def attach_identity(sheet_rows, sheets, records):
    """Give each record a SharePoint identity (site, list, item) when the sheet carries one. Never invents one."""
    for sheet in sheets:
        rows = sheet_rows.get(sheet['name'], [])
        for table in sheet['tables']:
            heads = _headers(rows, table['header_row'])
            url_col = next((i for i, h in enumerate(heads) if h in ('source record url', 'source url', 'record url', 'item url', 'sharepoint url',
                                                                     'source record', 'source link', 'sharepoint link', 'record link')), None)
            if url_col is None:   # any column whose values are SharePoint item links
                first = rows[table['header_row']:table['header_row'] + 5] if table['header_row'] else []
                url_col = next((i for i in range(len(heads)) if any(parse_source_url(r[i]) for r in first if i < len(r))), None)
            id_col = next((i for i, h in enumerate(heads) if h in ('source item id', 'sharepoint item id', 'list item id', 'item id', 'source id')), None)
            if id_col is None and sheet.get('role') == 'source_export':
                id_col = next((i for i, h in enumerate(heads) if h == 'id'), None)
            ver_col = next((i for i, h in enumerate(heads) if h in ('ui version string', 'version', 'item version')), None)
            mod_col = next((i for i, h in enumerate(heads) if h == 'modified'), None)
            table['identity_columns'] = dict(url=url_col, item=id_col, version=ver_col, modified=mod_col)
            for r in records:
                if r['sheet'] != sheet['name'] or r['table'] != table['index']:
                    continue
                row = rows[r['row'] - 1] if r['row'] - 1 < len(rows) else []
                site = lst = item = None
                if url_col is not None and url_col < len(row):
                    parsed = parse_source_url(row[url_col])
                    if parsed:
                        site, lst, item = parsed
                if id_col is not None and id_col < len(row):
                    item = _item_id(row[id_col]) or item
                r['source'] = dict(site=site, list=lst, item=item) if item else None
                if item and ver_col is not None and ver_col < len(row) and row[ver_col] not in (None, ''):
                    r['source']['version'] = str(row[ver_col])
                if item and mod_col is not None and mod_col < len(row) and row[mod_col] not in (None, ''):
                    r['source']['modified'] = str(row[mod_col])


def _key(r):
    s = r.get('source')
    return (s['site'], s['list'], s['item']) if s else None


def _item_key(r):
    s = r.get('source')
    return s['item'] if s else None


def select(sources, sheets, records, finding):
    """Assign roles, choose the authoritative record set and compare the other presentations with it.
    Records outside the authoritative set get table_kind 'summary', 'source_copy' or 'photo' so nothing
    downstream counts them. Returns the dataset_selection dict stored on the result."""
    sheet_rows = {title: list(rows) for title, rows in sources}
    by_sheet = defaultdict(list)
    for r in records:
        if r['table_kind'] == 'detail' and not r['is_total']:
            by_sheet[r['sheet']].append(r)
    export_like = defaultdict(list)   # an export without an Inspected column is still compared item by item
    for r in records:
        if r['table_kind'] == 'supporting' and not r['is_total']:
            export_like[r['sheet']].append(r)
    for sheet in sheets:
        header = sheet['tables'][0]['header_row'] if sheet['tables'] else None
        sheet['role'] = sheet_role(sheet['name'], sheet_rows.get(sheet['name'], []), header, bool(by_sheet.get(sheet['name'])))
    for sheet in sheets:   # a raw SharePoint export is a source copy, never a reject log or production
        if sheet['role'] != 'source_export' and not by_sheet.get(sheet['name']) and export_like.get(sheet['name']):
            heads = set(_headers(sheet_rows.get(sheet['name'], []), sheet['tables'][0]['header_row'] if sheet['tables'] else None))
            if len(heads & SYSTEM_COLUMNS) >= 2 or _norm(sheet['name']) in SOURCE_SHEET_NAMES:
                sheet['role'] = 'source_export'
    attach_identity(sheet_rows, sheets, records)
    for sheet in sheets:
        if sheet['role'] == 'source_export' and not by_sheet.get(sheet['name']) and export_like.get(sheet['name']):
            by_sheet[sheet['name']] = export_like[sheet['name']]
            for r in export_like[sheet['name']]:
                r['table_kind'] = 'detail'   # becomes 'source_copy' below unless it is the only record set
            for t in sheet['tables']:
                if t['kind'] == 'supporting':
                    t['kind'] = 'detail'
    roles = {s['name']: s['role'] for s in sheets}
    with_rows = [s['name'] for s in sheets if by_sheet.get(s['name'])]
    notes, ambiguous = [], False

    record_sheets = [n for n in with_rows if roles[n] == 'records']
    export_sheets = [n for n in with_rows if roles[n] == 'source_export']
    summary_sheets = [n for n in with_rows if roles[n] == 'summary']
    photo_sheets = [n for n in with_rows if roles[n] == 'photos']
    if record_sheets:
        candidates = record_sheets
    elif export_sheets:
        candidates = export_sheets
        notes.append('Only a raw SharePoint export holds record rows; it is used as the record set.')
    else:
        candidates = summary_sheets
        if summary_sheets:
            notes.append('No record-level sheet was found; the summary rows are the only quantities in this file.')

    # Several record-level sheets are added together only when they hold different records (shift sheets).
    # Sheets that repeat the same SharePoint items are duplicate presentations: one is kept.
    chosen = []
    for name in candidates:
        keys = {_key(r) for r in by_sheet[name]} - {None}
        overlap = next((c for c in chosen if keys & {_key(r) for r in by_sheet[c]}), None)
        if overlap is None:
            chosen.append(name)
            continue
        other = {_key(r) for r in by_sheet[overlap]} - {None}
        if keys == other:
            keep = _prefer(overlap, name, by_sheet)
            drop = name if keep == overlap else overlap
            chosen[chosen.index(overlap)] = keep
            notes.append(f'{drop} repeats the same {len(keys)} records as {keep}; it was compared, not added.')
            finding('duplicate_presentation', drop, message=f'{drop} shows the same {len(keys)} SharePoint records as {keep}, so only {keep} is counted.')
        else:
            ambiguous = True
            shared = len(keys & other)
            notes.append(f'{name} and {overlap} share {shared} records but not all of them; which sheet is authoritative is unclear.')
            finding('ambiguous_record_sheets', name,
                    message=f'{name} and {overlap} share {shared} SharePoint records but each has records the other lacks. Only {overlap} is counted; confirm which sheet is the record set.')
    authoritative = set(chosen)

    kind_for = {'summary': 'summary', 'source_export': 'source_copy', 'photos': 'photo', 'records': 'source_copy'}
    for r in records:
        if r['table_kind'] == 'detail' and r['sheet'] not in authoritative:
            r['table_kind'] = kind_for.get(roles.get(r['sheet']), 'source_copy')
    for sheet in sheets:
        sheet['authoritative'] = sheet['name'] in authoritative
        sheet['data_row_count'] = sum(1 for r in records if r['sheet'] == sheet['name'] and r['table_kind'] == 'detail' and not r['is_total'])
        for t in sheet['tables']:
            if t['kind'] == 'detail' and sheet['name'] not in authoritative:
                t['kind'] = kind_for.get(sheet['role'], 'source_copy')

    detail = [r for r in records if r['table_kind'] == 'detail' and not r['is_total']]
    # a SharePoint item listed twice in the record set is a real duplicate: shown, never silently dropped
    counts = Counter(_key(r) for r in detail if _key(r))
    for key, n in counts.items():
        if n > 1:
            rows_ = [r for r in detail if _key(r) == key]
            for r in rows_[1:]:
                finding('duplicate_source_record', r['sheet'], r['row'],
                        message=f'SharePoint item {key[2]} appears {n} times in the record rows ({", ".join(f"{x["sheet"]} row {x["row"]}" for x in rows_)}).')
    for name in export_sheets:
        if name not in authoritative:
            _compare_export(name, by_sheet[name], detail, finding, notes)
    for name in summary_sheets:
        if name not in authoritative:
            _compare_summary(name, by_sheet[name], detail, finding, notes)
    identified = sum(1 for r in detail if r.get('source'))
    if detail and identified and identified < len(detail):
        ambiguous = True
        finding('record_identity_incomplete', detail[0]['sheet'],
                message=f'{len(detail) - identified} of {len(detail)} record rows have no SharePoint item ID, so they cannot be matched to source records one by one.')
    lists = {(r['source']['site'], r['source']['list']) for r in detail if r.get('source') and r['source']['list']}
    return dict(
        roles=roles, authoritative=[n for n in with_rows if n in authoritative],
        compared=[n for n in with_rows if n not in authoritative and roles[n] != 'photos'],
        photo_sheets=[s['name'] for s in sheets if s['role'] == 'photos'],
        record_count=len(detail), identified_records=identified,
        source_lists=[dict(site=s, list=l) for s, l in sorted(lists, key=str)],
        status='ambiguous' if ambiguous else ('certain' if detail else 'no_records'),
        notes=notes)


def _prefer(a, b, by_sheet):
    """Between two sheets with the same records, keep the record-detail presentation."""
    def score(name):
        words = _norm(name).split()
        return (any(w in ('detail', 'details') for w in words), len(by_sheet[name][0]['labels']) if by_sheet[name] else 0)
    return a if score(a) >= score(b) else b


def _sum(rows, field):
    vals = [r['values'].get(field) for r in rows]
    known = [v for v in vals if v is not None]
    return (sum(known, Decimal(0)) if known else None), (len(known) == len(vals) and bool(vals))


def _compare_export(name, export_rows, detail, finding, notes):
    det = {_item_key(r): r for r in detail if _item_key(r)}
    exp = {_item_key(r): r for r in export_rows if _item_key(r)}
    if det and exp:
        missing = [k for k in exp if k not in det]
        extra = [k for k in det if k not in exp]
        for k in missing:
            r = exp[k]
            finding('source_record_not_in_report', r['sheet'], r['row'], message=f'SharePoint item {k} is on {name} but not in the record rows.')
        for k in extra:
            r = det[k]
            finding('report_record_not_in_source_sheet', r['sheet'], r['row'], message=f'SharePoint item {k} is in the record rows but not on {name}.')
        diffs = 0
        for k, e in exp.items():
            d = det.get(k)
            if not d:
                continue
            for f in ('inspected', 'certified', 'rejected', 'reworked'):
                if f in e['values'] and f in d['values'] and e['values'][f] != d['values'][f] and e['values'][f] is not None:
                    diffs += 1
                    finding('source_sheet_value_mismatch', d['sheet'], d['row'], d['columns'].get(f),
                            message=f'SharePoint item {k}: {f} is {d["values"][f] if d["values"][f] is not None else "blank"} here but {e["values"][f]} on {name}.')
        notes.append(f'{name}: {len(exp)} records compared with the record rows by item ID; {len(missing)} missing, {len(extra)} extra, {diffs} value differences.')
        return
    for f in ('inspected', 'certified'):
        a, a_known = _sum(export_rows, f)
        b, b_known = _sum(detail, f)
        if a is not None and b is not None and a_known and b_known and a != b:
            finding('source_sheet_total_mismatch', name, message=f'{name} adds up to {a} {f} but the record rows add up to {b}.')
    notes.append(f'{name}: compared by totals only (no item IDs to match records one by one).')


def _compare_summary(name, summary_rows, detail, finding, notes):
    parts = defaultdict(list)
    for r in detail:
        p = (r['labels'].get('part') or '').strip().upper()
        if p:
            parts[p].append(r)
    checked = 0
    for s in summary_rows:
        p = (s['labels'].get('part') or '').strip().upper()
        group = parts.get(p) if p else None
        if not group:
            if p:
                finding('summary_part_not_in_records', s['sheet'], s['row'], message=f'Part {p} is on {name} but in no record row.')
            continue
        for f, n in s['values'].items():
            if n is None:
                continue
            total, known = _sum(group, f)
            checked += 1
            if total is None:
                finding('summary_value_without_records', s['sheet'], s['row'], s['columns'].get(f),
                        message=f'{name} shows {n} {f} for part {p} but the record rows have no {f} values (blank, not zero).')
            elif known and total != n:
                finding('summary_part_mismatch', s['sheet'], s['row'], s['columns'].get(f),
                        message=f'{name} shows {n} {f} for part {p} but the {len(group)} record rows add up to {total}.')
    notes.append(f'{name}: {checked} per-part values compared with the record rows; never added to production.')

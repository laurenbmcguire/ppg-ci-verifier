"""Compare a report's rows with the SharePoint daily export for the same job and date.

The export is the source of truth for what was scanned. Rows are matched by Identifier
(the scan / lot / serial), falling back to part + quantities when a report has no identifier
column. Nothing is changed in either file.
"""
from collections import Counter, defaultdict
import csv
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import io
import re

from hosting.adaptive_reports import scan, near_miss, norm, shift_number

FIELDS = ('inspected', 'rejected', 'certified')
NAMES = {'inspected': 'Inspected', 'rejected': 'Rejected / NOK', 'certified': 'Certified / OK', 'part': 'Part number',
         'day': 'Date', 'shift': 'Shift'}
MAX_FINDINGS = 300


class SourceError(ValueError):
    pass


def parse_export(filename, raw):
    """Rows of a SharePoint export (CSV or XLSX) as detail records with identifier/part/quantities."""
    name = (filename or '').lower()
    if name.endswith('.csv') or not name.endswith('.xlsx'):
        try:
            text = raw.decode('utf-8-sig')
        except UnicodeDecodeError as exc:
            raise SourceError('The SharePoint export is not UTF-8 CSV.') from exc
        sources = [('SharePoint export', list(csv.reader(io.StringIO(text, newline=''))))]
        cached = None
    else:
        from hosting.spreadsheets import _xlsx_rows, _cached_rows
        sources = [(title, list(rows)) for title, rows in _xlsx_rows(raw, [])]
        cached = _cached_rows(raw)
    sheets, records, _ = scan(sources, cached)
    rows = [r for r in records if r['table_kind'] == 'detail' and not r['is_total']]
    if not rows:
        raise SourceError('No Qty Inspected / Qty OK / Qty NOK columns were found in the SharePoint export.')
    return rows


def _key(value):
    return (value or '').strip().upper()


def _fmt(value):
    if isinstance(value, Decimal):
        return str(value.normalize()) if value == value.to_integral_value() else str(value)
    return '' if value is None else str(value)


def compare(report_rows, source_rows, meta=None):
    meta = dict(meta or {})
    findings, counts = [], Counter()

    def finding(code, message, sheet=None, row=None, column=None, **extra):
        counts[code] += 1
        if len(findings) < MAX_FINDINGS:
            findings.append(dict(code=code, sheet=sheet or 'SharePoint comparison', row=row, column=column, message=message, **extra))

    def cell_of(rep, field):
        from hosting.fixes import col_letter
        if field in FIELDS and rep['columns'].get(field):
            return f'{col_letter(rep["columns"][field] - 1)}{rep["row"]}'
        letter = (rep.get('label_columns') or {}).get('identifier' if field == 'identifier' else field)
        return f'{letter}{rep["row"]}' if letter else None

    # Duplicates inside the SharePoint export itself.
    by_id = defaultdict(list)
    for s in source_rows:
        if s['labels'].get('identifier'):
            by_id[_key(s['labels']['identifier'])].append(s)
    for ident, rows in by_id.items():
        if len(rows) > 1:
            finding('sharepoint_duplicate_identifier', f'Identifier {ident} appears {len(rows)} times in the SharePoint export (rows {", ".join(str(r["row"]) for r in rows)}).')

    unmatched_source = {id(s): s for s in source_rows}
    matched = differences = 0

    def compare_pair(rep, src):
        nonlocal differences
        diffs = []
        for f in FIELDS:
            a, b = rep['values'].get(f), src['values'].get(f)
            if a is not None and b is not None and a != b:
                diffs.append((f, a, b))
        pa, pb = _key(rep['labels'].get('part')), _key(src['labels'].get('part'))
        if pa and pb and pa != pb:
            diffs.append(('part', rep['labels'].get('part'), src['labels'].get('part')))
        if rep.get('day') and src.get('day') and rep['day'] != src['day']:
            diffs.append(('day', rep['day'].isoformat(), src['day'].isoformat()))
        sa, sb = shift_number(rep['labels'].get('shift')) or shift_number(rep['sheet']), shift_number(src['labels'].get('shift'))
        if sa and sb and sa != sb:
            diffs.append(('shift', sa, sb))
        ident = rep['labels'].get('identifier') or rep['labels'].get('part') or f'row {rep["row"]}'
        for f, a, b in diffs:
            differences += 1
            finding('differs_from_sharepoint', f'{ident}: {NAMES[f]} is {_fmt(a)} in the report but {_fmt(b)} on SharePoint.',
                    rep['sheet'], rep['row'], field=f, report_value=_fmt(a), sharepoint_value=_fmt(b), cell=cell_of(rep, f))

    leftovers = []
    for rep in report_rows:
        ident = _key(rep['labels'].get('identifier'))
        if not ident:
            leftovers.append(rep)
            continue
        candidates = [s for s in by_id.get(ident, []) if id(s) in unmatched_source]
        if candidates:
            src = candidates[0]
            del unmatched_source[id(src)]
            matched += 1
            compare_pair(rep, src)
            continue
        if ident in by_id:
            finding('duplicate_identifier_vs_sharepoint', f'Identifier {ident} is listed more times in the report than on SharePoint.', rep['sheet'], rep['row'])
            continue
        twin = next((s for s in unmatched_source.values()
                     if s['labels'].get('identifier') and near_miss(ident, _key(s['labels']['identifier']))), None)
        suggestion = _key(twin['labels']['identifier']) if twin else None
        if twin:
            del unmatched_source[id(twin)]  # one finding for the typo, not also "missing from report"
            compare_pair(rep, twin)
            finding('identifier_typo_vs_sharepoint', f'Identifier {ident} is not on SharePoint; SharePoint has {suggestion}, which looks like the same scan with a typo.',
                    rep['sheet'], rep['row'], report_value=ident, sharepoint_value=suggestion, field='identifier', cell=cell_of(rep, 'identifier'))
        else:
            finding('not_on_sharepoint', f'Identifier {ident} is in the report but not in the SharePoint export for this job and date.', rep['sheet'], rep['row'])

    # Rows without an identifier column: match by part number and the quantities both sides have.
    def same(rep, src):
        if _key(rep['labels'].get('part')) != _key(src['labels'].get('part')):
            return False
        return all(rep['values'].get(f) == src['values'].get(f) for f in FIELDS
                   if rep['values'].get(f) is not None and src['values'].get(f) is not None)
    for rep in leftovers:
        src = next((x for x in unmatched_source.values() if same(rep, x)), None)
        if src:
            del unmatched_source[id(src)]
            matched += 1
        else:
            finding('unmatched_report_row', f'Report row (part {rep["labels"].get("part") or "?"}) has no matching row on SharePoint.', rep['sheet'], rep['row'])

    for s in unmatched_source.values():
        ident = s['labels'].get('identifier') or s['labels'].get('part') or f'export row {s["row"]}'
        finding('missing_from_report', f'{ident} ({", ".join(f"{NAMES[f]} {_fmt(s["values"].get(f))}" for f in FIELDS if s["values"].get(f) is not None)}) is on SharePoint but not in the report.',
                row=s['row'])

    def total(rows, f):
        vals = [r['values'].get(f) for r in rows]
        return sum((v for v in vals if v is not None), Decimal(0)) if vals and all(v is not None for v in vals) else None
    totals = {}
    for f in FIELDS:
        a, b = total(report_rows, f), total(source_rows, f)
        totals[f] = dict(report=_fmt(a) if a is not None else None, sharepoint=_fmt(b) if b is not None else None)
        if a is not None and b is not None and a != b:
            finding('total_differs_from_sharepoint', f'Total {NAMES[f]}: report {_fmt(a)}, SharePoint {_fmt(b)}.', field=f, report_value=_fmt(a), sharepoint_value=_fmt(b))

    status = 'match' if not counts else 'differences'
    return dict(meta, status=status, compared_at=datetime.now(timezone.utc).isoformat(),
                report_rows=len(report_rows), sharepoint_rows=len(source_rows), matched_rows=matched,
                value_differences=differences, missing_from_report=counts['missing_from_report'],
                not_on_sharepoint=counts['not_on_sharepoint'] + counts['identifier_typo_vs_sharepoint'] + counts['unmatched_report_row'],
                totals=totals, finding_counts=dict(counts), findings=findings)


JOB = re.compile(r'(?<![A-Z0-9])((?=[A-Z0-9-]*\d)[A-Z0-9]{2,}(?:-[A-Z0-9]{2,}){2,})(?![A-Z0-9])')
DATES = [(re.compile(r'(\d{4})-(\d{2})-(\d{2})'), lambda m: (int(m[1]), int(m[2]), int(m[3]))),
         (re.compile(r'(?<!\d)(\d{1,2})[./](\d{1,2})[./](\d{2,4})(?!\d)'), lambda m: (int(m[3]) + (2000 if len(m[3]) == 2 else 0), int(m[1]), int(m[2])))]


def identify(filename, result):
    """(job, day) for the SharePoint lookup, from the report's own identity, its rows, or its filename."""
    identity = result.get('report_identity') or {}
    job = identity.get('project_number')
    day = identity.get('report_date') or result.get('report_day')
    upper = (filename or '').upper()
    if not job:
        m = JOB.search(upper)
        job = m.group(1) if m else None
    if not day:
        for pattern, parts in DATES:
            m = pattern.search(filename or '')
            if m:
                try:
                    y, mo, d = parts(m)
                    day = datetime(y, mo, d).date().isoformat()
                    break
                except ValueError:
                    continue
    return job, day


def digest(raw):
    return hashlib.sha256(raw).hexdigest()

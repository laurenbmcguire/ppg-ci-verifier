"""Legacy EOS builder, hardened to preserve source identity, recorded shifts and blanks.

This is NOT the selected Briggs-template/photo report generator. Builds remain
provisional; missing source mappings are held, never inferred from inactivity.
"""
import io
import re
from datetime import date, datetime, timedelta

from hosting.adaptive_reports import field_for_header, label_for_header, header_text, norm

HEADERS = ['Date', 'Shift', 'Area', 'Part number', 'Identifier / lot', 'Inspected for', 'Quantity Inspected',
           'Condition', 'Reject reason', 'Reject Quantity', 'Quantity Certified', 'Comments']
FIRST_ROW = 16
BAD_WORDS = {'nok', 'ng', 'suspect', 'reject', 'rejected', 'fail', 'failed', 'bad', 'no good', 'defect', 'defective', 'hold'}
NEXUS_DEFAULT = [dict(name='Shift 1', start='06:00', end='14:30'), dict(name='Shift 2', start='14:30', end='22:30'),
                 dict(name='Shift 3', start='22:00', end='06:30')]
WAIT_MINUTES = 45   # compatibility argument only; inactivity never establishes completion


class BuildError(Exception):
    pass


def _num(value):
    if value in (None, ''):
        return None
    try:
        n = float(str(value).replace(',', '').strip())
    except ValueError:
        return None
    return int(n) if n == int(n) else n


def column_roles(names):
    """{internal column name: role} from the list's display names. Unknown columns (and inspector, time and
    picture columns) get no role, so they can never reach the client file."""
    roles = {}
    for key, display in names.items():
        text = display or header_text(key)
        k = norm(header_text(text))
        if k in ('inspected for', 'inspect for', 'inspection for', 'inspection type', 'sort for', 'checked for'):
            roles[key] = 'inspected_for'; continue
        f = field_for_header(text)
        if f:
            roles.setdefault(key, f); continue
        lab = label_for_header(text)
        if lab and lab not in ('auditor', 'date', 'expected'):
            roles[key] = lab
    seen, out = set(), {}
    for key, role in roles.items():          # first column wins for each role
        if role in seen:
            raise BuildError(f'Multiple source columns map to {role}; confirm a field mapping before building.')
        seen.add(role); out[key] = role
    return out


def _row(fields, roles):
    r = {}
    for key, role in roles.items():
        v = fields.get(key)
        if isinstance(v, (dict, list)):
            v = v.get('Value') or v.get('LookupValue') if isinstance(v, dict) else ', '.join(map(str, v))
        r[role] = v
    cond = str(r.get('condition') or '').strip()
    inspected = _num(r.get('inspected'))
    rejected = _num(r.get('rejected'))
    certified = _num(r.get('certified'))
    return dict(identifier=_s(r.get('identifier')), part=_s(r.get('part')), area=_s(r.get('area')), inspected_for=_s(r.get('inspected_for')),
                defect=_s(r.get('defect')), comment=_s(r.get('comment')), condition=cond.upper() if cond.lower() in ('ok', 'nok', 'ng') else cond,
                inspected=inspected, rejected=rejected, certified=certified)


def _s(v):
    return '' if v is None else ' '.join(str(v).split())


def day_rows(job, day, schedule, opener=None, *, site_id=None, list_id=None):
    """(rows, info) for one report day: each row has the client fields plus internal 'time' and 'shift'."""
    from hosting import sharepoint
    from hosting.shifts import local_time, assign, number, _minutes
    if not sharepoint.configured():
        raise BuildError('SharePoint is not connected, so there are no scans to build from.')
    if not site_id or not list_id:
        raise BuildError('Build requires the verified site_id and list_id; job number alone is not sufficient.')
    try:
        data = sharepoint._list_items(job, opener, site_id=site_id, list_id=list_id)
    except (sharepoint.SharePointError, sharepoint.SharePointNotFound) as exc:
        raise BuildError(str(exc)) from None
    if not data:
        raise BuildError(f'No SharePoint data list was found for {job}.')
    site, lst, names, items = data
    roles = column_roles(names)
    if 'identifier' not in roles.values() and 'part' not in roles.values():
        raise BuildError(f'The SharePoint list "{lst.get("displayName")}" has no identifier or part number column the builder recognises.')
    rows, outside = [], 0
    shift_key = next((k for k,v in names.items() if re.sub(r'[^a-z]', '', str(v).lower()) == 'shift'), 'Shift')
    shift_map = {str(number(sh['name'])):sh for sh in schedule}
    columns = {c['name']:c for c in lst.get('_capture',{}).get('columns',[])}
    for it in items:
        business_day = sharepoint._item_day(it, schedule, columns)
        if business_day is None:
            raise BuildError('Source has unconfirmed business dates; no submission-time assignment was invented.')
        if business_day != day:
            continue
        fields = it.get('fields') or {}
        picked = fields.get(shift_key)
        if isinstance(picked,dict):picked = picked.get('Value')
        picked_key = str(number(str(picked))) if picked not in (None,'') else ''
        shift = shift_map.get(picked_key)
        if not shift:
            raise BuildError(f'Item {it.get("id")} has an unrecognized recorded Shift; no time-based substitute was applied.')
        local = local_time(sharepoint._stamp(it))
        row = _row(fields, roles)
        row.update(time=local, shift=number(shift['name']), shift_name=shift['name'],
                   source=dict(site_id=site_id,list_id=list_id,item=str(it['id'])), raw=dict(fields))
        rows.append(row)  # preserve every selected source record, including incomplete ones
    rows.sort(key=lambda r: (r['time'] or datetime.min))
    last = max((r['time'] for r in rows if r['time']), default=None)
    return rows, dict(site=site, site_id=site_id, list_id=list_id, capture=lst.get('_capture'), list=lst.get('displayName'), list_url=lst.get('webUrl'), scans=len(rows), outside=outside,
                      last_scan=last.isoformat() if last else None, columns={v: names.get(k, k) for k, v in roles.items()})


def readiness(last_scan_iso, now=None, wait=WAIT_MINUTES):
    """A scan timestamp never proves completion, regardless of inactivity duration."""
    return dict(state='provisional' if last_scan_iso else 'no_scans', ready_at=None,
                shift_end_confirmed=False,
                note='Through capture only. A quiet interval does not establish shift completion.')


def file_name(job, day):
    d = date.fromisoformat(day)
    return f'{job} - EOS - {d.month:02d}.{d.day:02d}.{d.year % 100:02d}.xlsx'


def build_xlsx(job, day, rows, schedule, header=None):
    """xlsx bytes in the new-format EOS layout."""
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter
    from hosting.shifts import number
    from hosting.recalc import recalc
    header = header or {}
    teal, pale, line = PatternFill('solid', fgColor='1F4E4A'), PatternFill('solid', fgColor='E8F0EE'), Side(style='thin', color='C9D6D2')
    white, bold = Font(bold=True, color='FFFFFF'), Font(bold=True)
    wb = Workbook()
    sm = wb.active
    sm.title = 'Summary'
    sm['B1'] = 'PPG QUALITY   /   END OF SHIFT DATA AND BILLING REPORT'
    sm['B1'].font = Font(bold=True, size=14, color='1F4E4A')
    info = [('Project', job), ('Report date', date.fromisoformat(day)), ('Customer', header.get('customer', '')),
            ('Worksite', header.get('worksite', '')), ('Part / program', header.get('program', '')), ('Prepared by', header.get('prepared_by', 'PPG Quality'))]
    for i, (k, v) in enumerate(info, 3):
        sm.cell(i, 2, k).font = bold
        c = sm.cell(i, 4, v)
        if isinstance(v, date):
            c.number_format = 'mm/dd/yyyy'
    for col, h in ((2, 'Shift'), (7, 'Inspected'), (9, 'Certified'), (11, 'Rejected'), (13, 'Reject rate')):
        c = sm.cell(20, col, h); c.font = white; c.fill = teal
    tabs = []
    for sh in schedule:
        n = number(sh['name'])
        mine = [r for r in rows if r['shift'] == n]
        if not mine:
            continue
        tabs.append((f'{sh["name"]} Shift' if not sh['name'].lower().endswith('shift') else sh['name'], sh, mine))
    for i, (title, sh, mine) in enumerate(tabs):
        ws = wb.create_sheet(title[:31])
        ws['A1'] = 'PPG QUALITY / DAILY INSPECTION REPORT'; ws['A1'].font = Font(bold=True, size=13, color='1F4E4A')
        ws['A3'] = title; ws['A3'].font = Font(bold=True, size=12)
        ws['A4'] = f'{job}   {date.fromisoformat(day).strftime("%m/%d/%Y")}'
        for col, h in enumerate(HEADERS, 1):
            c = ws.cell(15, col, h); c.font = white; c.fill = teal; c.alignment = Alignment(wrap_text=True, vertical='center')
        first, last = FIRST_ROW, FIRST_ROW + max(len(mine), 1) - 1
        for r, row in enumerate(mine, first):
            vals = [date.fromisoformat(day), number(sh['name']), row['area'], row['part'], row['identifier'], row['inspected_for'],
                    row['inspected'], row['condition'], row['defect'], row['rejected'], row['certified'], row['comment']]
            for col, v in enumerate(vals, 1):
                c = ws.cell(r, col, v)
                c.border = Border(bottom=line)
                if col == 1:
                    c.number_format = 'mm/dd/yyyy'
                if col == 5:
                    c.number_format = '@'        # identifiers stay text: no scientific notation, no lost zeros
                if col == 4:
                    c.number_format = '@'
            if (r - first) % 2:
                for col in range(1, 13):
                    ws.cell(r, col).fill = pale
        trow = last + 3
        for col, label, f in ((3, 'Total Parts Inspected', 'G'), (5, 'Total Certified', 'K'), (7, 'Total Rejected', 'J')):
            ws.cell(trow, col, label).font = bold
            ws.cell(trow + 1, col, f'=IF(COUNT({f}{first}:{f}{last})={len(mine)},SUM({f}{first}:{f}{last}),"")').font = Font(bold=True, size=12)
        widths = [12, 7, 12, 16, 22, 16, 11, 11, 22, 10, 11, 30]
        for col, w in enumerate(widths, 1):
            ws.column_dimensions[get_column_letter(col)].width = w
        ws.freeze_panes = 'A16'
        sr = 21 + i
        sm.cell(sr, 2, title)
        sm.cell(sr, 7, f"='{ws.title}'!C{trow + 1}"); sm.cell(sr, 9, f"='{ws.title}'!E{trow + 1}"); sm.cell(sr, 11, f"='{ws.title}'!G{trow + 1}")
        sm.cell(sr, 13, f'=IF(AND(COUNT(G{sr},K{sr})=2,G{sr}>0),K{sr}/G{sr},"")').number_format = '0.0%'
    tr = 21 + len(tabs)
    sm.cell(tr, 2, 'Total').font = bold
    for col in ('G', 'I', 'K'):
        c = sm[f'{col}{tr}']; c.value = f'=IF(COUNT({col}21:{col}{tr - 1})={len(tabs)},SUM({col}21:{col}{tr - 1}),"")'; c.font = bold
    sm[f'M{tr}'] = f'=IF(AND(COUNT(G{tr},K{tr})=2,G{tr}>0),K{tr}/G{tr},"")'; sm[f'M{tr}'].number_format = '0.0%'; sm[f'M{tr}'].font = bold
    for col, w in (('A', 3), ('B', 18), ('D', 30), ('G', 12), ('I', 12), ('K', 12), ('M', 12)):
        sm.column_dimensions[col].width = w
    rj = wb.create_sheet('Rejects')
    rj['A1'] = 'Rejects'; rj['A1'].font = Font(bold=True, size=13, color='1F4E4A')
    for col, h in enumerate(['Shift', 'Part number', 'Identifier / lot', 'Reject reason', 'Reject Quantity', 'Comments'], 1):
        c = rj.cell(3, col, h); c.font = white; c.fill = teal
    k = 4
    for title, sh, mine in tabs:
        for row in mine:
            if row['rejected']:
                for col, v in enumerate([number(sh['name']), row['part'], row['identifier'], row['defect'], row['rejected'], row['comment']], 1):
                    rj.cell(k, col, v)
                k += 1
    for col, w in zip('ABCDEF', (7, 16, 22, 24, 12, 30)):
        rj.column_dimensions[col].width = w
    buf = io.BytesIO()
    wb.save(buf)
    return recalc(buf.getvalue())


def nexus_split(rows, windows=None):
    """Totals per Nexus shift by plant-time window (first matching window wins where windows overlap)."""
    from hosting.shifts import _minutes
    windows = windows or NEXUS_DEFAULT
    out = [dict(name=w['name'], start=w['start'], end=w['end'], scans=0, inspected=0, rejected=0, certified=0, missing={k:0 for k in ('inspected','rejected','certified')}) for w in windows]
    unplaced = 0
    for r in rows:
        if not r.get('time'):
            unplaced += 1; continue
        m = r['time'].hour * 60 + r['time'].minute
        for w, o in zip(windows, out):
            a, b = _minutes(w['start']), _minutes(w['end'])
            if (a <= m < b) if a < b else (m >= a or m < b):
                o['scans'] += 1
                for k in ('inspected', 'rejected', 'certified'):
                    if r.get(k) is None:
                        o['missing'][k] += 1
                    else:
                        o[k] += r[k]
                break
        else:
            unplaced += 1
    for o in out:
        for k in ('inspected','rejected','certified'):
            if o['missing'][k]:o[k]=None
    # Overall source totals include every selected record, even if its time cannot
    # place it into a Nexus window. Window coverage is reported separately.
    total = dict(scans=len(rows))
    total.update({k: None if any(r.get(k) is None for r in rows) else sum(r[k] for r in rows) for k in ('inspected', 'rejected', 'certified')})
    return dict(shifts=out, total=total, unplaced=unplaced,
                coverage=dict(complete=unplaced == 0, source_records=len(rows), placed_records=len(rows) - unplaced),
                status='held' if unplaced or any(total[k] is None for k in ('inspected','rejected','certified')) else 'compared')

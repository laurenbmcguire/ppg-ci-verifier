"""Build a job's End-of-Shift report from its SharePoint scans, and split the day into Nexus shifts.

The report is client-safe by construction: only part, identifier, quantities, condition, reject reason, area and
comments are copied. Scan times, inspector names, picked shifts and internal check results never go in the file.
Each scan goes on the shift its scan time falls in (midnight rule: a 3rd-shift scan after midnight belongs to the
day the shift started). The file uses the new-format EOS layout: a Summary tab, one tab per shift with total
formulas (saved results filled in, so Excel, the verifier and email previews all show the right numbers).
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
WAIT_MINUTES = 45   # a report is ready this long after the last scan, so breaks and lunches are not taken as the shift ending


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
        if role not in seen:
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
    if inspected is None:
        inspected = 1 if r.get('identifier') else None      # a scan list: one row per scanned part or vehicle
    if rejected is None:
        rejected = (1 if inspected == 1 else 0) if cond.lower() in BAD_WORDS else 0 if inspected is not None else None
    certified = _num(r.get('certified'))
    if certified is None and inspected is not None and rejected is not None:
        certified = inspected - rejected
    if not cond and inspected is not None:
        cond = 'NOK' if rejected else 'OK'
    return dict(identifier=_s(r.get('identifier')), part=_s(r.get('part')), area=_s(r.get('area')), inspected_for=_s(r.get('inspected_for')),
                defect=_s(r.get('defect')) if rejected else '', comment=_s(r.get('comment')), condition=cond.upper() if cond.lower() in ('ok', 'nok', 'ng') else cond,
                inspected=inspected, rejected=rejected, certified=certified)


def _s(v):
    return '' if v is None else ' '.join(str(v).split())


def day_rows(job, day, schedule, opener=None):
    """(rows, info) for one report day: each row has the client fields plus internal 'time' and 'shift'."""
    from hosting import sharepoint
    from hosting.shifts import local_time, assign, number, _minutes
    if not sharepoint.configured():
        raise BuildError('SharePoint is not connected, so there are no scans to build from.')
    data = sharepoint._list_items(job, opener)
    if not data:
        raise BuildError(f'No SharePoint data list was found for {job}.')
    site, lst, names, items = data
    roles = column_roles(names)
    if 'identifier' not in roles.values() and 'part' not in roles.values():
        raise BuildError(f'The SharePoint list "{lst.get("displayName")}" has no identifier or part number column the builder recognises.')
    rows, outside = [], 0
    for it in items:
        if sharepoint._item_day(it, schedule) != day:
            continue
        local = local_time(sharepoint._stamp(it))
        shift = assign(local, schedule)[0] if local else None
        if local and not shift:            # outside every shift: nearest shift by time, and say so
            outside += 1
            m = local.hour * 60 + local.minute
            shift = min(schedule, key=lambda s: min((m - _minutes(s['start'])) % 1440, (_minutes(s['end']) - m) % 1440))
        row = _row(it.get('fields') or {}, roles)
        if not row['identifier'] and not row['part'] and row['inspected'] in (None, 0):
            continue
        row.update(time=local, shift=number(shift['name']) if shift else '1', shift_name=shift['name'] if shift else schedule[0]['name'])
        rows.append(row)
    rows.sort(key=lambda r: (r['time'] or datetime.min))
    last = max((r['time'] for r in rows if r['time']), default=None)
    return rows, dict(site=site, list=lst.get('displayName'), list_url=lst.get('webUrl'), scans=len(rows), outside=outside,
                      last_scan=last.isoformat() if last else None, columns={v: names.get(k, k) for k, v in roles.items()})


def readiness(last_scan_iso, now=None, wait=WAIT_MINUTES):
    """'running' until WAIT_MINUTES after the last scan, then 'ready'. Times are plant time."""
    from zoneinfo import ZoneInfo
    if not last_scan_iso:
        return dict(state='no_scans', ready_at=None)
    last = datetime.fromisoformat(last_scan_iso)
    now = now or datetime.now(ZoneInfo('America/Chicago')).replace(tzinfo=None)
    if last.tzinfo is not None:
        last = last.astimezone(ZoneInfo('America/Chicago')).replace(tzinfo=None)
    ready_at = last + timedelta(minutes=wait)
    return dict(state='ready' if now >= ready_at else 'running', ready_at=ready_at.isoformat(timespec='minutes'),
                minutes_left=max(0, int((ready_at - now).total_seconds() // 60)))


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
                    row['inspected'], row['condition'], row['defect'], row['rejected'], f'=G{r}-J{r}', row['comment']]
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
            ws.cell(trow + 1, col, f'=SUM({f}{first}:{f}{last})').font = Font(bold=True, size=12)
        widths = [12, 7, 12, 16, 22, 16, 11, 11, 22, 10, 11, 30]
        for col, w in enumerate(widths, 1):
            ws.column_dimensions[get_column_letter(col)].width = w
        ws.freeze_panes = 'A16'
        sr = 21 + i
        sm.cell(sr, 2, title)
        sm.cell(sr, 7, f"='{ws.title}'!C{trow + 1}"); sm.cell(sr, 9, f"='{ws.title}'!E{trow + 1}"); sm.cell(sr, 11, f"='{ws.title}'!G{trow + 1}")
        sm.cell(sr, 13, f'=IF(G{sr}=0,0,K{sr}/G{sr})').number_format = '0.0%'
    tr = 21 + len(tabs)
    sm.cell(tr, 2, 'Total').font = bold
    for col in ('G', 'I', 'K'):
        c = sm[f'{col}{tr}']; c.value = f'=SUM({col}21:{col}{tr - 1})'; c.font = bold
    sm[f'M{tr}'] = f'=IF(G{tr}=0,0,K{tr}/G{tr})'; sm[f'M{tr}'].number_format = '0.0%'; sm[f'M{tr}'].font = bold
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
    out = [dict(name=w['name'], start=w['start'], end=w['end'], scans=0, inspected=0, rejected=0, certified=0) for w in windows]
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
                    o[k] += r.get(k) or 0
                break
        else:
            unplaced += 1
    total = {k: sum(o[k] for o in out) for k in ('scans', 'inspected', 'rejected', 'certified')}
    return dict(shifts=out, total=total, unplaced=unplaced)

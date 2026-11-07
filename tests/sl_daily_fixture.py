"""A faithful synthetic copy of the current daily template (the November 6 SL Alabama 2214 layout).

Sheets: Daily Inspection Summary (per-part totals as formulas with saved results), Inspection Detail
(one row per SharePoint record), Defect Photos (embedded pictures) and, optionally, Source Records
(the raw SharePoint export of the same records). All three data sheets describe the same 5 records /
400 inspected units; adding them together gives 12 rows / 1,200 units.
"""
import io
from datetime import datetime

from openpyxl import Workbook
from openpyxl.drawing.image import Image as XLImage

from tests.eos_fixture import add_cached_values

SITE = 'SLAlabama_AlexanderCity'
LIST = 'Data 2214SLALSLAL'
PNG = (b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89'
       b'\x00\x00\x00\rIDATx\x9cc\xf8\xcf\xc0\xf0\x1f\x00\x05\x00\x01\xff\x89\x99=\x1d\x00\x00\x00\x00IEND\xaeB`\x82')


def records(n=5, inspected=80, same_values=False):
    rows = []
    for i in range(n):
        item = 432 + i
        rows.append(dict(item=item, ident='LOT-A' if same_values else f'LOT-{i + 1:02d}',
                         part='80139189' if (same_values or i < 3) else '80139190',
                         part_name='Bracket LH' if (same_values or i < 3) else 'Bracket RH',
                         inspected=inspected, ok=inspected, shift='Shift 1', area='Line 2', auditor='Auditor 7'))
    return rows


def sl_daily(recs=None, source_records=True, photos=True, job='2214', day='2026-11-06', summary_inspected=None,
             nok_blank=True, source_overrides=None):
    recs = recs or records()
    wb = Workbook()
    s = wb.active
    s.title = 'Daily Inspection Summary'
    s['A1'] = 'PPG QUALITY / DAILY INSPECTION SUMMARY'
    s['A3'] = f'SL Alabama · Alexander City · Job {job}'
    s['A4'] = 'November 6, 2026'
    s['A6'] = '="Captured "&TEXT(NOW(),"yyyy-mm-dd")'
    s['A8'] = 'Qty NOK = Not recorded'
    s['A12'] = 'RESULTS BY PART'
    for col, head in zip('ACIKNP', ('Part number', 'Part name', 'Inspected', 'OK', 'NOK', 'Reworked')):
        s[f'{col}13'] = head
    parts = {}
    for r in recs:
        parts.setdefault((r['part'], r['part_name']), []).append(r)
    cached = {}
    row = 14
    for (part, name), group in parts.items():
        s[f'A{row}'] = part
        s[f'C{row}'] = name
        for col, src in (('I', 'J'), ('K', 'K'), ('N', 'M'), ('P', 'L')):
            s[f'{col}{row}'] = f"=SUMIF('Inspection Detail'!G:G,A{row},'Inspection Detail'!{src}:{src})"
        total = sum(g['inspected'] for g in group)
        cached[('Daily Inspection Summary', f'I{row}')] = summary_inspected.get(part, total) if summary_inspected else total
        cached[('Daily Inspection Summary', f'K{row}')] = sum(g['ok'] for g in group)
        row += 1

    d = wb.create_sheet('Inspection Detail')
    d['A1'] = 'Inspection Detail'
    heads = ['Project number', 'Date', 'Shift', 'Area', 'Auditor', 'Identifier', 'Part number', 'Part name',
             'Service performed', 'Quantity Inspected', 'OK', 'Reworked', 'NOK', 'Recorded defect', 'Comments',
             'Condition', 'Label photo', 'Defect photo', 'Source item ID', 'Source record URL', 'Supplier',
             'Auditor lookup ID', 'Recorded time (UTC)', 'Defect photo field', 'Inspected recorded', 'OK recorded',
             'NOK recorded']
    for c, h in enumerate(heads, 1):
        d.cell(3, c, h)
    for i, r in enumerate(recs, 4):
        vals = [job, datetime.strptime(day, '%Y-%m-%d'), r['shift'], r['area'], r['auditor'], r['ident'], r['part'],
                r['part_name'], 'Sort', r['inspected'], r['ok'], None, None if nok_blank else 0, None, None, 'OK',
                'Yes', None, r['item'], f'https://ppg.sharepoint.com/sites/{SITE}/Lists/{LIST}/{r["item"]}_.000',
                'SL', 7, '2026-11-06T13:05:00Z', None, 'Yes', 'Yes', 'No']
        for c, v in enumerate(vals, 1):
            d.cell(i, c, v)

    if photos:
        p = wb.create_sheet('Defect Photos')
        p['A1'] = 'Defect Photos'
        p['A3'] = 'Source item ID'; p['B3'] = 'Identifier'; p['C3'] = 'Photo'
        p['A4'] = recs[0]['item']; p['B4'] = recs[0]['ident']
        img = XLImage(io.BytesIO(PNG))
        p.add_image(img, 'C4')

    if source_records:
        x = wb.create_sheet('Source Records')
        cols = ['Source item ID', 'Source record URL', '@odata.etag', 'Title', 'LinkTitle', 'Date', 'Date0', 'Shift',
                'Area', 'AuditorLookupId', 'Identifier', 'PartNumber', 'QuantityInspected', 'QuantityOK',
                'PictureLabel', 'Condition', 'Supplier', 'id', 'ContentType', 'Modified', 'Created',
                '_UIVersionString', 'Attachments']
        for c, h in enumerate(cols, 1):
            x.cell(1, c, h)
        for i, r in enumerate(recs, 2):
            vals = [r['item'], f'https://ppg.sharepoint.com/sites/{SITE}/Lists/{LIST}/{r["item"]}_.000', f'"{r["item"]},3"',
                    job, job, f'{day}T06:00:00Z', '2026-11-06T13:05:00Z', r['shift'], r['area'], 7, r['ident'],
                    r['part'], r['inspected'], r['ok'], 'Yes', 'OK', 'SL', r['item'], 'Item',
                    '2026-11-06T14:00:00Z', '2026-11-06T13:05:00Z', '3.0', False]
            for c, v in enumerate(vals, 1):
                x.cell(i, c, v)
        for (ri, col), v in (source_overrides or {}).items():
            x.cell(ri + 2, cols.index(col) + 1, v)
    buf = io.BytesIO()
    wb.save(buf)
    return add_cached_values(buf.getvalue(), wb.sheetnames, cached)

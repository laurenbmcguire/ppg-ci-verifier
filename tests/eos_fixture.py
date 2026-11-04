"""A workbook shaped like the new-format PPG End-of-Shift report, with Excel-style saved formula results.

Rows mirror a real daily export shape (synthetic copy). openpyxl does not write formula results,
so the sheet XML is patched afterwards to include <v> cached values, as Excel would save them.
"""
import io
import re
import zipfile

from openpyxl import Workbook

ROWS = [  # identifier, part, inspected, rejected, defect, comment
    ('8010808539', '80139189', 34, 8, 'Holes / Glue', '7 holes 1 glue'),
    ('8010808540', '80139189', 34, 7, 'Holes / Glue', '7 holes'),
    ('8010808537', '80139189', 34, 11, 'Holes / Glue', '9 holes 2 glue'),
    ('8010808541', '80139189', 34, 10, 'Holes / Glue', '8 holes 2 glue'),
    ('8010852061', '80139189', 34, 3, 'Holes / Glue', '3 holes'),
    ('8010672070', '80139189', 34, 17, 'Holes / Glue', '10 holes 7 glue'),
    ('8010672071', '80139189', 35, 15, 'Holes / Glue', '7 holes 8 glue'),
    ('8010726199', '80139189', 34, 24, 'Holes / Glue', '4 holes 20 glue'),
    ('8010726202', '80139189', 34, 29, 'Holes / Glue', '9 holes 20 glue'),
    ('8010796870', '80139189', 32, 19, 'Holes / Glue', '3 holes 16 glue'),
    ('8010726174', '80139189', 33, 28, 'Holes / Glue', '3 holes 25 glue'),
    ('8010799672', '80139189', 32, 4, 'Holes / Glue', '1 holes 3 glue'),
    ('8010799675', '80139189', 34, 12, 'Holes / Glue', '4 holes 8 glue'),
]
HEADERS = ['Date', 'Shift', 'Area', 'Part number', 'Identifier / lot', 'Inspected for', 'Quantity Inspected',
           'Condition', 'Reject reason', 'Reject Quantity', 'Quantity Certified', 'Comments']
DAY = '2026-10-07'


def source_csv(rows=ROWS, day=DAY):
    out = ['Date,Shift,Area,Auditor,Identifier,Part Number,Qty Inspected,Qty OK,Qty Reworked,Qty NOK,Defect,Comments,Condition,Picture File']
    for ident, part, ins, rej, defect, comment in rows:
        out.append(f'{day},1,,Robert Swanke,{ident},{part},{ins},{ins - rej},,{rej},{defect},{comment},NOK,')
    return ('﻿' + '\n'.join(out) + '\n').encode()


def build(rows=ROWS, day=DAY, overrides=None, cached_overrides=None):
    """overrides: {(sheet, cell): value}; cached_overrides: {(sheet, cell): number} for formula results."""
    wb = Workbook()
    summary = wb.active
    summary.title = 'Summary'
    summary['B1'] = 'PPG QUALITY   /   END OF SHIFT DATA AND BILLING REPORT (new format)'
    summary['B3'] = 'Project'; summary['D3'] = '2230-OWENS-BASCO'
    summary['B4'] = 'Report date'; summary['D4'] = day
    summary['B20'] = 'Shift'; summary['G20'] = 'Inspected'; summary['I20'] = 'Certified'; summary['K20'] = 'Rejected'
    cached = {}
    first, last = 16, 15 + len(rows)
    for i, name in enumerate(('1st Shift', '2nd Shift', '3rd Shift')):
        ws = wb.create_sheet(name)
        ws['A1'] = 'PPG QUALITY / DAILY INSPECTION REPORT'
        ws['A3'] = name
        for col, h in enumerate(HEADERS, 1):
            ws.cell(15, col, h)
        data = rows if i == 0 else []
        for r, (ident, part, ins, rej, defect, comment) in enumerate(data, first):
            ws.cell(r, 1, day); ws.cell(r, 2, str(i + 1)); ws.cell(r, 4, part); ws.cell(r, 5, ident)
            ws.cell(r, 6, 'Holes / Glue'); ws.cell(r, 7, ins); ws.cell(r, 8, 'NOK' if rej else 'OK')
            ws.cell(r, 9, defect); ws.cell(r, 10, rej); ws.cell(r, 11, f'=G{r}-J{r}'); ws.cell(r, 12, comment)
            cached[(name, f'K{r}')] = ins - rej
        trow = last + 3 if data else 23
        ws.cell(trow, 3, 'Total Parts Inspected'); ws.cell(trow, 5, 'Total Certified'); ws.cell(trow, 7, 'Total Rejected')
        ws.cell(trow + 1, 3, f'=SUM(G{first}:G{last})'); ws.cell(trow + 1, 5, f'=SUM(K{first}:K{last})'); ws.cell(trow + 1, 7, f'=SUM(J{first}:J{last})')
        ins_t = sum(x[2] for x in data); rej_t = sum(x[3] for x in data)
        cached[(name, f'C{trow + 1}')] = ins_t; cached[(name, f'E{trow + 1}')] = ins_t - rej_t; cached[(name, f'G{trow + 1}')] = rej_t
        sr = 21 + i
        summary.cell(sr, 2, name)
        summary.cell(sr, 7, f"='{name}'!C{trow + 1}"); summary.cell(sr, 9, f"='{name}'!E{trow + 1}"); summary.cell(sr, 11, f"='{name}'!G{trow + 1}")
        cached[('Summary', f'G{sr}')] = ins_t; cached[('Summary', f'I{sr}')] = ins_t - rej_t; cached[('Summary', f'K{sr}')] = rej_t
    rp = wb.create_sheet('Rejects & Photos')
    rp['A1'] = 'Rejects & Photos'
    for (sheet, ref), value in (overrides or {}).items():
        wb[sheet][ref] = value
    cached.update(cached_overrides or {})
    buf = io.BytesIO(); wb.save(buf)
    return add_cached_values(buf.getvalue(), wb.sheetnames, cached)


def add_cached_values(raw, sheetnames, cached):
    zin = zipfile.ZipFile(io.BytesIO(raw))
    out = io.BytesIO()
    with zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED) as zout:
        for item in zin.infolist():
            data = zin.read(item)
            m = re.fullmatch(r'xl/worksheets/sheet(\d+)\.xml', item.filename)
            if m:
                name = sheetnames[int(m.group(1)) - 1]
                text = data.decode()
                for (sheet, ref), value in cached.items():
                    if sheet != name:
                        continue
                    text = re.sub(rf'(<c r="{ref}"[^>]*>)(<f>[^<]*</f>)(?:<v>[^<]*</v>|<v/>)?', rf'\g<1>\g<2><v>{value}</v>', text)
                data = text.encode()
            zout.writestr(item, data)
    return out.getvalue()

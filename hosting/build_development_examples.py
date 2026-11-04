"""Build only synthetic development examples; never reads real reports or sources.

Fixture authoring follows the accepted test layouts, copied here so this builder
has no runtime dependency on tests. Sources are separately authored constants,
not extracted from workbook output. All checks are read-only and release held.
Run from the project root: python -m hosting.build_development_examples
"""
from datetime import datetime
from pathlib import Path
import argparse
import copy
import hashlib
import io
import json
import zipfile
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment
from hosting.branded_reports import HEADERS, SHIFT_SHEETS, SUMMARY_BRAND, DETAIL_BRAND, fingerprint
from hosting.pratt_reports import (BRAND, DEFECTS, DETAIL_HEADERS, FAILURE_HEADERS,
    LOCATIONS, MAIN_GRAIN, MAIN_HEADERS, MULTI_GRAIN, MULTI_HEADERS,
    SUMMARY_BINDING, build_pratt_source, formula_map)
from hosting.spreadsheets import inspect_spreadsheet
from hosting.reconcile_spreadsheet import reconcile_result
DATE = '2026-11-03'
MAIN_REF = 'synthetic://development/main'
MULTI_REF = 'synthetic://development/multi'


def ppg_base():
    w = Workbook()
    s = w.active
    s.title = 'Summary'
    s['B1'] = SUMMARY_BRAND
    s['B13'] = 'Project Number'
    s['D13'] = 'Synthetic-Job'
    s['B14'] = 'Report Date'
    s['D14'] = datetime(2026, 11, 3)
    for coordinate, value in {'B8': 'Records', 'D8': 'Parts inspected', 'G8': 'Parts certified', 'J8': 'Parts rejected', 'B20': 'Shift', 'F20': 'Records', 'G20': 'Inspected', 'I20': 'Certified', 'K20': 'Rejected'}.items():
        s[coordinate] = value
    for name, shift in SHIFT_SHEETS.items():
        detail = w.create_sheet(name)
        detail['A1'] = DETAIL_BRAND
        detail['A3'] = name
        detail['C7'] = 'Synthetic-Job'
        detail['C8'] = datetime(2026, 11, 3)
        detail['C9'] = 'Shift ' + shift
        detail['A14'] = 'INSPECTION RECORDS'
        for i, h in enumerate(HEADERS, 1):
            detail.cell(15, i, h)
        if shift == '1':
            for i, value in enumerate([datetime(2026, 11, 3), 1, 'A', 'Part-1', 'Lot-1', None, 10, 'OK', None, None, 10, None, 'Not available'], 1):
                detail.cell(16, i, value)
        row = 20 + int(shift)
        s.cell(row, 2, 'Shift ' + shift)
        for col in (6, 7, 9, 11):
            s.cell(row, col, 'n/a')
    ref = "'1st Shift'!J16:J16"
    formulas = {'B9': '=SUM(F21:F23)', 'D9': '=SUM(G21:G23)', 'G9': '=SUM(I21:I23)', 'J9': f'=IF(COUNT({ref})=0,"Unreported",SUM({ref})&" known")', 'F21': "=COUNTA('1st Shift'!A16:A16)", 'G21': "=SUM('1st Shift'!G16:G16)", 'I21': "=SUM('1st Shift'!K16:K16)", 'K21': f'=IF(COUNT({ref})=0,"Unreported",SUM({ref})&" known")', 'F24': '=B9', 'G24': '=D9', 'I24': '=G9', 'K24': '=J9', 'J29': '=D9', 'L29': '=G9', 'N29': '=J9', 'J30': '=D9', 'L30': '=G9', 'N30': '=J9'}
    for coordinate, value in formulas.items():
        s[coordinate] = value
    w.create_sheet('Rejects & Photos')
    return w

def ppg_source_base():
    s = dict(schema='verifier-spreadsheet-source/1', scope=dict(site_id='site', list_id='list', job_id='job', project_number='Synthetic-Job', report_date='2026-11-03'), provenance_kind='independently_retrieved', captured_at='2026-11-04T00:00:00Z', source_reference='synthetic://separate-source', complete=True, consistency='unknown', shift_coverage_complete=False, timezone_status='unknown', records=[dict(id='1', date='2026-11-03', shift_id='1', part='Part-1', identifier='Lot-1', values=dict(inspected=10, certified=10, rejected=None), field_presence=dict(inspected='present', certified='present', rejected='absent'))])
    return rehash(s)

def rehash(s):
    s.pop('source_sha256', None)
    s['source_sha256'] = fingerprint(s)
    return s

def ppg_all_fields(reject=None):
    w = ppg_base()
    s = w['Summary']
    for coordinate in ('J9', 'K21', 'K24', 'N29', 'N30'):
        s[coordinate] = None
    for name in SHIFT_SHEETS:
        d = w[name]
        d['A7'] = 'Project'
        d['A8'] = 'Service type'
        d['C8'] = None
        d['A9'] = 'Project description'
        d['C9'] = None
        d['I9'] = 'Worksite'
        d['A19'] = 'SHIFT INSPECTION RESULTS'
        for off, label in enumerate(('Inspection records', 'Parts inspected', 'Parts certified', 'Rejected quantity', 'Defect rate', 'Inspection pass rate'), 20):
            d.cell(off, 1, label)
        if name == '1st Shift':
            d['J16'] = reject
            d['E20'] = '=COUNTA(A16:A16)'
            d['E21'] = '=SUM(G16:G16)'
            d['E22'] = '=SUM(K16:K16)'
            d['E23'] = '=IF(COUNT(J16:J16)=0,"",IF(COUNT(J16:J16)=COUNT(G16:G16),SUM(J16:J16),SUM(J16:J16)&" known; partial"))'
    s['B65'] = 'RECORDED RESULTS BY SHIFT'
    for c, v in ((2, 'Shift'), (3, 'Certified'), (4, 'Known rejects')):
        s.cell(66, c, v)
    for r, name in enumerate(SHIFT_SHEETS, 67):
        s.cell(r, 2, name)
    s['C67'] = "=SUM('1st Shift'!K16:K16)"
    s['B99'] = 'RESULTS BY PART NUMBER'
    for c, v in enumerate(('Part number', 'Part name', 'Inspected for', 'Inspected', 'Certified', 'Known rejects', 'Defect rate'), 2):
        s.cell(100, c, v)
    s['B101'] = 'Part-1'
    s['E101'] = "=SUMIF('1st Shift'!D16:D16,B101,'1st Shift'!G16:G16)"
    s['F101'] = "=SUMIF('1st Shift'!D16:D16,B101,'1st Shift'!K16:K16)"
    reg = w['Rejects & Photos']
    reg['A15'] = 'REJECT REGISTER'
    for c, v in enumerate(('Shift', 'Identifier / lot', 'Part number', 'Inspected', 'Known rejected', 'Reason', 'Comments', 'Photo'), 1):
        reg.cell(16, c, v)
    reg['A20'] = 'DEFECT PHOTOS'
    if reject is not None:
        s['J9'] = '=IF(COUNT(\'1st Shift\'!J16:J16)=0,"Unreported",SUM(\'1st Shift\'!J16:J16)&" known")'
        s['K21'] = '=J9'
        s['K24'] = '=J9'
        s['N29'] = '=J9'
        s['N30'] = '=J9'
        s['D67'] = "=SUM('1st Shift'!J16:J16)"
        s['G101'] = "=SUMIF('1st Shift'!D16:D16,B101,'1st Shift'!J16:J16)"
        if reject > 0:
            for c, v in enumerate(('1st Shift', 'Lot-1', 'Part-1', 10, reject), 1):
                reg.cell(17, c, v)
    return w

def ppg_active_tabs(reject=None):
    w = ppg_all_fields(reject)
    for name in ('2nd Shift', '3rd Shift'):
        del w[name]
    if reject is None:
        del w['Rejects & Photos']
    else:
        reg = w['Rejects & Photos']
        reg.title = 'Rejects'
        reg['H16'] = None
        reg['A20'] = None
    return w

def pratt_row(identifier, *, role='main', status='Complete', result='Pass', shift='1', location='Commercial Bulk', export='No', defects=None):
    r = dict(ID=identifier, Id=identifier, Date=DATE + 'T06:00:00Z', Status=status, AuditResult=result, DeliveryNote='0000' + str(identifier), PartNumber='aB-00' + str(identifier), OrderQty_x002e_=7, ActualQty_x002e_=7, Export=export, Location=location)
    r['Shift' if role == 'main' else 'Shift_x002e_'] = shift
    r['Defects' if role == 'main' else 'Defect'] = None if not defects else {'results': defects}
    if role == 'main':
        r['Resolution'] = {'results': ['Inspect at shipping']} if result == 'Fail' else None
    return r

def pratt_snapshots():
    main = [pratt_row(11), pratt_row(12, result='Fail', export='Yes', defects=list(DEFECTS[:2])), pratt_row(13, status='In Progress', result=None, shift='2'), pratt_row(14, shift='3', location='446', export='Yes', defects=['Other recorded nonfailure category'])]
    multi = [pratt_row(11, role='multi'), pratt_row(12, role='multi', status='In Progress'), pratt_row(13, role='multi', shift='2')]
    return ({'status': 200, 'next': None, 'data': main}, {'status': 200, 'next': None, 'data': multi})

def pratt_base(main=None, multi=None):
    default_main, default_multi = pratt_snapshots()
    main = default_main['data'] if main is None else main
    multi = default_multi['data'] if multi is None else multi
    w = Workbook()
    summary = w.active
    summary.title = 'Daily Summary'
    detail = w.create_sheet('Shift Detail')
    failure = w.create_sheet('Failure Summary')
    summary['A2'] = BRAND
    summary['A3'] = 'Daily audit report | November 3, 2026'
    summary['A33'] = 'Multi-pack children are shown separately. Do not add these records to the Main audit total.'
    for rownum, header in ((20, MAIN_HEADERS), (27, MULTI_HEADERS), (36, ('Recorded defect category', 'Occurrences'))):
        for col, value in enumerate(header, 1):
            summary.cell(rownum, col, value)
    for source_rows, start, field, columns in ((main, 21, 'Shift', (2, 3, 4, 6)), (multi, 28, 'Shift_x002e_', (2, 3, 4, 5))):
        for shift in ('1', '2', '3'):
            rows = [r for r in source_rows if r[field] == shift]
            complete = [r for r in rows if r['Status'] == 'Complete']
            values = [len(complete), sum((r['AuditResult'] == 'Pass' for r in complete)), sum((r['AuditResult'] == 'Fail' for r in complete)), sum((r['Status'] == 'In Progress' for r in rows))]
            rownum = start + int(shift) - 1
            summary.cell(rownum, 1, 'Shift ' + shift)
            for col, value in zip(columns, values):
                summary.cell(rownum, col, value)
        summary.cell(start + 3, 1, 'Daily total')
    failed = [r for r in main if r['Status'] == 'Complete' and r['AuditResult'] == 'Fail']
    for rownum, category in enumerate(DEFECTS, 37):
        summary.cell(rownum, 1, category)
        summary.cell(rownum, 2, sum((category in r['Defects']['results'] for r in failed)))
    summary['A40'] = 'Total occurrences'
    detail['A2'] = 'Completed Main audits by shift'
    for col, value in enumerate(DETAIL_HEADERS, 1):
        detail.cell(6, col, value)
    rownum = 7
    for shift in ('1', '2', '3'):
        for location in LOCATIONS:
            for export in ('No', 'Yes'):
                rows = [r for r in main if r['Status'] == 'Complete' and r['Shift'] == shift and (r['Location'] == location) and (r['Export'] == export)]
                failures = [r for r in rows if r['AuditResult'] == 'Fail']
                values = [int(shift), location, 'Domestic' if export == 'No' else 'Export', len(rows), sum((r['AuditResult'] == 'Pass' for r in rows)), len(failures)]
                values.extend((sum((category in r['Defects']['results'] for r in failures)) for category in DEFECTS))
                for col, value in enumerate(values, 1):
                    detail.cell(rownum, col, value)
                rownum += 1
    detail['A31'] = 'Daily total'
    failure['A2'] = 'Failure summary'
    failure['A3'] = str(len(failed)) + ' completed Main audits with recorded failures'
    for col, value in enumerate(FAILURE_HEADERS, 1):
        failure.cell(6, col, value)
    for rownum, r in enumerate(failed, 7):
        values = [r['DeliveryNote'], r['PartNumber'], r['OrderQty_x002e_'], r['ActualQty_x002e_'], '; '.join(r['Defects']['results']), r['Export'], int(r['Shift']), r['Location'], '; '.join(r['Resolution']['results'])]
        for col, value in enumerate(values, 1):
            failure.cell(rownum, col, value)
    for (sheet, address), value in formula_map().items():
        w[sheet][address] = value
    return w

def pratt_union():
    from datetime import datetime
    from collections import Counter
    from hosting.pratt_reports import ADDITIONAL_LABELS, ADDITIONAL_DEFECT_LABELS, ADDITIONAL_DEFECT_MAP, BILLING_LABELS, PRODUCTION_LABELS, SHIFT_NAMES, UNION_SHIFT_HEADERS
    main, multi = pratt_snapshots()
    w = pratt_base()
    for col, label in enumerate(('Reject quantity', 'Comments', 'Defect Photo', 'Original'), 10):
        w['Failure Summary'].cell(6, col, label)
    sheet = w.create_sheet('Project and Production')
    sheet['A2'] = 'Project and production results'
    sheet['A3'] = BRAND + ' | November 3, 2026'
    sheet['A6'] = 'Project details'
    sheet['B6'] = 'Value'
    labels = ('Project Number', 'Report Date', 'Paying Client', 'Client Contact Name', 'Worksite Location', 'Area', 'Project Service Type', 'Project Description', 'Prepared By PPG', 'Overall Daily Notes')
    for rownum, label in enumerate(labels, 7):
        sheet.cell(rownum, 1, label)
    sheet['B8'] = datetime(2026, 11, 3)
    for header_row, title, start, labels in ((19, 'Production measure', 20, PRODUCTION_LABELS), (35, 'Billing and productivity', 36, BILLING_LABELS)):
        for col, value in enumerate((title, '1st shift', '2nd shift', '3rd shift', 'Daily total'), 1):
            sheet.cell(header_row, col, value)
        for rownum, label in enumerate(labels, start):
            sheet.cell(rownum, 1, label)
    sheet['A47'] = 'Inspection notes'
    sheet['A53'] = 'Shift comments'
    sheet['B53'] = 'Comments'
    for rownum, label in enumerate(('1st shift', '2nd shift', '3rd shift'), 54):
        sheet.cell(rownum, 1, label)
    sheet['A59'] = 'Audit totals and outcomes are reported separately in Daily Summary. Production quantities are a different measure.'
    sheet = w.create_sheet('Additional Template Fields')
    sheet['A2'] = 'Additional inspection fields'
    sheet['A3'] = BRAND + ' | November 3, 2026'
    for header_row, title, start, labels in ((6, 'Inspection measure', 7, ADDITIONAL_LABELS), (31, 'Pratt audit defect field', 32, ADDITIONAL_DEFECT_LABELS)):
        for col, value in enumerate((title, '1st shift', '2nd shift', '3rd shift', 'Daily total'), 1):
            sheet.cell(header_row, col, value)
        for rownum, label in enumerate(labels, start):
            sheet.cell(rownum, 1, label)
            if rownum in ADDITIONAL_DEFECT_MAP:
                for col, shift in enumerate(('1', '2', '3'), 2):
                    sheet.cell(rownum, col, sum((r['Status'] == 'Complete' and r['AuditResult'] == 'Fail' and (r['Shift'] == shift) and (ADDITIONAL_DEFECT_MAP[rownum] in (r['Defects'] or {}).get('results', [])) for r in main['data'])))
                sheet.cell(rownum, 5, '=SUM(B%d:D%d)' % (rownum, rownum))
    for name, shift in SHIFT_NAMES.items():
        sheet = w.create_sheet(name)
        sheet['A2'] = name + ' inspection detail'
        sheet['A3'] = BRAND + ' | November 3, 2026 | Completed records'
        for col, label in enumerate(UNION_SHIFT_HEADERS, 1):
            sheet.cell(6, col, label)
        rownum = 7
        for role, rows in (('main', main['data']), ('multi', multi['data'])):
            for r in rows:
                if r['Status'] != 'Complete' or r['Shift' if role == 'main' else 'Shift_x002e_'] != shift:
                    continue
                defects = (r['Defects' if role == 'main' else 'Defect'] or {}).get('results', [])
                values = [datetime(2026, 11, 3), int(shift), r['Location'], r['PartNumber'], r['DeliveryNote'], None, None, None, None, None, None, None, None, r['AuditResult'], '; '.join(defects) or None, r['OrderQty_x002e_'], r['ActualQty_x002e_'], r['Export'], '; '.join((r.get('Resolution') or {}).get('results', [])) or None, 'Main audit' if role == 'main' else 'Multi-pack child']
                for col, value in enumerate(values, 1):
                    sheet.cell(rownum, col, value)
                rownum += 1
        end = rownum - 1
        sheet.cell(end + 3, 1, 'Shift inspection results')
        sheet.cell(end + 3, 2, 'Value')
        for rownum, label in enumerate(('Boxes Inspected', 'Total Parts Inspected', 'Total Certified', 'Total Rejected', 'Defect Rate', 'Inspection Pass %'), end + 4):
            sheet.cell(rownum, 1, label)
        sheet.cell(end + 12, 1, 'Billing')
        sheet.cell(end + 12, 2, 'Value')
        for rownum, label in enumerate(('Team Member / Inspector', 'Hours on Shift', 'Working Supervisor', '2-Hour Supervisor Charge', 'Billable Shift Hours', 'Shift Productivity'), end + 13):
            sheet.cell(rownum, 1, label)
    sheet = w.create_sheet('Results by Part')
    sheet['A2'] = 'Results by part number'
    sheet['A3'] = 'Completed Main audits | November 3, 2026'
    for col, label in enumerate(('Part number', 'Part name', 'Inspected for', 'Inspected', 'Certified', 'Rejected', 'Defect rate', 'Main audits'), 1):
        sheet.cell(6, col, label)
    for rownum, (part, count) in enumerate(Counter((r['PartNumber'] for r in main['data'] if r['Status'] == 'Complete')).items(), 7):
        sheet.cell(rownum, 1, part)
        sheet.cell(rownum, 8, count)
    return w


def ppg_workbook():
    workbook = ppg_active_tabs()
    detail = workbook['1st Shift']
    # Two separately typed fictional lots; no value is inferred for rejects.
    for column, value in enumerate([datetime(2026, 11, 3), 1, 'A', 'Part-1', 'Lot-2', None, 24, 'OK', None, None, 24, None, 'Not available'], 1):
        detail.cell(17, column, value)
    for sheet in workbook:
        for row in sheet:
            for cell in row:
                if cell.data_type == 'f':
                    import re
                    cell.value = re.sub(r'([A-Z]+)16:([A-Z]+)16', r'\g<1>16:\g<2>17', cell.value)
    return workbook


def ppg_source(mismatch=False):
    # Authored independently of workbook parsing. Null + absent is intentional.
    source = ppg_source_base()
    source['provenance_kind'] = 'retained_supplied_snapshot'
    source['source_reference'] = 'synthetic://development/ppg-separate-fixture'
    source['evidence_blockers'] = ['SYNTHETIC DEVELOPMENT DATA ONLY; not authenticated production evidence.']
    source['records'].append(dict(id='2', date=DATE, shift_id='1', part='Part-1', identifier='Lot-2',
        values=dict(inspected=24, certified=24, rejected=None),
        field_presence=dict(inspected='present', certified='present', rejected='absent')))
    if mismatch:
        source['records'][1]['values']['certified'] = 23
    return rehash(source)


def pratt_source(workbook_hash):
    main, multi = pratt_snapshots()
    main_raw = (json.dumps(main, indent=2, sort_keys=True, allow_nan=False) + '\n').encode()
    multi_raw = (json.dumps(multi, indent=2, sort_keys=True, allow_nan=False) + '\n').encode()
    binding = dict(library_file_id='synthetic-development-file', file_sha256=workbook_hash,
        report_date=DATE, main_grain=MAIN_GRAIN, multi_grain=MULTI_GRAIN,
        client_summary_binding=SUMMARY_BINDING,
        source_sha256={MAIN_REF: hashlib.sha256(main_raw).hexdigest(), MULTI_REF: hashlib.sha256(multi_raw).hexdigest()})
    source = build_pratt_source(json.dumps(binding).encode(), main_raw, multi_raw,
        captured_at='2026-11-04T03:20:00Z', main_source_reference=MAIN_REF,
        multi_source_reference=MULTI_REF)
    source['synthetic_provenance'] = 'Entirely fictional, separately authored source rows. No service was accessed.'
    return rehash(source), main, multi


def workbook_bytes(workbook, title='SYNTHETIC DEVELOPMENT ONLY'):
    workbook.properties.creator = 'Synthetic development fixture builder'
    workbook.properties.title = title
    workbook.properties.description = 'Fictional records. Not production evidence. Release held.'
    workbook.properties.created = datetime(2026, 11, 3)
    workbook.properties.modified = datetime(2026, 11, 3)
    for sheet in workbook:
        sheet.freeze_panes = 'A7' if sheet.title != 'Summary' else 'B20'
        for row in sheet:
            for cell in row:
                if cell.value is None:
                    continue
                cell.font = Font(name='Calibri', size=11, color='172B4D')
                cell.alignment = Alignment(vertical='top', wrap_text=True)
                if isinstance(cell.value, str) and not cell.value.startswith('=') and (cell.row in (1, 2, 6, 15, 20) or cell.value.isupper()):
                    cell.fill = PatternFill('solid', fgColor='183153')
                    cell.font = Font(name='Calibri', size=11, bold=True, color='FFFFFF')
        for column in sheet.columns:
            sheet.column_dimensions[column[0].column_letter].width = 20
        sheet.sheet_properties.pageSetUpPr.fitToPage = True
        sheet.page_setup.orientation = 'landscape'
        sheet.page_setup.paperSize = sheet.PAPERSIZE_A3
        sheet.page_setup.fitToWidth = 1
        sheet.page_setup.fitToHeight = 0
    raw = io.BytesIO()
    workbook.save(raw)
    # Stable ZIP timestamps make distributable fixtures reproducible.
    output = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(raw.getvalue())) as original, zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED) as result:
        for name in sorted(original.namelist()):
            info = zipfile.ZipInfo(name, (2026, 11, 3, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            result.writestr(info, original.read(name))
    return output.getvalue()


def build_examples(directory):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    entries = []
    provenance = 'SYNTHETIC DEVELOPMENT ONLY. All rows are fictional, created from explicit fixture constants; no real workbook, private source, credentials, or live service used.'
    def write(name, data):
        (directory / name).write_bytes(data)
        return dict(path=name, sha256=hashlib.sha256(data).hexdigest(), size_bytes=len(data))
    def json_bytes(value):
        return (json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + '\n').encode()
    ppg = workbook_bytes(ppg_workbook())
    pratt = workbook_bytes(pratt_union())
    pratt_evidence, main, multi = pratt_source(hashlib.sha256(pratt).hexdigest())
    raw_sources = [write('synthetic_pratt_main_snapshot.json', json_bytes(main)), write('synthetic_pratt_multi_snapshot.json', json_bytes(multi))]
    for slug, raw, source, purpose in (
        ('synthetic_ppg_active_tabs', ppg, ppg_source(), 'Known quantities agree; both rejected quantities remain unknown, not zero.'),
        ('synthetic_ppg_source_mismatch', workbook_bytes(ppg_workbook(), 'SYNTHETIC SOURCE MISMATCH DEMO'), ppg_source(True), 'Lot-2 certified quantity is 24 in workbook and 23 in separate source: exactly one numeric difference.'),
        ('synthetic_pratt_template_union', pratt, pratt_evidence, 'Nine sheets; three completed Main audits and two completed Multi-pack children remain separate.'),
        ('synthetic_plain_numeric_issues', b'label,inspected,accepted,rejected\nSynthetic mismatch,10,7,2\nSynthetic invalid,-2,1.5,\n', None, 'Plain CSV: inconsistent counts, negative/fractional quantity, and missing value.')):
        filename = slug + ('.xlsx' if source else '.csv')
        artifact = write(filename, raw)
        result = inspect_spreadsheet(filename, raw)
        source_artifact = None
        if source:
            source_artifact = write(slug + '.source.json', json_bytes(source))
            result = reconcile_result(result, source)
        expected = {key: result.get(key) for key in ('template_id', 'template_mapping_status', 'record_count', 'numeric_cells_checked', 'numeric_values_compared', 'source_difference_count', 'source_reconciliation_status', 'finding_counts', 'release_status', 'approval_effective', 'source_write_enabled', 'send_enabled')}
        entries.append(dict(id=slug, artifact=artifact, source=source_artifact, purpose=purpose, synthetic=True, expected=expected))
    index = dict(schema='verifier-development-examples/1', synthetic=True, provenance=provenance,
        generated_by='python -m hosting.build_development_examples', release_status='held',
        source_generation='Separate explicit synthetic constants, not workbook extraction; matching fixtures test implementation consistency, not source authenticity.',
        examples=entries, supporting_source_snapshots=raw_sources)
    write('index.json', json_bytes(index))
    write('README.txt', (provenance + '\n\nRun: python -m hosting.build_development_examples\n'
        'Inspect and reconcile each pair using the existing read-only verifier. All release gates remain held.\n'
        'Blank rejected values are unknown, never zero. Absent shifts are not certified inactive.\n'
        'Pratt Main audit counts and Multi-pack child counts are different grains and must not be added.\n'
        'The mismatch example changes only Lot-2 certified in the separate source from 24 to 23.\n'
        'JSON hashes in index.json bind exact artifact bytes; source_sha256 binds canonical source contents.\n'
        'Workbook styling and document metadata label development examples without changing mapped cells.\n').encode())
    return index


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=Path(__file__).resolve().parents[1] / 'samples' / 'development_examples')
    args = parser.parse_args()
    index = build_examples(args.output)
    print(json.dumps(index, indent=2))

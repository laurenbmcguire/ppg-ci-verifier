import io
import unittest

from openpyxl import Workbook

from hosting.spreadsheets import inspect_spreadsheet, header_identity
from hosting.source_compare import identify


def provisional(job='2207', client='SL Alabama', when='November 6, 2026'):
    """The layout of the provisional daily reports: the job and date live in title lines, and totals are formulas."""
    wb = Workbook(); s = wb.active; s.title = 'Summary'
    s['A1'] = 'PPG QUALITY / DAILY INSPECTION REPORT'
    s['A4'] = f'{client} · Job {job}'
    s['A6'] = f'{when} · Provisional daily report'
    s['A7'] = 'Shift completion is not confirmed. Figures include the records available at capture time.'
    s['A9'] = 'Captured: 2026-11-07T05:17:04.548Z UTC'
    s['A11'] = 'DAILY RESULTS'
    for c, v in zip('ABCDE', ('Shift', 'Records', 'Inspected', 'OK', 'Recorded rejects')):
        s[f'{c}12'] = v
    s['A13'] = 'Shift 1'; s['B13'] = 3
    s['C13'] = "=IF(COUNT('Shift 1'!C6:C8)=0,\"\",SUM('Shift 1'!C6:C8))"
    s['D13'] = "=IF(COUNT('Shift 1'!D6:D8)=0,\"\",SUM('Shift 1'!D6:D8))"
    s['A14'] = 'Total'
    d = wb.create_sheet('Shift 1')
    d['A1'] = 'PPG QUALITY / DAILY INSPECTION REPORT'
    for c, v in zip('ABCDEF', ('Identifier / lot', 'Part number', 'Inspected', 'OK', 'Rejected', 'Condition')):
        d[f'{c}5'] = v
    for r, (lot, part, n) in enumerate([('L1', '80139189', 40), ('L2', '80139189', 30), ('L3', '80139190', 20)], start=6):
        d[f'A{r}'] = lot; d[f'B{r}'] = part; d[f'C{r}'] = n; d[f'D{r}'] = n; d[f'F{r}'] = 'OK'
    out = io.BytesIO(); wb.save(out)
    return out.getvalue()


class ReportTitles(unittest.TestCase):
    def test_title_lines_give_job_and_date_not_a_formula(self):
        r = inspect_spreadsheet('PPG_2207_Nov06_Provisional.xlsx', provisional())
        ident = r['report_identity']
        self.assertEqual(ident['project_number'], '2207')
        self.assertEqual(ident['report_date'], '2026-11-06')
        self.assertEqual(ident.get('client'), 'SL Alabama')
        self.assertFalse(str(ident['project_number']).startswith('='))
        self.assertEqual(identify(r['filename'], r), ('2207', '2026-11-06'))
        self.assertEqual(r.get('report_day'), '2026-11-06')

    def test_capture_time_is_not_the_report_date(self):
        got = header_identity([('Summary', [['PPG QUALITY / DAILY INSPECTION REPORT'], ['Captured: 2026-11-07T05:17:04Z'], ['Job HANDS-HMMA-2219'], ['October 30, 2026']])])
        self.assertEqual(got['report_date'], '2026-10-30')
        self.assertEqual(got['project_number'], 'HANDS-HMMA-2219')

    def test_stored_formula_identity_is_ignored_and_filename_used(self):
        stored = dict(filename='PPG_2214_Nov06_Provisional.xlsx', report_identity={'project_number': "=IF(COUNT('Shift 3'!D6:D10)=0,\"\",SUM('Shift 3'!D6:D10))", 'report_date': None})
        self.assertEqual(identify(stored['filename'], stored)[0], '2214')

    def test_a_year_is_never_a_job(self):
        self.assertNotIn('project_number', header_identity([('Summary', [['Job 2026 results'], ['November 6, 2026']])]))


class RecordedRejects(unittest.TestCase):
    def test_recorded_rejects_column_counts_rejects(self):
        from openpyxl import load_workbook
        wb = load_workbook(io.BytesIO(provisional()))
        d = wb['Shift 1']
        d['E5'] = 'Recorded rejects'
        d['D7'] = 27; d['E7'] = 3; d['G5'] = 'Reject reason'; d['G7'] = 'Scratches'
        out = io.BytesIO(); wb.save(out)
        r = inspect_spreadsheet('PPG_2200_Nov06_Provisional.xlsx', out.getvalue())
        self.assertEqual(r['totals']['rejected']['known_sum'], '3')

    def test_no_reject_column_is_unknown_not_zero(self):
        from openpyxl import load_workbook
        wb = load_workbook(io.BytesIO(provisional()))
        wb['Shift 1']['E5'] = None
        out = io.BytesIO(); wb.save(out)
        r = inspect_spreadsheet('PPG_2207_Nov06_Provisional.xlsx', out.getvalue())
        self.assertIsNone((r.get('totals') or {}).get('rejected', {}).get('known_sum'))

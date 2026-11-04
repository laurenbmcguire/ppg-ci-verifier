"""Automatic column detection: any layout, any header names, multiple tables, never evaluates formulas."""
import io
from pathlib import Path
import sys
import unittest

from openpyxl import Workbook

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from hosting.adaptive_reports import field_for_header
from hosting.spreadsheets import inspect_spreadsheet


def xlsx(build):
    wb = Workbook(); build(wb); buf = io.BytesIO(); wb.save(buf); return buf.getvalue()


class HeaderRecognitionTests(unittest.TestCase):
    def test_synonyms_and_noise_words(self):
        cases = {'Inspected': 'inspected', 'Qty Inspected': 'inspected', 'Total Parts Sorted': 'inspected', 'Checked (pcs)': 'inspected',
                 'Certified': 'certified', 'Accepted Qty': 'certified', 'Good': 'certified', '# OK': 'certified',
                 'Rejected': 'rejected', 'Rejects': 'rejected', 'NG': 'rejected', 'Defects (pcs)': 'rejected', 'Qty Suspect': 'rejected'}
        for header, field in cases.items():
            with self.subTest(header=header):
                self.assertEqual(field_for_header(header), field)

    def test_ambiguous_or_descriptive_headers_stay_unmapped(self):
        for header in ('Inspected for', 'Qty', 'Quantity', 'Total', 'Part Number', 'Comments', 'Reject reason', 'Inspection photo', None, 12):
            with self.subTest(header=header):
                self.assertIsNone(field_for_header(header))


class AdaptiveWorkbookTests(unittest.TestCase):
    def test_header_anywhere_two_tables_totals_and_formulas(self):
        def build(wb):
            ws = wb.active; ws.title = 'Night'
            ws['A1'] = 'Sort report'; ws['A2'] = 'Customer'; ws['B2'] = 'SYN'
            for _ in range(5): ws.append([])
            ws.append(['Part Number', 'Total Parts Sorted', 'Good', 'NG'])      # header row 8
            ws.append(['SYN-1', 100, 95, 5])
            ws.append(['SYN-2', 50, 49, 2])                                     # 49 + 2 != 50
            ws.append(['Total', 150, 144, 7])                                   # matches sums
            ws.append([])
            ws.append(['Part Number', 'Total Parts Sorted', 'Good', 'NG'])      # second table, row 13
            ws.append(['SYN-3', 10, '=B14-D14', 0])                             # formula → unknown, not zero
            ws.append(['Total', 11, None, 0])                                   # 11 != 10
            wb.create_sheet('Photos')['A1'] = 'Photos go here'
        r = inspect_spreadsheet('night.xlsx', xlsx(build))
        self.assertEqual(r['mapping_mode'], 'adaptive')
        night = r['sheets'][0]
        self.assertEqual([t['header_row'] for t in night['tables']], [8, 13])
        self.assertEqual(night['tables'][0]['columns']['inspected']['header'], 'Total Parts Sorted')
        self.assertEqual(r['record_count'], 3)
        self.assertEqual(r['finding_counts']['inspected_count_mismatch'], 1)
        self.assertEqual(r['finding_counts']['total_mismatch'], 1)
        self.assertEqual(r['formula_count'], 1)
        self.assertIsNone(r['totals']['certified']['total'])  # unknown formula value keeps the total unknown
        self.assertIn('Photos', [s['name'] for s in r['sheets']])  # every sheet accounted for
        self.assertEqual(r['release_status'], 'held')
        self.assertEqual(r['internal_check_status'], 'findings')

    def test_clean_report_has_no_findings(self):
        def build(wb):
            ws = wb.active
            ws.append(['Date', 'Part', 'Inspected', 'Certified', 'Rejected'])
            ws.append(['2026-10-07', 'A', 20, 18, 2])
            ws.append(['2026-10-07', 'B', 5, 5, 0])
        r = inspect_spreadsheet('clean.xlsx', xlsx(build))
        # First-row conventional headers keep the original generic checks; offset ones use detection.
        self.assertIn(r['internal_check_status'], ('no_findings_in_checks_performed',))

    def test_csv_with_title_rows_above_headers(self):
        raw = b'PPG daily sort\nProject,SYN-9\n\nPart #,Qty Inspected,Qty Accepted,Qty Rejected\nX,10,7,3\nY,4,4,1\n'
        r = inspect_spreadsheet('offset.csv', raw)
        self.assertEqual(r['mapping_mode'], 'adaptive')
        self.assertEqual(r['sheets'][0]['tables'][0]['header_row'], 4)
        self.assertEqual(r['record_count'], 2)
        self.assertEqual(r['finding_counts']['inspected_count_mismatch'], 1)

    def test_no_quantity_columns_anywhere_stays_unverified(self):
        r = inspect_spreadsheet('notes.csv', b'name,comment\na,b\n')
        self.assertEqual(r['internal_check_status'], 'unverified')
        self.assertNotEqual(r.get('mapping_mode'), 'adaptive')


if __name__ == '__main__':
    unittest.main()

"""Common data-entry problems (hosting/common_issues.py), each seeded into an otherwise clean report."""
import io, os, sys, unittest
from collections import Counter
from openpyxl import Workbook

sys.path.insert(0, os.path.dirname(__file__))
from hosting.adaptive_reports import inspect_adaptive, field_for_header, label_for_header
from hosting.spreadsheets import inspect_spreadsheet

HEAD = ['Date', 'Shift', 'Auditor', 'Identifier', 'Part Number', 'Qty Inspected', 'Qty OK', 'Qty NOK', 'Defect', 'Comments']


def clean_rows(n=10):
    return [['2026-10-07', '1', 'Robert Swanke', f'801080{8500 + i}', '80139189', 34, 30, 4, 'Holes', '4 holes'] for i in range(n)]


def codes(rows, head=HEAD, title='1st Shift'):
    result = inspect_adaptive([(title, [head] + rows)], {'limitations': []})
    return Counter(f['code'] for f in result['findings']), result


class CommonIssueTests(unittest.TestCase):
    def test_clean_report_has_no_findings(self):
        found, result = codes(clean_rows())
        self.assertEqual(found, Counter())
        self.assertEqual(result['internal_check_status'], 'no_findings_in_checks_performed')

    def test_excel_error_value(self):
        rows = clean_rows(); rows[2][8] = '#REF!'
        self.assertIn('excel_error_value', codes(rows)[0])

    def test_hidden_and_garbled_characters(self):
        rows = clean_rows(); rows[1][2] = 'Robert Swanke'; rows[3][8] = 'HolesÃ©'
        self.assertEqual(codes(rows)[0]['hidden_characters'], 2)

    def test_same_value_written_differently(self):
        rows = clean_rows(); rows[4][8] = 'Holes/'; rows[5][3] = rows[6][3].replace('8010', '8010-')
        found = codes(rows)[0]
        self.assertIn('inconsistent_spelling', found)
        self.assertIn('duplicate_identifier_variant', found)

    def test_capital_letters_alone_are_not_an_error(self):
        rows = clean_rows(); rows[4][8] = 'HOLES '; rows[5][3] = rows[6][3].lower()
        found = codes(rows)[0]
        self.assertNotIn('inconsistent_spelling', found)
        self.assertNotIn('duplicate_identifier_variant', found)

    def test_placeholders_and_scientific_notation(self):
        rows = clean_rows(); rows[0][2] = 'TBD'; rows[1][3] = '8.01081E+09'; rows[2][4] = '0000'
        found = codes(rows)[0]
        self.assertEqual(found['placeholder_value'], 2)
        self.assertIn('scientific_notation_code', found)

    def test_value_in_wrong_column(self):
        rows = clean_rows(); rows[3][3] = '80139189'  # part number typed as identifier
        rows[4][5], rows[4][6] = 8010808539, 8010808535  # identifier typed into quantity
        found = codes(rows)[0]
        self.assertGreaterEqual(found['value_in_wrong_column'], 2)

    def test_extra_zero_outlier(self):
        rows = clean_rows(); rows[5][5], rows[5][6] = 340, 336
        found, result = codes(rows)
        self.assertIn('quantity_outlier', found)
        self.assertTrue(any('extra zero' in f.get('message', '') for f in result['findings']))

    def test_blank_in_filled_column_and_zero_inspected(self):
        rows = clean_rows(); rows[2][2] = None; rows[7][5:8] = [0, 0, 0]; rows[7][9] = None
        found = codes(rows)[0]
        self.assertIn('missing_value', found); self.assertIn('zero_inspected', found)

    def test_comment_counts_must_add_up(self):
        rows = clean_rows(); rows[1][9] = '3 holes 2 glue'  # 5, but NOK is 4
        found, result = codes(rows)
        self.assertEqual(found['comment_count_mismatch'], 1)
        rows = clean_rows(); rows[1][9] = '2 holes 2 glue, bin 3 on 2nd shift'
        self.assertNotIn('comment_count_mismatch', codes(rows)[0])

    def test_condition_columns_vs_suspect_quantity(self):
        head = ['Date', 'VIN Inspected', 'Vehicle', 'Quantity Inspected 8 Wheel Weights per Vehicle', 'LF Condition', 'RF Condition',
                'LR Condition', 'RR Condition', 'Suspect Reason', 'Suspect Wheel Position Quantity']
        rows = [['2026-09-29', f'HZA {257100 + i}', 'Santa Fe (MX5A)', 8, 'OK', 'OK', 'OK', 'OK', 'None', 0] for i in range(8)]
        rows[2][4:8] = ['SUSPECT', 'OK', 'SUSPECT', 'OK']; rows[2][9] = 2
        rows[3][5] = 'SUSPECT'; rows[3][9] = 0          # marked suspect but quantity 0
        rows[4][6] = 'SUSPCT'; rows[4][9] = 0           # typo in a condition value
        found, result = codes(rows, head)
        self.assertEqual(result['record_count'], 8)    # SUSPECT/OK rows are data, not new headers
        self.assertEqual(found['condition_count_mismatch'], 1)  # the typo row is reported as an unexpected value instead
        self.assertEqual(found['unexpected_condition_value'], 1)

    def test_labeled_total_and_unlabeled_sum_row(self):
        rows = clean_rows(4)
        sheet = [HEAD] + rows + [[None, None, None, None, None, '=SUM(F2:F5)', '=SUM(G2:G5)', '=SUM(H2:H5)', None, None],
                                 [], ['Shift results', 'Total Inspected', None, None, None, 140]]
        cached = {'1st Shift': [[None] * 10] * 5 + [[None, None, None, None, None, 136, 120, 16, None, None]]}
        result = inspect_adaptive([('1st Shift', sheet)], {'limitations': []}, cached=cached)
        found = Counter(f['code'] for f in result['findings'])
        self.assertEqual(result['record_count'], 4)     # =SUM row is a totals row, not data
        self.assertIn('labeled_total_mismatch', found)  # 140 vs 136

    def test_descriptive_headers(self):
        self.assertEqual(field_for_header('Quantity Inspected  8 Wheel Weights per Vehicle'), 'inspected')
        self.assertEqual(field_for_header('Suspect Wheel Position Quantity'), 'rejected')
        self.assertIsNone(field_for_header('Inspected For'))
        self.assertIsNone(field_for_header('Reject Reason'))
        self.assertEqual(label_for_header('VIN Inspected'), 'identifier')
        self.assertEqual(label_for_header('RR Condition'), 'condition')

    def test_hidden_rows_and_sheets_in_xlsx(self):
        wb = Workbook(); ws = wb.active; ws.title = '1st Shift'
        ws.append(HEAD)
        for row in clean_rows(6):
            ws.append(row)
        ws.row_dimensions[4].hidden = True
        ws.append([None] * 7 + ['=COUNTIF(A1:A1,"x")'])   # template formula only: not data
        ws.row_dimensions[8].hidden = True
        other = wb.create_sheet('Old data'); other.append(['scratch', 1]); other.sheet_state = 'hidden'
        buf = io.BytesIO(); wb.save(buf)
        result = inspect_spreadsheet('report.xlsx', buf.getvalue())
        found = Counter(f['code'] for f in result['findings'])
        self.assertEqual(found['hidden_rows_with_data'], 1)
        self.assertIn('hidden_sheet_with_data', found)
        msg = next(f['message'] for f in result['findings'] if f['code'] == 'hidden_rows_with_data')
        self.assertIn('rows 4)', msg)


if __name__ == '__main__':
    unittest.main()

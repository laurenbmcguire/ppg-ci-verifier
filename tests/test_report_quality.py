"""Duplicates, typos, validity and totals checks on an End-of-Shift-shaped workbook (any layout path)."""
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(Path(__file__).parent))
from eos_fixture import build, ROWS
from hosting.spreadsheets import inspect_spreadsheet
from hosting.adaptive_reports import near_miss

INFO = {'no_quantity_columns_detected', 'formula_results_used', 'known_template_layout_differs', 'no_data_rows'}


def issues(result):
    return {c: n for c, n in result['finding_counts'].items() if c not in INFO}


class CleanReportTests(unittest.TestCase):
    def test_clean_report_uses_saved_formula_results_and_has_no_issues(self):
        r = inspect_spreadsheet('eos.xlsx', build())
        self.assertEqual(r['mapping_mode'], 'adaptive')
        self.assertEqual(issues(r), {})
        self.assertEqual(r['internal_check_status'], 'no_findings_in_checks_performed')
        self.assertEqual(r['record_count'], 13)                 # Summary roll-up rows are not data rows
        self.assertEqual(r['totals']['inspected']['total'], '438')
        self.assertEqual(r['totals']['rejected']['total'], '187')
        self.assertEqual(r['totals']['certified']['total'], '251')  # from saved formula results
        self.assertGreater(r['formula_results_used'], 0)
        self.assertNotIn('_detail_records', r)


class TemplateRowTests(unittest.TestCase):
    def test_unused_template_rows_with_formulas_are_ignored(self):
        overrides = {}
        for r in range(16, 21):
            overrides[('2nd Shift', f'K{r}')] = f'=G{r}-J{r}'
        r = inspect_spreadsheet('eos.xlsx', build(overrides=overrides))
        self.assertEqual(issues(r), {})
        self.assertEqual(r['record_count'], 13)


class SeededErrorTests(unittest.TestCase):
    def check(self, code, rows=None, overrides=None, cached=None):
        r = inspect_spreadsheet('eos_bad.xlsx', build(rows=rows or list(ROWS), overrides=overrides, cached_overrides=cached))
        self.assertIn(code, r['finding_counts'], r['finding_counts'])
        return r

    def test_duplicate_identifier_and_row(self):
        rows = list(ROWS); rows[5] = rows[4]
        r = self.check('duplicate_identifier', rows)
        self.assertIn('duplicate_row', r['finding_counts'])

    def test_duplicate_across_shifts(self):
        r = self.check('duplicate_identifier', overrides={('2nd Shift', 'E16'): ROWS[0][0], ('2nd Shift', 'G16'): 34, ('2nd Shift', 'J16'): 1,
                                                           ('2nd Shift', 'D16'): '80139189', ('2nd Shift', 'I16'): 'Holes / Glue'})
        self.assertTrue(any('across shifts' in f.get('message', '') for f in r['findings']))

    def test_letter_o_in_identifier(self):
        rows = list(ROWS); rows[3] = ('8O10808541',) + rows[3][1:]
        self.check('identifier_format_differs', rows)

    def test_swapped_digits_part_number(self):
        rows = list(ROWS); rows[7] = (rows[7][0], '80139198') + rows[7][2:]
        self.check('possible_part_number_typo', rows)

    def test_defect_misspelling(self):
        rows = list(ROWS); rows[2] = rows[2][:4] + ('Holes / Gleu',) + rows[2][5:]
        self.check('possible_defect_typo', rows)

    def test_missing_defect_reason(self):
        rows = list(ROWS); rows[9] = rows[9][:4] + ('',) + rows[9][5:]
        self.check('missing_defect_reason', rows)

    def test_wrong_date_and_bad_date(self):
        self.check('date_differs_from_report', overrides={('1st Shift', 'A20'): '2026-10-06'})
        self.check('invalid_date', overrides={('1st Shift', 'A20'): '10/32/2026'})

    def test_shift_mismatch(self):
        self.check('shift_differs_from_sheet', overrides={('1st Shift', 'B17'): '2'})

    def test_rejected_more_than_inspected_and_count_mismatch(self):
        self.check('rejected_exceeds_inspected', overrides={('1st Shift', 'J16'): 50}, cached={('1st Shift', 'K16'): -16})
        self.check('inspected_count_mismatch', cached={('1st Shift', 'K16'): 30})

    def test_condition_conflict(self):
        self.check('condition_conflicts_with_quantity', overrides={('1st Shift', 'H16'): 'OK'})

    def test_totals_block_and_summary_mismatch(self):
        self.check('totals_block_mismatch', cached={('1st Shift', 'C32'): 999})
        self.check('summary_mismatch', cached={('Summary', 'K21'): 1})


class NearMissTests(unittest.TestCase):
    def test_near_miss_rules(self):
        self.assertTrue(near_miss('8O10808541', '8010808541'))
        self.assertTrue(near_miss('S8010799672', '8010799672'))
        self.assertTrue(near_miss('80139198', '80139189'))           # swapped neighbours
        self.assertFalse(near_miss('8010808537', '8010808539', one_char=False))  # sequential serials inside a report
        self.assertTrue(near_miss('8010808537', '8010808539'))       # but a valid suggestion against SharePoint


if __name__ == '__main__':
    unittest.main()

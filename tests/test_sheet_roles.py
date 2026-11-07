"""Regression tests: one authoritative record set per workbook (the November 6 SL Alabama 2214 double count)."""
import io
import unittest

from openpyxl import load_workbook

from hosting.spreadsheets import inspect_spreadsheet
from hosting.sheet_roles import parse_source_url
from tests.sl_daily_fixture import records, sl_daily

NAME = 'SL Alabama - 2214 - 2026-11-06.xlsx'


def check(raw, name=NAME):
    return inspect_spreadsheet(name, raw)


def inspected(r):
    return r['totals']['inspected']['total']


class The2214Case(unittest.TestCase):
    def test_exactly_5_records_and_400_inspected(self):
        r = check(sl_daily())
        self.assertEqual(r['record_count'], 5)
        self.assertEqual(inspected(r), '400')
        sel = r['dataset_selection']
        self.assertEqual(sel['authoritative'], ['Inspection Detail'])
        self.assertEqual(sel['roles']['Daily Inspection Summary'], 'summary')
        self.assertEqual(sel['roles']['Source Records'], 'source_export')
        self.assertEqual(sel['roles']['Defect Photos'], 'photos')
        self.assertEqual(sel['status'], 'certain')
        self.assertEqual(r['release_status'], 'held')

    def test_same_answer_without_the_optional_source_tab(self):
        r = check(sl_daily(source_records=False))
        self.assertEqual((r['record_count'], inspected(r)), (5, '400'))

    def test_same_answer_with_repeated_summary_and_source_tabs(self):
        raw = sl_daily()
        wb = load_workbook(io.BytesIO(raw))
        wb.copy_worksheet(wb['Daily Inspection Summary']).title = 'Summary (2)'
        wb.copy_worksheet(wb['Source Records']).title = 'Source Records (2)'
        buf = io.BytesIO(); wb.save(buf)
        r = check(buf.getvalue())
        self.assertEqual((r['record_count'], inspected(r)), (5, '400'))
        self.assertEqual(r['dataset_selection']['authoritative'], ['Inspection Detail'])

    def test_a_second_copy_of_the_detail_counts_once(self):
        wb = load_workbook(io.BytesIO(sl_daily(source_records=False)))
        wb.copy_worksheet(wb['Inspection Detail']).title = 'Inspection Detail Copy'
        buf = io.BytesIO(); wb.save(buf)
        r = check(buf.getvalue())
        self.assertEqual((r['record_count'], inspected(r)), (5, '400'))
        self.assertIn('duplicate_presentation', r['finding_counts'])

    def test_record_identity_is_site_list_item(self):
        r = check(sl_daily())
        src = r['records'][0]['source']
        self.assertEqual(src, dict(site='SLAlabama_AlexanderCity', list='Data 2214SLALSLAL', item='432'))
        self.assertEqual(parse_source_url('https://x.sharepoint.com/sites/A/Lists/B%20C/DispForm.aspx?ID=9'), ('A', 'B C', '9'))

    def test_photo_rows_are_not_production(self):
        r = check(sl_daily())
        self.assertFalse(any(rec['sheet'] == 'Defect Photos' for rec in r['records']))


class IdenticalLookingRecords(unittest.TestCase):
    def test_records_with_matching_values_but_different_items_all_count(self):
        r = check(sl_daily(recs=records(same_values=True)))
        self.assertEqual((r['record_count'], inspected(r)), (5, '400'))
        self.assertNotIn('duplicate_row', r['finding_counts'])
        self.assertNotIn('duplicate_source_record', r['finding_counts'])

    def test_the_same_item_twice_is_flagged_not_dropped(self):
        recs = records()
        recs[4]['item'] = recs[3]['item']
        r = check(sl_daily(recs=recs, source_records=False))
        self.assertEqual(r['record_count'], 5)
        self.assertIn('duplicate_source_record', r['finding_counts'])


class ComparedNotAdded(unittest.TestCase):
    def test_source_tab_difference_is_shown_by_item(self):
        r = check(sl_daily(source_overrides={(1, 'QuantityInspected'): 90}))
        self.assertEqual(inspected(r), '400')
        msgs = [f.get('message', '') for f in r['findings'] if f['code'] == 'source_sheet_value_mismatch']
        self.assertTrue(any('433' in m and '90' in m for m in msgs), msgs)

    def test_summary_difference_is_shown_by_part(self):
        r = check(sl_daily(summary_inspected={'80139189': 250}))
        self.assertEqual(inspected(r), '400')
        self.assertIn('summary_part_mismatch', r['finding_counts'])

    def test_partial_overlap_is_ambiguous_and_held(self):
        wb = load_workbook(io.BytesIO(sl_daily(source_records=False)))
        copy = wb.copy_worksheet(wb['Inspection Detail'])
        copy.title = 'More Records'
        copy['S4'] = 999  # one item differs: the sheets share 4 of 5 records
        buf = io.BytesIO(); wb.save(buf)
        r = check(buf.getvalue())
        self.assertEqual(r['dataset_selection']['status'], 'ambiguous')
        self.assertIn('ambiguous_record_sheets', r['finding_counts'])
        self.assertEqual(r['release_status'], 'held')
        self.assertEqual(r['record_count'], 5)


class BlankIsNotZero(unittest.TestCase):
    def test_blank_rejects_stay_unknown(self):
        r = check(sl_daily(nok_blank=True))
        self.assertIsNone(r['totals']['rejected']['total'])
        self.assertTrue(all(rec['values'].get('rejected') is None for rec in r['records']))

    def test_zero_rejects_stay_zero(self):
        r = check(sl_daily(nok_blank=False))
        self.assertEqual(r['totals']['rejected']['total'], '0')

    def test_units_audits_defects_rejects_are_separate(self):
        r = check(sl_daily())
        t = r['totals']
        self.assertEqual(t['inspected']['total'], '400')   # units inspected
        self.assertEqual(r['record_count'], 5)              # audit records
        self.assertIsNone(t['rejected']['total'])           # rejected units: not recorded


if __name__ == '__main__':
    unittest.main()

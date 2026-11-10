"""Date proposals respect the common vertical summary layout and remain proposals.

Only synthetic workbooks are used. Layout recognition supplies a cell location,
never source verification, release authority, or an automatic workbook edit.
"""
import copy
import datetime
import io
import tempfile
import unittest

from openpyxl import load_workbook

from hosting import suggest
from hosting.fixes import apply_fixes, col_letter
from hosting.spreadsheet_inbox import SpreadsheetInbox
from hosting.review_summary import summarize
from tests.sl_daily_fixture import sl_daily


SHEET = 'Daily Inspection Summary'
DAY = '2026-11-06'


def summary_rows(value=None):
    rows = [[None] * 16 for _ in range(8)]
    rows[3][1], rows[3][7], rows[3][12] = 'Date', 'Shift', 'Area'
    rows[4][1], rows[4][7], rows[4][12] = value, 1, 'VPC'
    rows[6][1], rows[6][2] = 'Project Number:', 'SLAL-HMMA-2236'
    return rows


def suggestions(rows, sheet=SHEET):
    out = []
    def add(sheet, cell, after, reason, source, confidence, sure=None):
        out.append(dict(sheet=sheet, cell=cell, after=after, reason=reason,
                        source=source, confidence=confidence, sure=sure))
    suggest.blank_details([(sheet, rows)], {'day': DAY}, add, col_letter)
    return out


def workbook(value=None, merged=False):
    wb = load_workbook(io.BytesIO(sl_daily()))
    ws = wb[SHEET]
    ws['A4'] = None
    for row in (4, 5, 7):
        for col, cell in enumerate(summary_rows(value)[row - 1], 1):
            ws.cell(row, col).value = cell
    if merged:
        for row in (4, 5):
            for left, right in (('B', 'G'), ('H', 'L'), ('M', 'P')):
                ws.merge_cells(f'{left}{row}:{right}{row}')
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


class SummaryDateSuggestions(unittest.TestCase):
    def test_populated_vertical_date_never_generates_a_header_row_fix(self):
        for value in (DAY, DAY + ' 00:00:00', datetime.date(2026, 11, 6),
                      datetime.datetime(2026, 11, 6), 46332, '=TODAY()',
                      'Unknown', 'Shift', 0, False):
            with self.subTest(value=value):
                self.assertEqual(suggestions(summary_rows(value)), [])

    def test_blank_vertical_date_targets_b5_and_does_not_claim_it_was_filled(self):
        for value in (None, '', '   '):
            with self.subTest(value=value):
                got = suggestions(summary_rows(value))
                self.assertEqual([(p['cell'], p['after']) for p in got], [('B5', DAY)])
                self.assertIn('Proposed:', got[0]['reason'])
                self.assertNotIn('Filled in', got[0]['reason'])

    def test_vertical_label_normalization_keeps_supported_coordinates(self):
        rows = summary_rows()
        rows[3][1], rows[3][7], rows[3][12] = 'Report Date:', ' SHIFT ', 'Area:'
        self.assertEqual([p['cell'] for p in suggestions(rows)], ['B5'])

    def test_incomplete_or_changed_vertical_layout_is_not_guessed(self):
        variants = []
        for col in (7, 12):
            rows = summary_rows(); rows[3][col] = None; variants.append(rows)
        rows = summary_rows(); rows[3][2] = 'Client'; variants.append(rows)
        rows = summary_rows(); rows[3][1] = None; rows[3][2] = 'Date'; variants.append(rows)
        for rows in variants:
            with self.subTest(header=rows[3]):
                self.assertEqual(suggestions(rows), [])
        self.assertEqual(suggestions(summary_rows(), 'Inspection Detail'), [])

    def test_date_shift_area_table_header_is_not_a_blank_detail(self):
        for row in (['Date', 'Shift', 'Area'], ['Project number', 'Date', 'Shift', 'Area']):
            self.assertEqual(suggestions([row, [None] * len(row)]), [])

    def test_unknown_vertical_date_does_not_become_a_horizontal_blank(self):
        for value in (DAY, '=TODAY()', 'Unknown'):
            self.assertEqual(suggestions([[None, 'Date'], [None, value]]), [])

    def test_horizontal_date_proposals_keep_their_existing_value_cell(self):
        self.assertEqual([p['cell'] for p in suggestions([['Date', None]])], ['B1'])
        rows = [['Client', None, 'Synthetic'], ['Report date', None, None]]
        self.assertEqual([p['cell'] for p in suggestions(rows)], ['C2'])
        self.assertEqual(suggestions([['Date', DAY]]), [])

    def test_nonempty_horizontal_value_is_not_reported_as_blank(self):
        for value in ('Unknown', '=TODAY()', 'Shift', 0, False):
            self.assertEqual(suggestions([['Date', value]]), [])

    def test_suggestion_generation_does_not_mutate_inputs(self):
        rows = summary_rows()
        before = copy.deepcopy(rows)
        suggestions(rows)
        self.assertEqual(rows, before)


class WorkbookDateSuggestions(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.box = SpreadsheetInbox(self.tmp.name)

    def ingest(self, raw):
        return self.box.ingest('Synthetic HMMA - SLAL-HMMA-2236 - 2026-11-06.xlsx', raw)

    def dates(self, item):
        return [p for p in self.box.fix_proposals(item['file_hash'])['proposals']
                if p['sheet'] == SHEET and '"Date" is blank' in p['reason']]

    def test_populated_merged_and_unmerged_templates_have_no_date_fix(self):
        for merged in (False, True):
            with self.subTest(merged=merged):
                raw = workbook(datetime.datetime(2026, 11, 6), merged)
                item = self.ingest(raw)
                self.assertEqual(self.dates(item), [])
                self.assertEqual(self.box.download(item['file_hash'])[1], raw)

    def test_blank_vertical_date_proposal_is_read_only_and_preserves_source_holds(self):
        for merged in (False, True):
            with self.subTest(merged=merged):
                raw = workbook(merged=merged)
                item = self.ingest(raw)
                self.box._update(item['file_hash'], lambda r: r.update(source_reconciliation=dict(
                    status='incomplete', verified=False, site_id='synthetic-site', list_id='synthetic-list',
                    bound_to=dict(file_hash=r['file_hash'], checker_version=r['checker_version']),
                    not_verified_because=['Synthetic exact source remains unverified.'])))
                before = self.box.get(item['file_hash'])
                holds = self.box.holds(before)
                self.assertTrue(any(h['hard'] and h['code'].startswith('source_') for h in holds))
                props = self.dates(item)
                self.assertEqual([(p['cell'], p['after']) for p in props], [('B5', DAY)])
                self.assertIn('Proposed:', props[0]['reason'])
                self.assertNotIn('Filled in', props[0]['reason'])
                after = self.box.get(item['file_hash'])
                self.assertEqual(after, before)
                self.assertEqual(self.box.download(item['file_hash'])[1], raw)
                self.assertEqual(self.box.holds(after), holds)
                self.assertFalse(summarize(after)['sharepoint']['source_verified'])
                self.assertFalse(props[0]['recommendation_eligible'])

    def test_vertical_date_context_is_not_bulk_apply_authority(self):
        raw = workbook()
        item = self.ingest(raw)
        prop = self.dates(item)[0]
        self.assertFalse(prop['recommendation_eligible'])
        with self.assertRaisesRegex(ValueError, 'Recommendations changed'):
            self.box.apply_fixes(item['file_hash'], [prop], expected_check_hash=item['check_hash'],
                                 recommended_only=True)
        self.assertEqual(self.box.download(item['file_hash'])[1], raw)
        self.assertFalse(self.box.get(item['file_hash']).get('fixes_applied'))

    def test_explicit_synthetic_apply_changes_only_actual_date_anchor(self):
        raw = workbook(merged=True)
        item = self.ingest(raw)
        props = self.dates(item)
        self.assertEqual(len(props), 1)
        fixed = apply_fixes('synthetic.xlsx', raw, props)
        old = load_workbook(io.BytesIO(raw))
        new = load_workbook(io.BytesIO(fixed))
        changed = [(ws.title, cell.coordinate, cell.value, new[ws.title][cell.coordinate].value)
                   for ws in old for row in ws for cell in row
                   if cell.value != new[ws.title][cell.coordinate].value]
        self.assertEqual(changed, [(SHEET, 'B5', None, DAY)])
        self.assertEqual(self.box.download(item['file_hash'])[1], raw)

    def test_covered_date_anchor_and_formulas_never_produce_a_fix(self):
        for formula in (True, False):
            wb = load_workbook(io.BytesIO(workbook()))
            ws = wb[SHEET]
            if formula:
                ws['B5'] = '=TODAY()'
            else:
                ws['A5'] = 'Existing merged value'
                ws.merge_cells('A5:G5')
            buf = io.BytesIO(); wb.save(buf)
            self.assertEqual(self.dates(self.ingest(buf.getvalue())), [])


if __name__ == '__main__':
    unittest.main()

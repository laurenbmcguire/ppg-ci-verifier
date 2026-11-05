import io
import os
import unittest
from unittest.mock import patch

from openpyxl import Workbook

from hosting import ai_layout
from hosting.review_summary import summarize
from hosting.spreadsheets import inspect_spreadsheet


def _xlsx(build):
    wb = Workbook()
    build(wb)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def scan_list(wb):
    """A new-format EOS file: title block, then a VIN scan list with no count columns."""
    ws = wb.active
    ws.title = 'Shift Log'
    ws['A1'] = 'Hyundai Mobis - End of Shift'
    ws['A3'] = 'Prepared by'; ws['B3'] = 'PPG'
    ws.append([])
    ws.append(['Scan Time', 'Vehicle ID', 'Model Code', 'Result', 'Why'])   # row 5
    for i in range(12):
        ws.append([f'06:{i:02d}', f'KMHL{i:07d}', 'DN8', 'NOK' if i in (3, 7) else 'OK', 'Scratch' if i in (3, 7) else None])
    other = wb.create_sheet('Contacts')
    other.append(['Name', 'Phone'])
    other.append(['Lauren', '555'])


def counts(wb):
    ws = wb.active
    ws.title = 'Daily'
    ws.append(['Containment report'])
    ws.append(['Lot', 'Qty in', 'Good', 'Bad'])
    ws.append(['L1', 100, 98, 2])
    ws.append(['L2', 50, 50, 0])


def answer(tables, notes='ok'):
    return {'content': [{'type': 'tool_use', 'name': 'report_layout', 'input': {'tables': tables, 'notes': notes}}], 'model': 'test-model'}


class AILayoutTests(unittest.TestCase):
    def setUp(self):
        ai_layout._CACHE.clear()
        self.env = patch.dict(os.environ, {'ANTHROPIC_API_KEY': 'test-key'})
        self.env.start()

    def tearDown(self):
        self.env.stop()

    def test_scan_list_without_count_columns_is_found_by_ai(self):
        raw = _xlsx(scan_list)
        spec = dict(sheet='Shift Log', kind='detail', header_row=5, first_data_row=6, last_data_row=17,
                    columns=dict(identifier='B', part='C', condition='D', defect='E'), one_row_per_unit=True, reject_values=['NOK'])
        with patch('hosting.ai_review._post', return_value=answer([spec])) as post:
            result = inspect_spreadsheet('eos.xlsx', raw)
        self.assertTrue(post.called)
        self.assertEqual(result['layout_source'], 'ai')
        self.assertEqual(result['record_count'], 12)
        self.assertEqual(result['totals']['inspected']['known_sum'], '12')
        self.assertEqual(result['totals']['rejected']['known_sum'], '2')
        self.assertEqual(result['totals']['certified']['known_sum'], '10')
        self.assertNotIn('no_rows_found', [i['code'] for i in summarize(result)['issues']])

    def test_without_ai_the_same_file_has_no_rows(self):
        os.environ.pop('ANTHROPIC_API_KEY')
        result = inspect_spreadsheet('eos.xlsx', _xlsx(scan_list))
        self.assertIn('no_rows_found', [i['code'] for i in summarize(result)['issues']])

    def test_unusual_count_headers_mapped_by_ai(self):
        spec = dict(sheet='Daily', kind='detail', header_row=2, first_data_row=3, columns=dict(identifier='A', inspected='B', certified='C', rejected='D'))
        with patch('hosting.ai_review._post', return_value=answer([spec])):
            result = inspect_spreadsheet('daily.xlsx', _xlsx(counts))
        self.assertEqual(result['record_count'], 2)
        self.assertEqual(result['totals']['inspected']['known_sum'], '150')

    def test_bad_answers_are_ignored(self):
        bad = [dict(sheet='Nope', kind='detail', header_row=2, first_data_row=3, columns={'inspected': 'B'}),
               dict(sheet='Daily', kind='detail', header_row=99, first_data_row=100, columns={'inspected': 'B'}),
               dict(sheet='Daily', kind='detail', header_row=2, first_data_row=3, columns={'inspected': 'ZZ'})]
        with patch('hosting.ai_review._post', return_value=answer(bad)):
            self.assertIsNone(ai_layout.detect([('Daily', [['a'], ['b'], ['c']])]))

    def test_ai_failure_falls_back_quietly(self):
        from hosting.ai_review import AIReviewError
        with patch('hosting.ai_review._post', side_effect=AIReviewError('down')):
            result = inspect_spreadsheet('eos.xlsx', _xlsx(scan_list))
        self.assertIn('no_rows_found', [i['code'] for i in summarize(result)['issues']])

    def test_turned_off(self):
        with patch.dict(os.environ, {'AI_LAYOUT': '0'}), patch('hosting.ai_review._post') as post:
            inspect_spreadsheet('eos.xlsx', _xlsx(scan_list))
        post.assert_not_called()


if __name__ == '__main__':
    unittest.main()

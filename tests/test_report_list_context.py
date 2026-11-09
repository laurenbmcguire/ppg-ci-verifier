import math
import tempfile
import unittest
from hosting.review_summary import quantity_summary, summarize
from hosting.spreadsheet_inbox import SpreadsheetInbox


class ReportListContext(unittest.TestCase):
    def test_each_measure_distinguishes_partial_unknown_and_zero(self):
        q = quantity_summary({'inspected': {'known_sum': '120', 'total': None, 'unit': 'parts'},
                              'certified': {'known_sum': '110', 'total': '110', 'unit': 'parts'},
                              'rejected': {'total': '0', 'known_sum': '0', 'unit': 'rows'}})
        self.assertEqual(q['inspected'], dict(value=120, partial=True, unit='parts', complete=False))
        self.assertEqual(q['certified'], dict(value=110, partial=False, unit='parts', complete=True))
        self.assertEqual(q['rejected'], dict(value=0, partial=False, unit='rows', complete=True))
        self.assertEqual(quantity_summary({})['rejected'], dict(value=None, partial=False, unit=None, complete=False))

    def test_malformed_negative_or_nonfinite_numbers_are_unknown(self):
        for value in (True, False, '', 'abc', math.nan, math.inf, -1):
            with self.subTest(value=value):
                self.assertIsNone(quantity_summary({'inspected': {'total': value}})['inspected']['value'])
        self.assertIsNone(quantity_summary({'inspected': {'total': '12', 'known_sum': '99'}})['inspected']['value'])
        self.assertTrue(quantity_summary({'inspected': {'total': '12', 'known_sum': '12', 'missing_count': 1}})['inspected']['partial'])
        self.assertIsNone(quantity_summary({'inspected': {'known_sum': '12', 'numeric_range_exceeded': True}})['inspected']['value'])

    def test_list_is_read_only_bounded_and_has_metadata_without_raw_rows(self):
        with tempfile.TemporaryDirectory() as root:
            inbox = SpreadsheetInbox(root)
            raw = b'identifier,inspected,certified,rejected\nSYNTHETIC-1,12,12,\n'
            item = inbox.ingest('FabricatedReport.csv', raw)
            before = inbox.get(item['file_hash'])
            result = inbox.list()
            row = result['spreadsheets'][0]
            self.assertIn('quantities', row['review'])
            self.assertNotIn('records', row)
            self.assertNotIn('rows', row)
            self.assertNotIn('issues', row['review'])
            self.assertEqual(inbox.get(item['file_hash']), before)
            self.assertEqual(inbox.download(item['file_hash'])[1], raw)
            self.assertEqual(result['limit'], 100)
            self.assertFalse(result['has_more'])
            self.assertIsNone(row['review']['day'])
            self.assertIsNotNone(row['received_at'])

if __name__ == '__main__': unittest.main()

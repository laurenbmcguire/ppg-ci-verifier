import unittest
from hosting.adaptive_reports import header_text, field_for_header, label_for_header
from hosting.spreadsheets import inspect_spreadsheet
from hosting.fixes import proposals
from hosting.review_summary import summarize

HEAD = 'Source list,Item ID,Date,Shift,AuditResult,DeliveryNote,PartNumber,Location,OrderQty_x002e_,ActualQty_x002e_,AuditorLookupId,Defects'


def csv_report(rows):
    lines = ['Packing audit', '', HEAD] + rows
    return '\n'.join(lines).encode()


ROWS = ['Main,%d,"""2026-11-04T06:00:00Z""",1,%s,05749%05d,%s,Commercial Bulk,%d,%d,379,' % (264000 + i, 'Fail' if i in (3, 7) else 'Pass', i, part, q, q)
        for i, (part, q) in enumerate([('30G5030', 1), ('30G5030', 2), ('30g5030', 1), ('ST1946-115', 4), ('ST1946-015', 4), ('ST1946-015', 4),
                                       ('ST1946-015', 4), ('5329809-01', 1), ('32A4314', 1), ('32A4314', 1)] + [('P%d' % n, 1) for n in range(30)])]


class SharePointExportTests(unittest.TestCase):
    def test_header_names_are_decoded(self):
        self.assertEqual(header_text('OrderQty_x002e_'), 'Order Qty.')
        self.assertEqual(header_text('PartNumber'), 'Part Number')
        self.assertEqual(field_for_header('ActualQty_x002e_'), 'inspected')
        self.assertEqual(label_for_header('AuditResult'), 'condition')
        self.assertEqual(label_for_header('DeliveryNote'), 'identifier')
        self.assertEqual(label_for_header('OrderQty_x002e_'), 'expected')
        self.assertIsNone(field_for_header('Defects'))

    def test_rows_found_and_failed_audits_counted(self):
        r = inspect_spreadsheet('PW packing audit.csv', csv_report(ROWS))
        self.assertEqual(r['record_count'], len(ROWS))
        self.assertEqual(r['report_day'], '2026-11-04')
        self.assertEqual(r['totals']['rejected']['known_sum'], '2')
        self.assertEqual(r['totals']['rejected']['unit'], 'rows')
        codes = {f['code'] for f in r['findings']}
        self.assertNotIn('possible_part_number_typo', codes)  # big catalog: one digit apart = different parts
        self.assertNotIn('no_data_rows', codes)

    def test_part_number_capitals_get_a_fix(self):
        raw = csv_report(ROWS)
        r = inspect_spreadsheet('PW packing audit.csv', raw)
        fixes = [p for p in proposals(r, 'PW packing audit.csv', raw) if p['before'] == '30g5030']
        self.assertEqual(len(fixes), 1)
        self.assertEqual(fixes[0]['after'], '30G5030')


class VerifyTests(unittest.TestCase):
    def test_dismissed_issues_leave_the_count(self):
        result = {'filename': 'x.csv', 'record_count': 5, 'checker_version': 'v',
                  'findings': [dict(code='duplicate_row', sheet='S', row=4, message='Same as row 3'),
                               dict(code='duplicate_row', sheet='S', row=9, message='Same as row 8')]}
        keys = [i['key'] for i in summarize(result)['issues']]
        result['ai_verify'] = dict(status='done', checker_version='v', verdicts={
            keys[0]: dict(verdict='not_an_issue', reason='Own record ID.', fixable=False),
            keys[1]: dict(verdict='valid', reason='Exact copy.', fixable=True)})
        from hosting.review_summary import check_run
        result['ai_verify']['run'] = check_run(result)
        out = summarize(result)
        self.assertEqual(len(out['issues']), 1)
        self.assertEqual(out['issues'][0]['verdict'], 'valid')
        self.assertEqual(out['dismissed'][0]['verdict_reason'], 'Own record ID.')
        self.assertEqual(out['verify']['dismissed'], 1)
        self.assertEqual(len(summarize(result, use_verdicts=False)['issues']), 2)

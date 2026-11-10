"""A majority-pattern hint is not authority to rewrite an identifier."""
import copy
import unittest
from unittest.mock import patch

from hosting.adaptive_reports import inspect_adaptive, shape
from hosting.explain import explain
from hosting.review_summary import summarize
from hosting.review_decisions import decide


def report(part='00356871-V-RH'):
    rows = [['Identifier', 'Part number', 'Quantity Inspected', 'OK', 'NOK']]
    rows += [[f'LOT-{i:02}', part if i == 16 else '12345678 AA', 1, 1, 0] for i in range(1, 30)]
    result = inspect_adaptive([('Inspection Detail', rows)], {'limitations': []})
    result.update(file_hash='a' * 64, checker_version='synthetic')
    return result


class FormatWarningGuidance(unittest.TestCase):
    def test_outlier_is_a_pattern_hint_not_a_replacement(self):
        item = report()
        before = copy.deepcopy(item)
        finding = next(i for i in summarize(item)['issues'] if i['code'] == 'part_format_differs')
        self.assertEqual(finding['row'], 17)
        self.assertIn('00356871-V-RH', finding['text'])
        self.assertIn('######## AA', finding['text'])
        self.assertFalse(finding['sure'])
        self.assertIsNone(finding['only'])
        self.assertIn('alone does not establish an error', finding['why'])
        self.assertIn('Keep the value unchanged', finding['fix'])
        # Presentation guidance does not claim verification, clear holds or change stored evidence.
        self.assertEqual(finding['severity'], 'high')
        self.assertEqual(item, before)

    def test_case_is_already_ignored_by_pattern_but_values_are_not_rewritten(self):
        self.assertEqual(shape('12345678 aa'), shape('12345678 AA'))
        for value in ('00356871-v-rh', '00356871-V-RH ', '00356871-V-RH', '00356871-V-LH',
                      'OO356871-V-RH', '00356871 V RH', '0356871-V-RH'):
            item = report(value)
            before = copy.deepcopy(item)
            summarize(item)
            self.assertEqual(item, before)

    def test_guidance_never_says_lookalike_identifiers_are_equivalent(self):
        for code in ('part_format_differs', 'identifier_format_differs'):
            hint = explain(code)
            self.assertFalse(hint['sure'])
            self.assertIn('exact assigned source', hint['fix'])
            self.assertIsNone(hint['only'])

    def test_kept_and_deferred_decisions_keep_their_fingerprints(self):
        for action in ('reject', 'defer'):
            item = report()
            # A decision made with the prepatch explanation must still apply.
            old = dict(why='This value is shaped differently from the others in the report, which often means it was mistyped or pasted wrongly.',
                       fix='Check it against the scan or label.', sure=False)
            with patch.dict('hosting.explain.CATALOG', {'part_format_differs': old}), patch.dict(
                    'hosting.explain.WRITTEN', {'part_format_differs': 'Often the same part number typed a little differently (like the letter O for a zero). Check it; if that is all, nothing else is wrong.'}):
                review = summarize(item)
            now = summarize(item)
            self.assertEqual(now['counts'], review['counts'])
            self.assertEqual(now['status'], review['status'])
            self.assertEqual([(i['key'], i['fp']) for i in now['issues']],
                             [(i['key'], i['fp']) for i in review['issues']])
            finding = next(i for i in review['issues'] if i['code'] == 'part_format_differs')
            decide(item, {i['key']: i for i in review['issues']}, ids=[finding['key']], action=action,
                   reason='Reviewed exact source', by='Reviewer', request_id=action,
                   evidence=['Synthetic source item confirms the exact recorded value'],
                   expected_rev=0, expected_fps={finding['key']: finding['fp']}, expected_run=review['run'])
            after = summarize(item)
            found = next(i for i in after['issues'] + after['dismissed'] if i['key'] == finding['key'])
            self.assertEqual(found['fp'], finding['fp'])
            self.assertFalse(after['stale_decisions'])
            self.assertEqual(found['decision']['action'], action)


if __name__ == '__main__':
    unittest.main()

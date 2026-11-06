"""Plain explanations and the sure / not sure label for every issue, and the three newer common-data checks."""
import ast
import glob
import os
import sys
import unittest
from collections import Counter

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import eos_fixture
from hosting import explain, overview
from hosting.adaptive_reports import inspect_adaptive
from hosting.review_summary import summarize
from hosting.spreadsheets import EXTERNAL_CONTENT_CODES, inspect_spreadsheet

HOSTING = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'hosting')
HEAD = ['Date', 'Shift', 'Auditor', 'Identifier', 'Part Number', 'Qty Inspected', 'Qty OK', 'Qty NOK', 'Defect', 'Comments']
# Codes built from an f-string: what each placeholder can be.
FSTRINGS = {
    "f'possible_{kind}_typo'": ['possible_defect_typo', 'possible_condition_typo', 'possible_auditor_typo'],
    "f'{kind}_format_differs'": ['identifier_format_differs', 'part_format_differs'],
    "f'unusual_{field}_volume'": ['unusual_scans_volume', 'unusual_inspected_volume', 'unusual_rejected_volume'],
}


def _arms(node, found):
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        found.add(node.value)
    elif isinstance(node, ast.IfExp):
        _arms(node.body, found)
        _arms(node.orelse, found)
    elif isinstance(node, ast.JoinedStr):
        text = ast.unparse(node)
        assert text in FSTRINGS, f'new f-string finding code {text}: add what it can expand to in FSTRINGS (and to hosting/explain.py)'
        found.update(FSTRINGS[text])


def emitted_codes():
    """Every finding code written in hosting/*.py: the first argument of finding()/difference() (and add() in pratt_reports),
    code=... keywords, the external-content codes and the source adapter's blockers."""
    found = set(EXTERNAL_CONTENT_CODES)
    for path in glob.glob(os.path.join(HOSTING, '*.py')):
        base = os.path.basename(path)
        for node in ast.walk(ast.parse(open(path, encoding='utf-8').read())):
            if not isinstance(node, ast.Call):
                continue
            f = node.func
            name = f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else None
            if node.args and (name in ('finding', 'difference') or (name == 'add' and base == 'pratt_reports.py')
                              or (name == 'append' and base == 'source_adapter.py')):
                _arms(node.args[0], found)
            for kw in node.keywords:
                if kw.arg == 'code' and isinstance(kw.value, (ast.Constant, ast.JoinedStr)) and (
                        not isinstance(kw.value, ast.Constant) or isinstance(kw.value.value, str)):
                    _arms(kw.value, found)
    return found


def common(rows, title='1st Shift'):
    result = inspect_adaptive([(title, [HEAD] + rows)], {'limitations': []})
    return Counter(f['code'] for f in result['findings']), result


def varied_rows(n=14):
    rows = []
    for i in range(n):
        ins, nok = 30 + i % 5, 2 + i % 4
        rows.append(['2026-10-07', '1', 'Robert Swanke', f'801080{8500 + i}', '80139189', ins, ins - nok, nok, 'Holes', f'{nok} holes'])
    return rows


class ExplainCatalogTests(unittest.TestCase):
    def test_every_emitted_code_has_an_entry(self):
        codes = emitted_codes()
        self.assertGreater(len(codes), 150)   # the scan really found the checks
        missing = sorted(c for c in codes if explain.lookup(c) is None)
        self.assertEqual(missing, [], 'codes with no why/fix/sure in hosting/explain.py')

    def test_entries_are_complete_plain_sentences(self):
        for code, e in explain.CATALOG.items():
            self.assertEqual(set(e), {'why', 'fix', 'sure'}, code)
            self.assertIsInstance(e['sure'], bool, code)
            for key in ('why', 'fix'):
                self.assertTrue(e[key].endswith('.'), f'{code} {key}')
                self.assertLess(len(e[key]), 200, f'{code} {key} is not short')
                self.assertNotIn('AI', e[key].split(), code)

    def test_unknown_code_falls_back_and_is_never_sure(self):
        self.assertIsNone(explain.lookup('some_new_code'))
        e = explain.explain('some_new_code')
        self.assertFalse(e['sure'])
        self.assertTrue(e['why'] and e['fix'])
        self.assertFalse(explain.explain(None)['sure'])

    def test_certainty_classification(self):
        for code in ('excel_error_value', 'duplicate_row', 'negative_count_or_quantity', 'rejected_exceeds_inspected',
                     'inspected_count_mismatch', 'total_mismatch', 'summary_mismatch', 'vin_has_i_o_q', 'differs_from_sharepoint'):
            self.assertTrue(explain.lookup(code)['sure'], code)
        for code in ('possible_part_number_typo', 'possible_identifier_typo', 'quantity_outlier', 'identifier_unusual_length',
                     'vin_check_digit_wrong', 'unusual_reject_rate', 'unusual_scans_volume', 'placeholder_value', 'duplicate_identifier',
                     'fill_series_identifiers', 'value_in_wrong_column', 'comment_count_mismatch', 'missing_value',
                     'duplicate_count_block', 'total_row_blank', 'defect_reject_mismatch'):
            self.assertFalse(explain.lookup(code)['sure'], code)
        # anything a code says is only "possible" or a pattern guess must not be sure
        for code, e in explain.CATALOG.items():
            if code.startswith(('possible_', 'unusual_')) or code.endswith('_unverified'):
                self.assertFalse(e['sure'], code)


class SummarizeFieldsTests(unittest.TestCase):
    def test_every_issue_gets_why_fix_sure_and_keeps_its_fields(self):
        rows = [list(r) for r in eos_fixture.ROWS]
        rows[2] = (rows[2][0], rows[2][1], 10, 12, 'Holes / Glue', '')    # rejected exceeds inspected
        result = inspect_spreadsheet('job.xlsx', eos_fixture.build(rows))
        result['sharepoint'] = dict(status='compared', findings=[dict(code='not_on_sharepoint', message='X is not on SharePoint.', sheet='SharePoint comparison')])
        result['baseline'] = dict(status='compared', findings=[dict(code='unusual_reject_rate', severity='medium', message='Rate is unusual.')])
        result['ai_review'] = dict(status='done', issues=[dict(category='odd_pattern', issue='Looks odd', severity='medium', sheet='1st Shift', row=20,
                                                               why='The AI says why.', check='Look at row 20.')])
        review = summarize(result)
        self.assertGreaterEqual(len(review['issues']), 4)
        for i in review['issues']:
            for field in ('key', 'source', 'severity', 'code', 'where', 'text'):
                self.assertIn(field, i)
            self.assertTrue(i['why'] and i['fix'], i['code'])
            self.assertIsInstance(i['sure'], bool, i['code'])
        by = {i['code']: i for i in review['issues']}
        self.assertTrue(by['rejected_exceeds_inspected']['sure'])
        self.assertEqual(by['rejected_exceeds_inspected']['why'], explain.lookup('rejected_exceeds_inspected')['why'])
        self.assertTrue(by['not_on_sharepoint']['sure'])
        self.assertFalse(by['unusual_reject_rate']['sure'])
        ai = by['odd_pattern']                       # AI findings are never sure and keep their own wording
        self.assertFalse(ai['sure'])
        self.assertEqual((ai['why'], ai['fix']), ('The AI says why.', 'Look at row 20.'))
        self.assertEqual(ai['check'], 'Look at row 20.')

    def test_no_rows_found_is_explained(self):
        review = summarize(dict(filename='x.xlsx', findings=[], record_count=0))
        issue = next(i for i in review['issues'] if i['code'] == 'no_rows_found')
        self.assertTrue(issue['sure'])
        self.assertTrue(issue['why'] and issue['fix'])


class NewCheckTests(unittest.TestCase):
    NEW = ('duplicate_count_block', 'total_row_blank', 'defect_reject_mismatch')

    def test_clean_fixture_and_clean_rows_stay_quiet(self):
        result = inspect_spreadsheet('job.xlsx', eos_fixture.build())
        self.assertFalse(set(self.NEW) & {f['code'] for f in result['findings']})
        found, _ = common(varied_rows())
        self.assertFalse(set(self.NEW) & set(found))
        # a fixed lot size on every row is a normal report, not a copy-paste
        same = [['2026-10-07', '1', 'Robert Swanke', f'801080{8500 + i}', '80139189', 34, 30, 4, 'Holes', '4 holes'] for i in range(12)]
        self.assertFalse(set(self.NEW) & set(common(same)[0]))

    def test_copied_count_block(self):
        rows = varied_rows()
        for r in rows[3:9]:
            r[5:8], r[9] = [34, 28, 6], '6 holes'
        found, result = common(rows)
        self.assertEqual(found['duplicate_count_block'], 1)
        f = next(f for f in result['findings'] if f['code'] == 'duplicate_count_block')
        self.assertIn('Rows 5 to 10', f['message'])
        self.assertEqual((f['sheet'], f['row']), ('1st Shift', 5))
        for r in rows[3:9]:                               # only five alike is not enough
            r[5:8] = [34, 28, 6]
        rows[8][5:8] = [31, 28, 3]
        self.assertNotIn('duplicate_count_block', common(rows)[0])
        zeros = varied_rows()
        for r in zeros[3:11]:                             # zero rejects repeated proves nothing
            r[5:8], r[9], r[8] = [34, 34, 0], None, None
        self.assertNotIn('duplicate_count_block', common(zeros)[0])

    def test_blank_total(self):
        blank = {('1st Shift', f'{c}32'): None for c in 'CEG'}
        result = inspect_spreadsheet('job.xlsx', eos_fixture.build(overrides=blank))
        hits = [f for f in result['findings'] if f['code'] == 'total_row_blank']
        self.assertEqual([(f['sheet'], f['row'], f['column']) for f in hits], [('1st Shift', 31, 'C'), ('1st Shift', 31, 'E'), ('1st Shift', 31, 'G')])
        self.assertIn('Total Parts Inspected', hits[0]['message'])
        # a number beside the label, instead of under it, is fine
        beside = {('1st Shift', 'D31'): 438, ('1st Shift', 'C32'): None, ('1st Shift', 'E32'): None, ('1st Shift', 'G32'): None}
        result = inspect_spreadsheet('job.xlsx', eos_fixture.build(overrides=beside))
        self.assertEqual([f['column'] for f in result['findings'] if f['code'] == 'total_row_blank'], ['E', 'G'])

    def test_defect_reason_on_zero_rejects(self):
        rows = varied_rows()
        rows[4][5:8], rows[4][9] = [34, 34, 0], None       # reason "Holes" but nothing rejected
        found, result = common(rows)
        self.assertEqual(found['defect_reject_mismatch'], 1)
        f = next(f for f in result['findings'] if f['code'] == 'defect_reject_mismatch')
        self.assertEqual((f['sheet'], f['row'], f['column']), ('1st Shift', 6, 'I'))
        rows[4][8] = 'None'                                 # "None" is a correct entry
        self.assertNotIn('defect_reject_mismatch', common(rows)[0])
        always = varied_rows()
        for r in always:                                    # a column filled on every zero-reject row is not a reject reason
            r[5:8], r[9] = [34, 34, 0], None
        self.assertNotIn('defect_reject_mismatch', common(always)[0])

    def test_new_codes_are_explained_rated_and_categorised(self):
        from hosting.review_summary import severity
        for code in self.NEW:
            self.assertIn(code, emitted_codes())
            self.assertIsNotNone(explain.lookup(code))
            self.assertFalse(explain.lookup(code)['sure'])
            self.assertEqual(severity(code), 'medium')
            self.assertNotEqual(overview.category(dict(code=code)), 'Other checks')
        self.assertEqual(overview.category(dict(code='duplicate_count_block')), 'Duplicate VINs or rows')
        self.assertEqual(overview.category(dict(code='total_row_blank')), 'Totals that did not add up')
        self.assertEqual(overview.category(dict(code='defect_reject_mismatch')), 'Totals that did not add up')

    def test_checks_page_lists_the_new_checks(self):
        from hosting.check_catalog import catalog
        text = ' '.join(i for g in catalog() for i in g['items'])
        for phrase in ('Copied-down rows', 'Blank totals', 'Reasons with no rejects'):
            self.assertIn(phrase, text)


if __name__ == '__main__':
    unittest.main()

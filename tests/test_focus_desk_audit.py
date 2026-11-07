"""Regression tests for the Focus Desk audit (November 7): every result on the screen belongs to one check run,
suggestions are type-checked, versions have one lineage, and reviewer decisions are kept."""
import io
import tempfile
import unittest

from openpyxl import load_workbook

from hosting import delivery, registry
from hosting.fixes import FixError, apply_fixes, type_problem, _sources
from hosting.review_summary import check_run, summarize
from hosting.spreadsheet_inbox import SpreadsheetInbox
from tests.sl_daily_fixture import sl_daily

NAME = 'SL Alabama - 2214 - 2026-11-06.xlsx'
SNAP = [dict(client='SL Alabama', key='SLAlabama', sites=[dict(site='SLAlabama_AlexanderCity', location='Alexander City')],
             jobs=[dict(job='2214', numbers=['2214'], site='SLAlabama_AlexanderCity', list_id='l-2214')])]


class _Store:
    """The minimal store delivery.send needs, over a local inbox."""
    def __init__(self, root):
        self.root = root

    def session(self):
        import contextlib
        return contextlib.nullcontext(self.root)


def inbox():
    root = tempfile.mkdtemp()
    box = SpreadsheetInbox(root)
    box.set_setting(registry.SNAPSHOT_KEY, SNAP)
    return root, box


def reconciled(box, item, snapshot='snap-1', status='verified', rows=None):
    rec = dict(status=status, rows=rows or [], snapshot_id=snapshot, report_rows=5,
               bound_to=dict(file_hash=item['file_hash'], checker_version=item['checker_version']))
    return box._update(item['file_hash'], lambda r: r.update(source_reconciliation=rec))


class OneCheckRun(unittest.TestCase):
    """Finding 1: metrics, issues, AI explanations and approval all belong to the same report hash, snapshot and checker."""

    def test_ai_read_from_an_earlier_snapshot_is_not_used(self):
        _, box = inbox()
        item = reconciled(box, box.ingest(NAME, sl_daily()), 'snap-1')
        box.record_ai_review(item['file_hash'], dict(status='done', summary='Old read', issues=[dict(issue='9 records / 720 units', severity='high', sheet='Inspection Detail', row=4)]))
        fresh = box.get(item['file_hash'])
        self.assertTrue(any(i['source'] == 'AI review' for i in summarize(fresh)['issues']))
        reconciled(box, fresh, 'snap-2')                                     # a fresh reconciliation
        rv = summarize(box.get(item['file_hash']))
        self.assertEqual(rv['ai']['status'], 'stale')
        self.assertFalse(any(i['source'] == 'AI review' for i in rv['issues']))
        self.assertIsNone(rv['ai']['summary'])

    def test_approval_is_tied_to_the_check_run_and_sending_refuses_a_stale_one(self):
        root, box = inbox()
        item = reconciled(box, box.ingest(NAME, sl_daily()), 'snap-1')
        box.approve(item['file_hash'], 'Reviewer', note='checked', override=True)
        self.assertTrue(summarize(box.get(item['file_hash']))['approval_current'])
        reconciled(box, box.get(item['file_hash']), 'snap-2')               # SharePoint read again after approval
        self.assertFalse(summarize(box.get(item['file_hash']))['approval_current'])
        with self.assertRaises(ValueError) as e:
            delivery.send(_Store(root), item['file_hash'], dict(mode='draft', to=['nobody@example.com'], subject='x', body='x'))
        self.assertIn('Approve it again', str(e.exception))

    def test_a_partial_result_is_never_called_verified(self):
        _, box = inbox()
        item = reconciled(box, box.ingest(NAME, sl_daily()), status='incomplete')
        self.assertEqual(summarize(item)['sharepoint']['status'], 'incomplete')

    def test_the_old_day_level_comparison_never_reads_as_a_match(self):
        _, box = inbox()
        item = box.ingest(NAME, sl_daily())
        item = box._update(item['file_hash'], lambda r: r.update(sharepoint=dict(status='match', findings=[])))
        sp = summarize(item)['sharepoint']
        self.assertEqual(sp['status'], 'day_level')
        self.assertIn('not record by record', sp['message'])

    def test_opening_a_report_made_by_older_checks_changes_nothing(self):
        _, box = inbox()
        item = box.ingest(NAME, sl_daily())
        with box.connect() as db:
            import json
            for table in ('spreadsheets', 'spreadsheet_checks'):
                for row in db.execute(f'SELECT rowid AS rid, result_json FROM {table}').fetchall():
                    r = json.loads(row['result_json']); r['checker_version'] = 'old'
                    db.execute(f'UPDATE {table} SET result_json=? WHERE rowid=?', (json.dumps(r), row['rid']))
        opened = box.get(item['file_hash'])
        self.assertEqual((opened['checker_version'], opened['internal_check_status']), ('old', 'outdated_checks_require_recheck'))
        self.assertEqual(box.get(item['file_hash'])['checker_version'], 'old')   # still not reclassified


class TypedSuggestions(unittest.TestCase):
    """Finding 2: labels, headers and wrong types never become source-backed values."""

    def setUp(self):
        wb = load_workbook(io.BytesIO(sl_daily()))
        s = wb['Daily Inspection Summary']; s['A9'] = 'Date'; s['A10'] = 'Quantity inspected'
        buf = io.BytesIO(); wb.save(buf)
        self.raw = buf.getvalue()
        self.sources, _ = _sources(NAME, self.raw)

    def test_header_words_and_wrong_types_are_refused(self):
        self.assertIsNotNone(type_problem(self.sources, 'Daily Inspection Summary', 'B9', 'Shift'))
        self.assertIsNotNone(type_problem(self.sources, 'Daily Inspection Summary', 'B9', 'third shift'))
        self.assertIsNone(type_problem(self.sources, 'Daily Inspection Summary', 'B9', '2026-11-06'))
        self.assertIsNotNone(type_problem(self.sources, 'Daily Inspection Summary', 'B10', 'eighty'))
        self.assertIsNone(type_problem(self.sources, 'Daily Inspection Summary', 'B10', '400'))

    def test_spacing_only_clean_up_of_a_header_is_allowed(self):
        self.assertIsNone(type_problem(self.sources, 'Inspection Detail', 'D3', 'Shift', before=' Shift '))

    def test_a_machine_suggestion_with_the_wrong_type_cannot_be_applied(self):
        with self.assertRaises(FixError):
            apply_fixes(NAME, self.raw, [dict(sheet='Daily Inspection Summary', cell='B9', before='', after='Shift', source='Suggested')])
        apply_fixes(NAME, self.raw, [dict(sheet='Daily Inspection Summary', cell='B9', before='', after='2026-11-06', source='Suggested')])


class Identity(unittest.TestCase):
    """Finding 3: a row from another site or list is a blocker, whatever its item number."""

    def test_wrong_list_is_a_must_fix_issue(self):
        _, box = inbox()
        item = reconciled(box, box.ingest(NAME, sl_daily()), status='discrepancies',
                          rows=[dict(status='wrong_list', item='432', sheet='Inspection Detail', row=4, note='From another list.')])
        issue = next(i for i in summarize(item)['issues'] if i['code'] == 'sharepoint_wrong_list')
        self.assertEqual(issue['severity'], 'high')
        self.assertIn('different SharePoint site or list', issue['text'])


class VersionLineage(unittest.TestCase):
    """Finding 11: each real changed version has its own hash, revision number and provenance."""

    def test_revisions_count_up_and_repeating_the_same_changes_adds_nothing(self):
        _, box = inbox()
        v1 = box.ingest(NAME, sl_daily())
        change = [dict(sheet='Inspection Detail', cell='B4', before=box.sheet_grid(v1['file_hash'], 'Inspection Detail')['rows'][3][1], after='CHANGED-1', source='Your edit', kind='text')]
        v2 = box.apply_fixes(v1['file_hash'], change)
        self.assertEqual(v2['revision'], 2)
        self.assertEqual(v2['root_hash'], v1['file_hash'])
        self.assertEqual(v2['fixed_from']['file_hash'], v1['file_hash'])
        again = box.apply_fixes(v1['file_hash'], change)                     # the same changes once more
        self.assertTrue(again.get('already_applied'))
        self.assertEqual(again['file_hash'], v2['file_hash'])
        self.assertEqual(len(box.get(v1['file_hash'])['fixes_applied']), 1)  # recorded once
        v3 = box.apply_fixes(v2['file_hash'], [dict(change[0], before='CHANGED-1', after='CHANGED-2')])
        self.assertEqual((v3['revision'], v3['root_hash']), (3, v1['file_hash']))
        self.assertEqual(box.download(v1['file_hash'])[1], sl_daily())       # the original is untouched


class Decisions(unittest.TestCase):
    """Finding 12 and the review flow: notes, keep-as-recorded and reject are kept, logged and never clear a blocker."""

    def test_decisions_are_saved_logged_and_survive_a_recheck(self):
        _, box = inbox()
        item = box.ingest(NAME, sl_daily(nok_blank=True))
        box.record_decision(item['file_hash'], 'check:missing:1', 'keep', 'Rejects were not recorded on the tablet.', 'Reviewer')
        box.record_decision(item['file_hash'], 'fix:abc', 'reject', '', 'Reviewer')
        got = box.get(item['file_hash'])
        self.assertEqual(got['review_decisions']['check:missing:1']['action'], 'keep')
        self.assertEqual(len(got['review_decision_log']), 2)
        after = box.apply_recheck(item['file_hash'], 'Reviewer')
        self.assertEqual(set(after['review_decisions']), {'check:missing:1', 'fix:abc'})
        self.assertEqual(after['release_status'], 'held')
        with self.assertRaises(ValueError):
            box.record_decision(item['file_hash'], 'x', 'approve', '', 'Reviewer')


class BlankIsNotZero(unittest.TestCase):
    """Finding 5: a reject rate is unknown, never 0.0%, when rejects were not recorded."""

    def test_rate_inputs_stay_unknown(self):
        _, box = inbox()
        rv = summarize(box.ingest(NAME, sl_daily(nok_blank=True)))
        self.assertIsNone(rv['rejected'])
        self.assertEqual((rv['scans'], rv['inspected']), (5, 400))


if __name__ == '__main__':
    unittest.main()

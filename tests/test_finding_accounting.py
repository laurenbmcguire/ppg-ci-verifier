"""Every finding is accounted for, Claude's recommendations stay separate from a person's decisions, and a decision
can never move to a different record (November 8, second round)."""
import io
import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from unittest.mock import patch

from openpyxl import load_workbook

from tests.review_request_fixture import decide_displayed
from tests.review_request_fixture import apply_displayed
from hosting.review_summary import summarize
from hosting.spreadsheet_inbox import SpreadsheetInbox
from tests import sl2200_review_fixture as fx
from tests.eos_fixture import add_cached_values
from tests.sl_daily_fixture import records, sl_daily

REASON = 'Rework is not a field in the captured source.'


def setup(complete=True):
    root = tempfile.mkdtemp()
    box = SpreadsheetInbox(root)
    return root, box, fx.build(box, complete=complete)['file_hash']


def key_at(rv, cell, lists=('issues', 'dismissed')):
    return next(x['key'] for k in lists for x in rv[k] if str(x.get('cell')) == cell and x.get('code') == 'missing_cell')


class EveryFindingIsStored(unittest.TestCase):
    """Item 1: the checks store what they count; nothing is approved past a finding nobody can see."""

    def test_a_report_with_photo_links_stores_every_finding(self):
        wb = load_workbook(io.BytesIO(sl_daily(recs=records(101, inspected=47), job='2200')))
        d = wb['Inspection Detail']
        for r in range(4, 105):
            d.cell(r, 17).hyperlink = f'https://ppg.sharepoint.com/sites/SLAlabama_AlexanderCity/photo-{r}.jpg'
        buf = io.BytesIO(); wb.save(buf)
        item = SpreadsheetInbox(tempfile.mkdtemp()).ingest('SL Alabama - 2200 - 2026-11-06.xlsx', add_cached_values(buf.getvalue(), wb.sheetnames, {}))
        self.assertGreater(item['finding_count'], 100)                 # wholly empty columns are no longer flagged (Nov 9), so fewer findings
        self.assertEqual(len(item['findings']), item['finding_count'])
        self.assertFalse(item['findings_truncated'])
        self.assertEqual(sum(1 for f in item['findings'] if f['code'] == 'external_hyperlink'), item['finding_counts']['external_hyperlink'])
        cov = summarize(item)['coverage']
        self.assertEqual((cov['not_listed'], cov['truncated']), (0, False))

    def test_the_live_shape_holds_until_rechecked_and_the_complete_one_does_not(self):
        for complete, held in ((False, True), (True, False)):
            root, box, h = setup(complete)
            item = box.get(h); rv = summarize(item)
            self.assertEqual(any(x['code'] == 'findings_incomplete' for x in box.holds(item, rv)), held, complete)


class LargeAndPartial(unittest.TestCase):
    """Item 8 cases: a 614-row blank field is stored and counted in full; every source state reads the same everywhere."""

    def test_614_blank_fields_are_all_stored_and_counted(self):
        from hosting.spreadsheets import inspect_spreadsheet
        rows = ['Part number,Identifier,Inspected,OK,NOK'] + [f'80139189,LOT-{i:03d},10,10,' for i in range(614)] + ['80139189,LOT-999,10,10,0']   # NOK is in use, so its blanks are missing
        r = inspect_spreadsheet('HMMA 2236 2026-11-06.csv', ('\n'.join(rows) + '\n').encode())
        self.assertEqual(r['finding_counts'].get('missing_cell'), 614)
        self.assertEqual(len([f for f in r['findings'] if f['code'] == 'missing_cell']), 614)
        rv = summarize(dict(r, file_hash='0' * 64, filename='HMMA 2236 2026-11-06.csv'))
        self.assertEqual(sum(1 for i in rv['issues'] if i['code'] == 'missing_cell'), 614)
        self.assertEqual((rv['coverage']['not_listed'], rv['reconcile']['adds_up']), (0, True))

    def test_never_run_partial_stale_and_failed_reads_say_the_same_in_every_panel(self):
        from hosting import recommend
        root, box, h = setup()
        cases = {'not_checked': None, 'incomplete': dict(status='incomplete', rows=[], snapshot_id='s1', coverage=dict(notes=['Page 3 was throttled.'])),
                 'stale': dict(status='verified', rows=[], snapshot_id='s1', bound_to=dict(file_hash='f' * 64, checker_version='old'))}
        for want, rec in cases.items():
            def change(r, rec=rec):
                r.pop('source_reconciliation', None)
                if rec:
                    r['source_reconciliation'] = dict(rec, report_rows=r['record_count'], bound_to=rec.get('bound_to') or dict(file_hash=r['file_hash'], checker_version=r['checker_version']))
            box._update(h, change)
            item = box.get(h); rv = summarize(item)
            facts = recommend.report_facts(item, rv, 0)
            self.assertEqual(rv['sharepoint']['status'], want)
            self.assertEqual(facts['sharepoint']['status'], want)
            if want in ('not_checked', 'stale'):
                self.assertTrue([x for x in recommend.report_rules(facts) if x['do'] == 'recheck_sharepoint'], want)
            if want == 'incomplete':
                self.assertTrue(any(x['code'] == 'source_coverage_incomplete' for x in box.holds(item, rv)))


class EverythingAddsUp(unittest.TestCase):
    """Item 2: Claude's set-asides are visible with reasons; the total reconciles."""

    def test_the_total_reconciles_and_claudes_set_asides_are_listed_with_reasons(self):
        root, box, h = setup()
        rv = summarize(box.get(h))
        rc = rv['reconcile']
        self.assertTrue(rc['adds_up'], rc)
        self.assertEqual((rc['total'], rc['open'], rc['informational'], rc['kept_earlier_record'], rc['set_aside_by_claude']), (340, 54, 113, 25, 148))
        claude = [x for x in rv['dismissed'] if x['dismissed_by'] == 'claude_verify']
        cols = sorted({x['cell'][0] for x in claude})
        self.assertEqual(cols, ['L', 'M'])
        self.assertEqual(sum(1 for x in claude if x['cell'][0] == 'L'), 77)
        self.assertEqual(sum(1 for x in claude if x['cell'][0] == 'M'), 71)
        self.assertTrue(all(x['claude']['reason'] and not x.get('decision') for x in claude))   # a recommendation, not a decision
        earlier = [x for x in rv['dismissed'] if x['dismissed_by'] == 'person_legacy']
        self.assertEqual(len(earlier), 25)
        self.assertTrue(all(x['legacy_record']['reason'] for x in earlier))

    def test_a_person_can_reopen_or_agree_with_claude_and_it_still_adds_up(self):
        root, box, h = setup()
        rv = summarize(box.get(h))
        l5, l6 = key_at(rv, 'L5'), key_at(rv, 'L6')
        decide_displayed(box, h, ids=[l5], action='reopen', reason='Check this one myself.', by='Lauren', request_id='a')
        decide_displayed(box, h, ids=[l6], action='reject', reason='Agreed with Claude: rework is not recorded on this report.', by='Lauren', request_id='b')
        rv2 = summarize(box.get(h)); rc = rv2['reconcile']
        self.assertTrue(rc['adds_up'], rc)
        self.assertEqual((rc['open'], rc['set_aside_by_claude'], rc['kept_by_person']), (55, 146, 1))
        self.assertIn(l5, [x['key'] for x in rv2['issues']])
        self.assertEqual(next(x for x in rv2['dismissed'] if x['key'] == l6)['dismissed_by'], 'person')

    def test_a_cell_changed_in_a_later_version_counts_as_corrected_on_this_one(self):
        root, box, h = setup()
        new = apply_displayed(box, h, [dict(sheet='Inspection Detail', cell='M81', before='', after='0', reason='Source owner confirmed 0 NOK',
                                       source='Your edit', kind='text')])
        rv = summarize(box.get(h)); rc = rv['reconcile']
        self.assertTrue(rc['adds_up'], rc)
        self.assertEqual(rc['corrected'], 1)
        m81 = next(x for x in rv['dismissed'] if x.get('cell') == 'M81')
        self.assertEqual((m81['dismissed_by'], m81['corrected']['file_hash']), ('corrected', new['file_hash']))

    def test_ambiguous_earlier_records_get_a_fresh_review_and_are_never_rewritten(self):
        root, box, h = setup()
        old = [dict(code='missing_cell', sheet='Inspection Detail', row=n, text='Missing cell.', reason='Marked fine as is by a reviewer.', by_person=True)
               for n in range(81, 105)]
        box._update(h, lambda r: r.update(accepted_issues=(r.get('accepted_issues') or []) + old))
        before = json.dumps(box.get(h)['accepted_issues'], sort_keys=True)
        rv = summarize(box.get(h))
        ambiguous_l = [x for x in rv['issues'] if x.get('legacy_note') and x['cell'].startswith('L')]
        self.assertEqual(len(ambiguous_l), 24)
        self.assertTrue(all(x['legacy_records'] and x['legacy_records'][0]['reason'] == 'Marked fine as is by a reviewer.' for x in ambiguous_l))
        decide_displayed(box, h, ids=[x['key'] for x in ambiguous_l], action='reject', reason=REASON, by='Lauren', request_id='fresh')
        after = box.get(h)
        self.assertEqual(json.dumps(after['accepted_issues'], sort_keys=True), before)   # the earlier records stay exactly as they were
        rv2 = summarize(after)
        self.assertEqual(len([x for x in rv2['issues'] if x.get('legacy_note')]), 22)   # the M ones still wait for their own review
        self.assertTrue(rv2['reconcile']['adds_up'])


class DecisionsStayWithTheirRecord(unittest.TestCase):
    """Item 4: sorting, inserting rows, regenerating or new source data never moves a decision to another record."""

    def child(self, box, h):
        """A next version of the report (any saved change makes one; the parent's decisions go along, marked as carried)."""
        return apply_displayed(box, h, [dict(sheet='Inspection Detail', cell='H4', before='Bracket LH', after='Bracket LH ', reason='edit', source='Your edit', kind='text')])

    def rebuild(self, box, h, new_hash, order):
        """Give the new version the parent's findings with its rows re-ordered: row n now holds the record from order[n]."""
        parent = box.get(h)
        recs = {r['row']: r for r in parent['records']}
        def change(r):
            r['records'] = [dict(recs[old], row=new) for new, old in order.items()]
            moved = {old: new for new, old in order.items()}
            r['findings'] = [dict(f, row=moved.get(f['row'], f['row'])) for f in parent['findings']]
            for k in ('accepted_issues', 'ai_verify', 'ai_review'):
                r.pop(k, None)
        box._update(new_hash, change)

    def test_sorting_rows_moves_the_decision_with_its_record(self):
        root, box, h = setup()
        rv = summarize(box.get(h))
        l81 = key_at(rv, 'L81'); item81 = next(x for x in rv['issues'] if x['key'] == l81)['record_key']
        decide_displayed(box, h, ids=[l81], action='reject', reason=REASON, by='Lauren', request_id='a')
        new = self.child(box, h)['file_hash']
        order = {r: r for r in range(4, 105)}; order[81], order[90] = 90, 81       # rows 81 and 90 swapped
        self.rebuild(box, h, new, order)
        rv2 = summarize(box.get(new))
        kept = [x for x in rv2['dismissed'] if x.get('decision')]
        self.assertEqual([(x['record_key'], x['cell']) for x in kept], [(item81, 'L90')])   # followed the record to row 90
        self.assertIn('L81', [x['cell'] for x in rv2['issues']])                           # the record now on row 81 is still open

    def test_inserting_a_row_shifts_rows_without_moving_decisions(self):
        root, box, h = setup()
        rv = summarize(box.get(h))
        ids = fx.l_ids(rv)
        decide_displayed(box, h, ids=ids, action='reject', reason=REASON, by='Lauren', request_id='a')
        new = self.child(box, h)['file_hash']
        self.rebuild(box, h, new, {(r + 1 if r >= 50 else r): r for r in range(4, 105)})   # a row inserted at 50
        rv2 = summarize(box.get(new))
        self.assertEqual(sorted(x['cell'] for x in rv2['dismissed'] if x.get('decision')), sorted(f'L{r}' for r in range(82, 106)))
        self.assertEqual(len([x for x in rv2['issues'] if x['code'] == 'missing_cell' and x['cell'][0] == 'M']), 95)

    def test_a_finding_without_a_verified_record_is_not_carried_by_its_coordinates(self):
        root, box, h = setup()
        def unlink(r):   # the same report without SharePoint item links on its rows
            for rec in r['records']:
                rec['source'] = {}; rec['identifier'] = None; rec['part'] = None
        box._update(h, unlink)
        rv = summarize(box.get(h))
        l81 = key_at(rv, 'L81')
        self.assertIsNone(next(x for x in rv['issues'] if x['key'] == l81)['record_key'])
        decide_displayed(box, h, ids=[l81], action='reject', reason=REASON, by='Lauren', request_id='a')
        self.assertEqual(summarize(box.get(h))['decisions']['rejected'], 1)               # on its own version it holds
        new = self.child(box, h)['file_hash']
        box._update(new, lambda r: (r.update(findings=box.get(h)['findings'], records=box.get(h)['records']), r.pop('ai_verify', None), r.pop('accepted_issues', None)))
        rv2 = summarize(box.get(new))
        self.assertEqual(rv2['decisions']['rejected'], 0)
        prev = next(x for x in rv2['issues'] if x.get('cell') == 'L81')['previous_decision']
        self.assertIn('not tied to the same verified record', prev['note'])

    def test_regenerating_the_report_starts_with_no_decisions(self):
        root, box, h = setup()
        decide_displayed(box, h, ids=fx.l_ids(summarize(box.get(h))), action='reject', reason=REASON, by='Lauren', request_id='a')
        again = fx.build(box, name='SL Alabama - 2200 - 2026-11-06 (resent).xlsx', complete=True, regenerated=True)
        self.assertNotEqual(again['file_hash'], h)
        self.assertEqual(summarize(again)['decisions']['rejected'], 0)

    def test_new_source_data_unsettles_a_decision_on_a_source_difference(self):
        root, box, h = setup()
        def rec(value):
            def change(r):
                r['source_reconciliation'] = dict(status='discrepancies', report_rows=r['record_count'], snapshot_id='snap-' + value, captured_at='2026-11-08T15:00:00Z',
                                                  rows=[dict(status='value_differs', item='6190', field='rejected', report=None, sharepoint=value,
                                                             sheet='Inspection Detail', row=19)],
                                                  bound_to=dict(file_hash=r['file_hash'], checker_version=r['checker_version']))
            box._update(h, change)
        rec('1')
        rv = summarize(box.get(h))
        diff = next(x for x in rv['issues'] if x['code'] == 'sharepoint_value_differs')
        decide_displayed(box, h, ids=[diff['key']], action='defer', reason='Owner to confirm', owner='List owner', request_id='a')
        self.assertEqual(summarize(box.get(h))['decisions']['deferred'], 1)
        rec('0')                                                                   # SharePoint now says something else
        rv2 = summarize(box.get(h))
        self.assertEqual(rv2['decisions']['deferred'], 0)
        self.assertEqual(rv2['decisions']['stale'], 1)


class Paged(unittest.TestCase):
    """Item 1: the list arrives in pages; counts, holds and approval always use all of it."""

    def setUp(self):
        from api.index import handler
        sys.path.insert(0, os.path.dirname(__file__))
        from legacy_storage_fake import PostgresWorkspace
        from test_vercel import FakeDatabase
        self.env = patch.dict(os.environ, dict(CI_LOCAL_DEV='1', VERCEL='0', CI_REVIEW_PAGE='25')); self.env.start()
        os.environ.pop('ANTHROPIC_API_KEY', None)
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), handler)
        self.server.workspace_store = PostgresWorkspace('postgresql://test.invalid/x', FakeDatabase().connect)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f'http://127.0.0.1:{self.server.server_port}'
        with self.server.workspace_store.session() as directory:
            self.h = fx.build(SpreadsheetInbox(directory), complete=True)['file_hash']

    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.env.stop()

    def req(self, path, body=None):
        r = urllib.request.Request(self.base + path, data=None if body is None else json.dumps(body).encode(),
                                   headers={'Content-Type': 'application/json', 'Origin': self.base, 'X-Review-Token': 'same-origin'})
        try:
            with urllib.request.urlopen(r, timeout=30) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_pages_add_up_to_the_full_list_and_off_page_findings_still_hold_approval(self):
        st, view = self.req('/api/spreadsheet?id=' + self.h)
        rv = view['review']
        self.assertEqual((len(rv['issues']), rv['page']['issues_total']), (25, 167))
        self.assertEqual(rv['counts']['medium'], 53)                             # counts come from every finding
        got = list(rv['issues'])
        while len(got) < rv['page']['issues_total']:
            st, page = self.req('/api/spreadsheets/findings', dict(file_hash=self.h, list='issues', offset=len(got), limit=25))
            got += page['items']
        self.assertEqual(len({x['key'] for x in got}), 167)
        last = got[-1]['key']                                                     # far off the first page
        clip = next(x['key'] for x in got if x['severity'] == 'high')
        st, out = self.req('/api/spreadsheets/decide', dict(file_hash=self.h, ids=[clip], action='defer', reason='Owner to confirm', owner='List owner',
                                                             request_id='p1', expected_rev=0, expected_run=rv['run'], expected_fps={x['key']:x['fp'] for x in got}))
        self.assertEqual(st, 200, out)
        st, appr = self.req('/api/spreadsheets/approve', dict(file_hash=self.h, approver='Lauren', note='override', override=True))
        self.assertNotEqual(st, 200)
        self.assertTrue(last)


if __name__ == '__main__':
    unittest.main()

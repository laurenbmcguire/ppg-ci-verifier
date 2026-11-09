"""Review decisions are safe and durable (November 8 regression).

The reviewer marked only the 24 blank rework cells L81:L104 "fine as is". After a reload the 24 untouched NOK
findings on the same rows had shrunk to 2: the old record kept code, sheet, row and message but not the cell, and
both columns say "Missing cell." on every row. These tests reproduce that sequence on an isolated fixture and pin
the fix: decisions are keyed by a stable finding ID, are distinct (reject / defer / confirm / undo), are logged,
are idempotent, refuse stale or partial batches, and never change the workbook.
"""
import hashlib
import io
import json
import os
import random
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from unittest.mock import patch

from tests.review_request_fixture import decide_displayed
from tests.review_request_fixture import apply_displayed
from hosting import review_decisions
from hosting.review_decisions import DecisionConflict, DecisionError
from hosting.review_summary import summarize
from hosting.spreadsheet_inbox import SpreadsheetInbox
from tests import sl2200_review_fixture as fx

REASON = 'Rework is not a field in the captured source; the blank is preserved as not recorded.'


def setup():
    root = tempfile.mkdtemp()
    box = SpreadsheetInbox(root)
    item = fx.build(box)
    return root, box, item['file_hash']


def counts(rv):
    c = rv['counts']
    return c['high'], c['medium'] + c['low'], c['info'], len(rv['issues'])


class TheRealSequence(unittest.TestCase):
    """Acceptance test 1: 58 -> reject only L81:L104 -> 34, including all 24 NOK findings, everywhere and always."""

    def test_rejecting_the_rework_group_leaves_exactly_34_and_every_nok_finding(self):
        root, box, h = setup()
        rv = summarize(box.get(h))
        self.assertEqual(counts(rv), (1, 53, 4, 58))
        ids = fx.l_ids(rv)
        self.assertEqual(len(ids), 24)
        nok_before = fx.m_open(rv)
        self.assertEqual(len(nok_before), 24)
        _, entry = decide_displayed(box, h, ids=ids, action='reject', reason=REASON, by='Lauren', request_id='r-1', expected_rev=0)
        self.assertEqual(entry['cells'][ids[0]], 'Inspection Detail L81')
        self.assertEqual(sorted(entry['cells'].values())[-1], 'Inspection Detail L99')
        for when in ('now', 'reload', 'new session'):
            item = (box if when == 'now' else SpreadsheetInbox(root)).get(h)
            rv = summarize(item)
            self.assertEqual(counts(rv), (1, 29, 4, 34), when)
            self.assertEqual(fx.m_open(rv), nok_before, when)                  # all 24 NOK findings untouched
            self.assertEqual(rv['decisions']['rejected'], 24, when)
            self.assertEqual(len(item['finding_decisions']), 24, when)
            self.assertEqual(sum(1 for x in rv['dismissed'] if x.get('decision')), 24, when)
        # grouping, sorting or filtering the list can never change which findings are decided
        open_keys = {x['key'] for x in rv['issues']}
        shuffled = list(rv['issues']); random.Random(4).shuffle(shuffled)
        self.assertEqual({x['key'] for x in shuffled if x['severity'] != 'info'} | {x['key'] for x in rv['issues'] if x['severity'] == 'info'}, open_keys)
        self.assertFalse(open_keys & set(ids))

    def test_ids_do_not_depend_on_the_order_findings_are_listed_in(self):
        root, box, h = setup()
        ids = fx.l_ids(summarize(box.get(h)))
        decide_displayed(box, h, ids=ids, action='reject', reason=REASON, by='L', request_id='r-1')
        def reorder(r):   # a recheck that lists the same findings in another order (or with one added in front)
            fs = list(r['findings']); random.Random(7).shuffle(fs)
            r['findings'] = [dict(code='extra_spaces', sheet='Inspection Detail', row=2, column=3)] + fs
        box._update(h, reorder)
        rv = summarize(box.get(h))
        self.assertEqual(rv['decisions']['rejected'], 24)
        self.assertEqual(len(fx.m_open(rv)), 24)
        self.assertEqual(fx.l_ids(rv), [])

    def test_old_fine_records_without_a_cell_never_settle_the_other_column(self):
        """What the live report has saved: 24 "fine as is" records with code/sheet/row/message and no cell."""
        root, box, h = setup()
        def legacy(r):
            r['accepted_issues'] = (r.get('accepted_issues') or []) + [dict(code='missing_cell', sheet='Inspection Detail', row=n, text='Missing cell.',
                                                                            reason='Marked fine as is by a reviewer.', by_person=True) for n in range(81, 105)]
        box._update(h, legacy)
        rv = summarize(box.get(h))
        self.assertEqual(len(fx.m_open(rv)), 24)            # the old matcher dropped 22 of these
        notes = [x for x in rv['issues'] if x.get('legacy_note')]
        self.assertEqual(len(notes), 46)                    # 22 rows have both L and M blank: ambiguous, so neither is settled
        self.assertIn('did not bind the exact finding', notes[0]['legacy_note'])
        # Even an unambiguous coordinate cannot settle data without observed-value/run binding.
        self.assertIn('L88', [x['cell'] for x in rv['issues']])
        # the 25 earlier part-number decisions (one finding per row) are kept
        self.assertFalse([x for x in rv['issues'] if x['code'] == 'part_format_differs'])

    def test_legacy_cell_address_without_value_binding_does_not_dismiss(self):
        rec = dict(code='missing_cell', sheet='Inspection Detail', row=81, cell='L81', text='Missing cell.', by_person=True, reason='fine')
        root, box, h = setup()
        box._update(h, lambda r: r.update(accepted_issues=(r.get('accepted_issues') or []) + [rec]))
        rv = summarize(box.get(h))
        self.assertIn('L81', [x['cell'] for x in rv['issues']])
        self.assertIn(81, fx.m_open(rv))


class DistinctActions(unittest.TestCase):
    """Acceptance test 2: one, a subset, a whole group, undo, defer; exact IDs, reasons and counters."""

    def setUp(self):
        self.root, self.box, self.h = setup()
        self.rv = summarize(self.box.get(self.h))
        self.L = fx.l_ids(self.rv)

    def decide(self, **kw):
        kw.setdefault('request_id', 'r-' + os.urandom(4).hex())
        kw.setdefault('by', 'Lauren')
        return decide_displayed(self.box, self.h, **kw)

    def test_reject_one_then_a_subset_then_undo_one(self):
        self.decide(ids=self.L[:1], action='reject', reason=REASON)
        self.assertEqual(len(summarize(self.box.get(self.h))['issues']), 57)
        _, sub = self.decide(ids=self.L[1:5], action='reject', reason='Same: rework not recorded.')
        rv = summarize(self.box.get(self.h))
        self.assertEqual((len(rv['issues']), rv['decisions']['rejected']), (53, 5))
        self.decide(action='undo', undo_of=sub['decision_id'], reason='Rows 82-85 need a second look.')
        item = self.box.get(self.h); rv = summarize(item)
        self.assertEqual((len(rv['issues']), rv['decisions']['rejected']), (57, 1))
        log = item['finding_decision_log']
        self.assertEqual([e['action'] for e in log], ['reject', 'reject', 'undo'])
        self.assertEqual(log[2]['undo_of'], sub['decision_id'])
        self.assertEqual(log[1]['ids'], self.L[1:5])
        self.assertTrue(all(e['by'] == 'Lauren' and e['at'] for e in log))
        with self.assertRaises(DecisionConflict):           # an undo is undone once
            self.decide(action='undo', undo_of=sub['decision_id'])

    def test_a_reason_is_required_and_a_blocker_needs_evidence(self):
        with self.assertRaises(DecisionError):
            self.decide(ids=self.L, action='reject', reason='')
        clip = next(x['key'] for x in self.rv['issues'] if x['severity'] == 'high')
        with self.assertRaises(DecisionError):
            self.decide(ids=[clip], action='reject', reason='Looks fine')
        with self.assertRaises(DecisionError):
            self.decide(ids=[], action='reject', reason='x')
        self.assertEqual(summarize(self.box.get(self.h))['decisions']['rejected'], 0)

    def test_deferring_the_broken_clip_keeps_it_open_and_blocks_approval(self):
        clip = next(x['key'] for x in self.rv['issues'] if x['severity'] == 'high')
        self.decide(ids=[clip], action='defer', owner='SL Alabama source owner',
                    reason='Comment "1 broken clip" but OK 47 of 47, NOK blank, Condition OK; the captured source has the same conflict.',
                    evidence=['SharePoint item 6190', 'Inspection Detail row 19'])
        item = self.box.get(self.h); rv = summarize(item)
        self.assertEqual(rv['counts']['high'], 1)            # still a blocker
        self.assertEqual(rv['decisions']['deferred'], 1)
        held = next(x for x in rv['issues'] if x['key'] == clip)
        self.assertEqual(held['decision']['owner'], 'SL Alabama source owner')
        holds = self.box.holds(item, rv)
        self.assertTrue(any(x['code'] == 'awaiting_source_owner' and x['hard'] for x in holds))
        with self.assertRaises(ValueError):                  # "Approve anyway" cannot get past a question to the source owner
            self.box.approve(self.h, 'Lauren', note='override', override=True)
        self.assertNotIn('approval', self.box.get(self.h))
        # the blank NOK cell on that row is a separate finding with its own decision
        self.assertIn(19, fx.m_open(rv))

    def test_a_mixed_batch_records_each_exact_cell(self):
        mixed = [self.L[0], next(x['key'] for x in self.rv['issues'] if str(x.get('cell')) == 'M81')]
        _, e = self.decide(ids=mixed, action='confirm')
        self.assertEqual(sorted(e['cells'].values()), ['Inspection Detail L81', 'Inspection Detail M81'])
        rv = summarize(self.box.get(self.h))
        self.assertEqual(len(rv['issues']), 58)               # confirmed findings stay open until they are corrected
        self.assertEqual(rv['decisions']['confirmed'], 2)

    def test_compound_findings_on_one_row_are_decided_separately(self):
        supplier = next(x['key'] for x in self.rv['issues'] if x['code'] == 'missing_info')
        auditor = next(x['key'] for x in self.rv['issues'] if x['code'] == 'unusual_auditor_id')
        self.assertNotEqual(supplier, auditor)
        self.decide(ids=[auditor], action='reject', reason='High lookup IDs are normal for new auditors.')
        rv = summarize(self.box.get(self.h))
        self.assertIn(supplier, [x['key'] for x in rv['issues']])


class SafeUnderFailure(unittest.TestCase):
    """Acceptance test 4: double clicks, lost responses, stale tabs, partial batches."""

    def setUp(self):
        self.root, self.box, self.h = setup()
        self.L = fx.l_ids(summarize(self.box.get(self.h)))

    def test_the_same_request_twice_is_recorded_once(self):
        decide_displayed(self.box, self.h, ids=self.L, action='reject', reason=REASON, request_id='same', expected_rev=0)
        _, e = decide_displayed(self.box, self.h, ids=self.L, action='reject', reason=REASON, request_id='same', expected_rev=0)
        self.assertTrue(e['replayed'])
        item = self.box.get(self.h)
        self.assertEqual(len(item['finding_decision_log']), 1)
        self.assertEqual(item['finding_decisions_rev'], 1)

    def test_a_stale_tab_is_refused_and_nothing_is_saved(self):
        decide_displayed(self.box, self.h, ids=self.L[:2], action='reject', reason=REASON, request_id='tab-a', expected_rev=0)
        with self.assertRaises(DecisionConflict) as ctx:
            decide_displayed(self.box, self.h, ids=self.L[2:4], action='reject', reason=REASON, request_id='tab-b', expected_rev=0)
        self.assertEqual(ctx.exception.detail['current_rev'], 1)
        self.assertEqual(summarize(self.box.get(self.h))['decisions']['rejected'], 2)

    def test_one_unknown_finding_refuses_the_whole_batch(self):
        with self.assertRaises(DecisionConflict) as ctx:
            decide_displayed(self.box, self.h, ids=self.L + ['check|missing_cell|Inspection Detail|Z999'], action='reject', reason=REASON, request_id='p')
        self.assertEqual(ctx.exception.detail['missing'], ['check|missing_cell|Inspection Detail|Z999'])
        item = self.box.get(self.h)
        self.assertFalse(item.get('finding_decisions'))
        self.assertNotIn('p', item.get('finding_requests') or {})

    def test_an_approved_or_sent_version_refuses_changes(self):
        self.box._update(self.h, lambda r: r.update(approval=dict(by='X', at='t')))
        with self.assertRaises(DecisionConflict):
            decide_displayed(self.box, self.h, ids=self.L, action='reject', reason=REASON, request_id='a')

    def test_a_decision_is_not_carried_onto_a_finding_that_now_says_something_else(self):
        decide_displayed(self.box, self.h, ids=self.L[:1], action='reject', reason=REASON, request_id='a')
        def changed(r):   # a recheck now reads a different message for L81
            for f in r['findings']:
                if f['code'] == 'missing_cell' and f['row'] == 81 and f['column'] == fx.L:
                    f['message'] = 'Rework is blank but OK is less than inspected.'
        self.box._update(self.h, changed)
        rv = summarize(self.box.get(self.h))
        self.assertEqual(rv['decisions']['rejected'], 0)
        self.assertEqual(rv['decisions']['stale'], 1)
        self.assertIn('L81', [x['cell'] for x in rv['issues']])
        self.assertIn('previous_decision', next(x for x in rv['issues'] if x['cell'] == 'L81'))


class NothingIsInvented(unittest.TestCase):
    """Acceptance tests 5 and 10: unknowns stay unknown and decisions never touch the workbook."""

    def test_decisions_leave_the_workbook_bytes_and_unknown_totals_alone(self):
        root, box, h = setup()
        before = box.get(h)
        _, raw0 = box.original_bytes(h)
        L = fx.l_ids(summarize(before))
        decide_displayed(box, h, ids=L, action='reject', reason=REASON, request_id='a')
        _, raw1 = box.original_bytes(h)
        self.assertEqual(hashlib.sha256(raw0).hexdigest(), hashlib.sha256(raw1).hexdigest())
        dl = box.download(h)
        data = dl[1] if isinstance(dl, tuple) else dl
        self.assertEqual(hashlib.sha256(data if isinstance(data, bytes) else raw1).hexdigest(), h)
        after = box.get(h)
        self.assertEqual(after['totals'], before['totals'])
        for d in after['finding_decisions'].values():
            self.assertIsNone(d['value'])                      # a blank stays blank (unknown), never 0
        self.assertEqual(summarize(after)['rejected'], summarize(before)['rejected'])


class OneTruthEverywhere(unittest.TestCase):
    """Acceptance tests 8 and 11: coverage limits are shown, panels agree, a held report is not sent."""

    def test_unstored_findings_are_counted_and_hold_the_report(self):
        root, box, h = setup()
        item = box.get(h); rv = summarize(item)
        cov = rv['coverage']
        self.assertEqual((cov['findings_total'], cov['findings_listed'], cov['not_listed'], cov['truncated']), (339, 230, 109, True))
        self.assertEqual(cov['by_code']['external_hyperlink'], dict(total=109, listed=0))
        self.assertTrue(any(x['code'] == 'findings_incomplete' and x['hard'] for x in box.holds(item, rv)))
        with self.assertRaises(ValueError):
            box.approve(h, 'Lauren', note='override', override=True)

    def test_recommendations_use_the_same_source_verdict_as_the_header(self):
        from hosting import recommend
        from tests.test_focus_desk_audit import inbox, approval_fixture
        root, box = inbox()
        item = approval_fixture(box)
        h = item['file_hash']
        box._update(h, lambda r: r['source_reconciliation'].update(captured_at='2026-11-08T14:00:00Z'))
        item = box.get(h); rv = summarize(item)
        self.assertEqual(rv['sharepoint']['status'], 'match')
        facts = recommend.report_facts(item, rv, 0)
        self.assertEqual(facts['sharepoint']['status'], 'match')
        self.assertEqual(facts['sharepoint']['captured_at'], '2026-11-08T14:00:00Z')
        self.assertFalse([x for x in recommend.report_rules(facts) if x['do'] == 'recheck_sharepoint'])
        # A success label without count evidence must not disagree with the header.
        box._update(h, lambda r: r['source_reconciliation'].pop('count_reconciliation'))
        item = box.get(h); rv = summarize(item)
        self.assertEqual(rv['sharepoint']['status'], 'unverified')
        self.assertEqual(recommend.report_facts(item, rv, 0)['sharepoint']['status'], 'unverified')

    def test_a_held_report_cannot_be_approved_or_sent(self):
        from hosting import delivery
        root, box, h = setup()
        clip = next(x['key'] for x in summarize(box.get(h))['issues'] if x['severity'] == 'high')
        decide_displayed(box, h, ids=[clip], action='defer', reason='Waiting for the list owner.', owner='List owner', request_id='d')
        with self.assertRaises(ValueError):
            box.approve(h, 'Lauren', note='ship it', override=True)

        class Store:
            def session(self_inner):
                import contextlib
                return contextlib.nullcontext(root)
        with patch('hosting.sharepoint._open', side_effect=AssertionError('nothing may be sent')), self.assertRaises(ValueError):
            delivery.send(Store(), h, dict(mode='send', to=['nobody@example.com'], subject='x', body='x'))

    def test_decisions_go_to_a_new_version_only_where_the_finding_is_unchanged(self):
        root, box, h = setup()
        rv = summarize(box.get(h))
        decide_displayed(box, h, ids=fx.l_ids(rv), action='reject', reason=REASON, request_id='a')
        new = apply_displayed(box, h, [dict(sheet='Inspection Detail', cell='F10', before='LOT-07', after='LOT-07A', reason='typed', source='Your edit', kind='text')])
        item = box.get(new['file_hash'])
        carried = item.get('finding_decisions') or {}
        self.assertEqual(len(carried), 24)
        self.assertTrue(all(d['carried_from'] == h for d in carried.values()))
        self.assertTrue(box.get(h)['finding_decision_log'])                   # the history stays with the version it was made on
        self.assertFalse(item.get('finding_decision_log'))


class OverHttp(unittest.TestCase):
    """The review screen's own requests: one answer carries the whole report state, conflicts are 409."""

    def setUp(self):
        from api.index import handler
        sys.path.insert(0, os.path.dirname(__file__))
        from legacy_storage_fake import PostgresWorkspace
        from test_vercel import FakeDatabase
        self.env = patch.dict(os.environ, dict(CI_LOCAL_DEV='1', VERCEL='0')); self.env.start()
        os.environ.pop('ANTHROPIC_API_KEY', None)
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), handler)
        self.server.workspace_store = PostgresWorkspace('postgresql://test.invalid/x', FakeDatabase().connect)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f'http://127.0.0.1:{self.server.server_port}'
        with self.server.workspace_store.session() as directory:
            self.h = fx.build(SpreadsheetInbox(directory))['file_hash']

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

    def test_decide_returns_the_same_state_a_reload_shows(self):
        st, view = self.req('/api/spreadsheet?id=' + self.h)
        self.assertEqual(st, 200)
        L = fx.l_ids(view['review'])
        st, out = self.req('/api/spreadsheets/decide', dict(file_hash=self.h, ids=L, action='reject', reason=REASON, request_id='h-1', expected_rev=0, expected_run=view['review']['run'], expected_fps={x['key']:x['fp'] for x in view['review']['issues']}))
        self.assertEqual(st, 200, out)
        st, reload = self.req('/api/spreadsheet?id=' + self.h)
        for rv in (out['report']['review'], reload['review']):
            self.assertEqual((rv['counts']['high'], rv['counts']['medium'], rv['counts']['info']), (1, 29, 4))
            self.assertEqual(len(fx.m_open(rv)), 24)
            self.assertEqual(rv['decisions']['rev'], 1)
        st, again = self.req('/api/spreadsheets/decide', dict(file_hash=self.h, ids=L, action='reject', reason=REASON, request_id='h-1', expected_rev=0, expected_run=view['review']['run'], expected_fps={x['key']:x['fp'] for x in view['review']['issues']}))
        self.assertEqual(st, 200); self.assertTrue(again['entry']['replayed'])
        st, stale = self.req('/api/spreadsheets/decide', dict(file_hash=self.h, ids=L[:1], action='confirm', request_id='h-2', expected_rev=0,
                                                               **fx.shown(view['review'], L[:1])))
        self.assertEqual(st, 409); self.assertEqual(stale['current_rev'], 1)
        st, known = self.req('/api/spreadsheets/decision-status', dict(file_hash=self.h, request_id='h-1'))
        self.assertEqual((st, known['saved']), (200, True))
        st, unknown = self.req('/api/spreadsheets/decision-status', dict(file_hash=self.h, request_id='never-sent'))
        self.assertFalse(unknown['saved'])


if __name__ == '__main__':
    unittest.main()

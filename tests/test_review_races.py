"""Independent review, Nov 8: three ways a decision, an approval or an automatic fix could act on something nobody saw.

1. A stale review click: the reviewer saw SharePoint say 72; SharePoint was read again (75) before the click arrived.
   The decision must be refused, not attached to the new value.
2. Approval and the holds it depends on are checked inside the same locked save: a hold that lands while the approval
   is on its way refuses it; a decision that changes after approval withdraws it; delivery checks again.
3. Automatic and AI writes fail closed before mutation. Explicitly reviewed manual corrections retain the source
   read, item and field, preserve human decisions, and check source freshness under the writer lock before creating
   a corrected version. Delivery and its internal reservation/recording entry points also remain paused.

Each race is made deterministic with a hook that runs the competing write just before the request takes the report's
lock (that is where the old code had already made its checks).
"""
import contextlib
import io
import json
import os
import sys
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from unittest.mock import patch

from openpyxl import load_workbook

from hosting import autofix, delivery, review_decisions
from hosting.fixes import FixError
from hosting.review_summary import approval_current, summarize
from hosting.spreadsheet_inbox import SpreadsheetInbox
from tests import sl2200_review_fixture as fx
from tests.sl_daily_fixture import sl_daily
from tests.test_focus_desk_audit import NAME, _Store, inbox, reconciled, verified_fixture, approval_fixture

REASON = 'Rework is not recorded on this report.'


def shown(review, ids):
    return dict(expected_run=review['run'], expected_fps={x['key']:x['fp'] for x in review['issues'] + review['dismissed'] if x['key'] in ids})


def decide_shown(box, file_hash, **kwargs):
    displayed = box.get(file_hash)
    review = summarize(displayed)
    kwargs.setdefault('expected_rev', int(displayed.get('finding_decisions_rev') or 0))
    kwargs.setdefault('expected_run', review['run'])
    kwargs.setdefault('expected_fps', {x['key']:x['fp'] for x in review['issues'] + review['dismissed']})
    return box.decide_findings(file_hash, **kwargs)


def approve_shown(box, file_hash, approver, **kwargs):
    displayed = box.get(file_hash)
    return box.approve(file_hash, approver, expected_check_hash=displayed['check_hash'], expected_run=summarize(displayed)['run'], **kwargs)



def sp_row(value):
    return dict(status='value_differs', item='433', field='inspected', report='72', sharepoint=str(value), sheet='Inspection Detail', row=5)


def sp_report(box, value=80, snapshot='snap-1'):
    """Item 433 says 72 inspected in the report; SharePoint says ``value``. J5 is the cell."""
    wb = load_workbook(io.BytesIO(sl_daily(nok_blank=False)))
    d = wb['Inspection Detail']; d['H6'] = '  ' + str(d['H6'].value) + ' '; d['J5'] = 72
    buf = io.BytesIO(); wb.save(buf)
    item = box.ingest(NAME, buf.getvalue())
    return reread(box, item['file_hash'], value, snapshot)


def reread(box, h, value, snapshot):
    """SharePoint read again: a new snapshot, and the value it now has for item 433."""
    item = box.get(h)
    rec = dict(status='discrepancies', rows=[sp_row(value)], snapshot_id=snapshot, report_rows=5,
               site_id='site-1', list_id='list-2214', mapping_binding=box.source_mapping('site-1','list-2214'),
               proposed_corrections=[dict(identity_verified=True, item='433', sheet='Inspection Detail', row=5, column=10, field='inspected', before='72', after=str(value))],
               bound_to=dict(file_hash=h, checker_version=item['checker_version']))
    return box._update(h, lambda r: r.update(source_reconciliation=rec))


def sp_finding(box, h):
    rv = summarize(box.get(h))
    return next(x for x in rv['issues'] + rv['dismissed'] if x.get('source') == 'SharePoint' and x.get('cell') == 'J5'), rv


@contextlib.contextmanager
def just_before_the_lock(hook, times=1):
    """Run ``hook`` once, right before the next request takes a report's lock (after any reads it made first)."""
    real, left = SpreadsheetInbox._begin, [times]
    def begin(self, db, file_hash):
        if left[0] > 0:
            left[0] -= 1
            hook()
        return real(self, db, file_hash)
    with patch.object(SpreadsheetInbox, '_begin', begin):
        yield


class StaleReviewClick(unittest.TestCase):
    """Issue 1: the decision is bound to the fingerprint and check run the reviewer saw, checked under the lock."""

    def setUp(self):
        self.root, self.box = inbox()
        self.h = sp_report(self.box, 72 + 8)['file_hash']   # report 72, SharePoint 80 at first

    def test_a_sharepoint_value_that_changed_before_the_click_refuses_the_decision(self):
        x, rv = sp_finding(self.box, self.h)
        seen, run = {x['key']: x['fp']}, rv['run']                         # what the reviewer's screen showed
        reread(self.box, self.h, 75, 'snap-2')                              # SharePoint now says 75
        with self.assertRaises(review_decisions.DecisionConflict):
            decide_shown(self.box, self.h, ids=[x['key']], action='defer', reason='Owner to confirm', owner='List owner',
                                     request_id='stale', expected_fps=seen, expected_run=run)
        self.assertFalse(self.box.get(self.h).get('finding_decisions'))
        now, _ = sp_finding(self.box, self.h)
        self.assertEqual(now['source_value'], '75')
        self.assertIsNone(now.get('decision'))

    def test_the_same_change_landing_inside_the_request_is_caught_under_the_lock(self):
        x, rv = sp_finding(self.box, self.h)
        other = SpreadsheetInbox(self.root)
        with just_before_the_lock(lambda: reread(other, self.h, 75, 'snap-2')):
            with self.assertRaises(review_decisions.DecisionConflict):
                decide_shown(self.box, self.h, ids=[x['key']], action='defer', reason='Owner to confirm', request_id='race',
                                         expected_fps={x['key']: x['fp']}, expected_run=rv['run'])
        self.assertFalse(self.box.get(self.h).get('finding_decisions'))

    def test_a_changed_fingerprint_alone_refuses_even_on_the_same_run(self):
        x, rv = sp_finding(self.box, self.h)
        with self.assertRaises(review_decisions.DecisionConflict) as ctx:
            decide_shown(self.box, self.h, ids=[x['key']], action='defer', reason='Owner to confirm', request_id='fp',
                                     expected_fps={x['key']: 'not-what-it-says'}, expected_run=rv['run'])
        self.assertEqual(ctx.exception.detail['stale'][0], x['key'])

    def test_a_request_that_names_what_was_seen_and_still_matches_is_saved(self):
        x, rv = sp_finding(self.box, self.h)
        _, entry = decide_shown(self.box, self.h, ids=[x['key']], action='defer', reason='Owner to confirm', request_id='ok',
                                            expected_fps={x['key']: x['fp']}, expected_run=rv['run'])
        self.assertEqual(entry['action'], 'defer')
        self.assertEqual(self.box.get(self.h)['finding_decisions'][x['key']]['source_value'], '80')


class OverHttp(unittest.TestCase):
    """The API refuses a decision that does not say what was seen, and refuses a stale one with 409."""

    def setUp(self):
        from api.index import handler
        sys.path.insert(0, os.path.dirname(__file__))
        from legacy_storage_fake import PostgresWorkspace
        from test_vercel import FakeDatabase
        self.env = patch.dict(os.environ, dict(CI_LOCAL_DEV='1', VERCEL='0')); self.env.start()
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), handler)
        self.server.workspace_store = PostgresWorkspace('postgresql://test.invalid/x', FakeDatabase().connect)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f'http://127.0.0.1:{self.server.server_port}'
        with self.server.workspace_store.session() as d:
            self.h = sp_report(SpreadsheetInbox(d))['file_hash']

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

    def test_72_then_75_the_click_made_on_72_is_refused(self):
        st, view = self.req('/api/spreadsheet?id=' + self.h)
        x = next(i for i in view['review']['issues'] if i.get('source') == 'SharePoint' and i.get('cell') == 'J5')
        body = dict(file_hash=self.h, ids=[x['key']], action='defer', reason='Owner to confirm', request_id='click', expected_rev=0,
                    **shown(view['review'], [x['key']]))
        with self.server.workspace_store.session() as d:
            reread(SpreadsheetInbox(d), self.h, 75, 'snap-2')
        st, out = self.req('/api/spreadsheets/decide', body)
        self.assertEqual(st, 409, out)
        st, after = self.req('/api/spreadsheet?id=' + self.h)
        self.assertEqual(after['review']['decisions']['rev'], 0)

    def test_keep_as_recorded_and_fine_as_is_are_bound_the_same_way(self):
        st, view = self.req('/api/spreadsheet?id=' + self.h)
        x = next(i for i in view['review']['issues'] if i.get('source') == 'SharePoint' and i.get('cell') == 'J5')
        keep = dict(file_hash=self.h, key=x['key'], action='keep', by='Lauren', **shown(view['review'], [x['key']]))
        fine = dict(file_hash=self.h, by='Lauren', issues=[{k: x.get(k) for k in ('code', 'sheet', 'row', 'cell', 'key', 'text')}], **shown(view['review'], [x['key']]))
        self.assertEqual(self.req('/api/spreadsheets/decision', {k: v for k, v in keep.items() if k not in ('expected_run', 'expected_fps')})[0], 409)
        self.assertEqual(self.req('/api/spreadsheets/mark-fine', {k: v for k, v in fine.items() if k not in ('expected_run', 'expected_fps')})[0], 409)
        with self.server.workspace_store.session() as d:
            reread(SpreadsheetInbox(d), self.h, 75, 'snap-2')                # 80 -> 75 while the page was open
        self.assertEqual(self.req('/api/spreadsheets/decision', keep)[0], 409)
        self.assertEqual(self.req('/api/spreadsheets/mark-fine', fine)[0], 409)
        with self.server.workspace_store.session() as d:
            item = SpreadsheetInbox(d).get(self.h)
        self.assertFalse(item.get('review_decisions'))
        self.assertFalse(item.get('accepted_issues'))

    def test_a_request_without_what_was_seen_is_refused(self):
        st, view = self.req('/api/spreadsheet?id=' + self.h)
        key = view['review']['issues'][0]['key']
        st, out = self.req('/api/spreadsheets/decide', dict(file_hash=self.h, ids=[key], action='confirm', request_id='old-page', expected_rev=0))
        self.assertEqual(st, 409)
        self.assertTrue(out.get('error'))
        with self.server.workspace_store.session() as d:
            self.assertFalse(SpreadsheetInbox(d).get(self.h).get('finding_decisions'))


class ApprovalUnderTheLock(unittest.TestCase):
    """Issue 2: approval, its holds and the frozen state are checked inside the save; delivery checks again."""

    def setUp(self):
        import tempfile
        self.root = tempfile.mkdtemp()
        self.box = SpreadsheetInbox(self.root)
        self.h = fx.build(self.box, complete=True)['file_hash']          # one blocker (the broken clip): approvable with a reason
        rv = summarize(self.box.get(self.h))
        self.clip = next(x['key'] for x in rv['issues'] if x['severity'] == 'high')

    def approve(self, box=None):
        return approve_shown(box or self.box, self.h, 'Lauren', note='Clip accepted by the customer.', override=True)

    def defer_clip(self, box=None, rid='defer'):
        return decide_shown(box or self.box, self.h, ids=[self.clip], action='defer', reason='Waiting for the list owner.', owner='List owner', request_id=rid)

    def test_a_hold_that_arrives_while_the_approval_is_on_its_way_refuses_it(self):
        other = SpreadsheetInbox(self.root)
        with just_before_the_lock(lambda: self.defer_clip(other)):
            with self.assertRaises(ValueError) as ctx:
                self.approve()
        self.assertIn('changed after it was displayed', str(ctx.exception))
        self.assertTrue(any(d['action']=='defer' for d in self.box.get(self.h)['finding_decisions'].values()))
        self.assertFalse(self.box.get(self.h).get('approval'))

    def test_decisions_cannot_change_on_an_approved_version_not_even_by_undo(self):
        _, e = decide_shown(self.box, self.h, ids=fx.l_ids(summarize(self.box.get(self.h)))[:1], action='reject', reason=REASON, request_id='a')
        self.approve()
        with self.assertRaises(review_decisions.DecisionConflict):
            self.defer_clip()
        with self.assertRaises(review_decisions.DecisionConflict):
            decide_shown(self.box, self.h, action='undo', undo_of=e['decision_id'], request_id='u')
        self.assertTrue(approval_current(self.box.get(self.h)))

    def test_an_approval_that_lands_first_freezes_decisions_made_in_the_same_moment(self):
        other = SpreadsheetInbox(self.root)
        with just_before_the_lock(lambda: self.approve(other)):
            with self.assertRaises(review_decisions.DecisionConflict):
                self.defer_clip()
        item = self.box.get(self.h)
        self.assertTrue(item.get('approval'))
        self.assertFalse(item.get('finding_decisions'))

    def test_any_other_change_to_decisions_withdraws_the_approval_and_delivery_refuses(self):
        self.approve()
        add = [dict(code='missing_cell', sheet='Inspection Detail', row=81, cell='L81', text='Missing cell.', by_person=True, reason='fine')]
        self.box._update(self.h, lambda r: r.update(accepted_issues=(r.get('accepted_issues') or []) + add))   # e.g. "mark fine"
        item = self.box.get(self.h)
        self.assertFalse(item.get('approval'))
        self.assertIn('decisions on this report changed', item['approval_withdrawn']['reason'])
        with self.assertRaises(ValueError):
            self.box.verify_for_delivery(self.h)

    def test_an_approval_from_before_approvals_recorded_their_decisions_is_not_current(self):
        self.approve()
        self.box._update(self.h, lambda r: r['approval'].pop('decisions_basis'))
        self.assertFalse(approval_current(self.box.get(self.h)))
        with self.assertRaises(ValueError):
            self.box.verify_for_delivery(self.h)

    def test_delivery_checks_again_and_sends_nothing_when_the_run_or_holds_changed(self):
        from hosting import mailer
        sent = []
        fake = lambda *a, **k: sent.append(a) or dict(sent=True, sender='x')
        self.approve()
        reread(self.box, self.h, 75, 'later-read')                          # SharePoint read again after approval: a new run
        with patch.object(mailer, 'deliver', side_effect=fake), self.assertRaises(ValueError):
            delivery.send(_Store(self.root), self.h, dict(to=['qa@example.com'], subject='s', body='b'))
        self.box.approve(self.h, 'Lauren', withdraw=True)
        with self.assertRaisesRegex(ValueError, 'not fully verified'):
            self.approve()  # A discrepancy-only read cannot be overridden into approval.
        # Independently complete source evidence restores the guarded approval path.
        self.h = self.box.ingest(NAME, sl_daily(nok_blank=False))['file_hash']
        verified_fixture(self.box, self.box.get(self.h))
        self.approve()
        total = self.box.get(self.h)['finding_count'] + 50
        self.box._update(self.h, lambda r: r.update(finding_count=total))   # ... then a hard hold appears (50 findings not stored)
        self.assertFalse(approval_current(self.box.get(self.h)))
        with patch.object(mailer, 'deliver', side_effect=fake), self.assertRaises(ValueError) as ctx:
            delivery.send(_Store(self.root), self.h, dict(to=['qa@example.com'], subject='s', body='b'))
        self.assertIn('paused', str(ctx.exception))
        self.assertEqual(sent, [])

    def test_concurrent_approve_and_defer_never_leave_an_approved_report_with_an_open_question(self):
        import tempfile
        outcomes = []
        for n in range(12):
            root = tempfile.mkdtemp()
            box = SpreadsheetInbox(root)
            self.h = fx.build(box, complete=True)['file_hash']
            gate, errs = threading.Barrier(2), []
            def a():
                gate.wait()
                try: self.approve(SpreadsheetInbox(root))
                except ValueError as exc: errs.append(('approve', type(exc).__name__))
            def d():
                gate.wait()
                try: self.defer_clip(SpreadsheetInbox(root), rid=f'd{n}')
                except ValueError as exc: errs.append(('defer', type(exc).__name__))
            ts = [threading.Thread(target=a), threading.Thread(target=d)]
            [t.start() for t in ts]; [t.join() for t in ts]
            item = box.get(self.h)
            deferred = any(v.get('action') == 'defer' for v in (item.get('finding_decisions') or {}).values())
            self.assertFalse(item.get('approval') and deferred, (n, errs))
            self.assertEqual(len(errs), 1, errs)                               # exactly one of the two went through
            outcomes.append(errs[0][0])
        self.assertTrue(outcomes)


class AutoFixHonoursPeopleAndSources(unittest.TestCase):
    """Issue 3: the automatic pass keeps source metadata, honours reject / defer and refuses stale source data."""

    def setUp(self):
        self.env = patch.dict(os.environ, {}, clear=False); self.env.start()
        os.environ.pop('ANTHROPIC_API_KEY', None)
        self.root, self.box = inbox()
        root = self.root
        class H:
            def store(self):
                return _Store(root)
            def baseline_check(self, file_hash):
                return None
        self.h_ = H()
        self.v1 = sp_report(self.box)['file_hash']                         # J5: report 72, SharePoint 80; H6: stray spaces
        self.addCleanup(self.env.stop)

    def cell(self, file_hash, ref):
        _, raw = self.box.original_bytes(file_hash)
        return load_workbook(io.BytesIO(raw))['Inspection Detail'][ref].value

    def versions(self):
        with self.box.connect() as db:
            return db.execute('SELECT COUNT(*) FROM spreadsheets').fetchone()[0]

    def test_the_sharepoint_correction_carries_its_read_item_and_field(self):
        displayed=self.box.get(self.v1)
        props=[p for p in self.box.fix_proposals(self.v1)['proposals'] if p['cell']=='J5']
        v2=self.box.apply_fixes(self.v1,props,expected_check_hash=displayed['check_hash'])
        change = next(c for c in v2['fixed_from']['changes'] if c['cell'] == 'J5')
        self.assertEqual((change['snapshot_id'], change['item'], change['field'], change['after']), ('snap-1', '433', 'inspected', '80'))
        self.assertEqual(self.cell(v2['file_hash'], 'J5'), 80)

    def automatic_is_paused(self):
        before=self.box.get(self.v1)
        raw=self.box.original_bytes(self.v1)[1]
        count=self.versions()
        with patch.object(SpreadsheetInbox,'apply_fixes',side_effect=AssertionError('No automatic mutation')), self.assertRaisesRegex(autofix.AutoFixError,'disabled'):
            autofix.run(self.h_,self.v1)
        self.assertEqual(self.box.get(self.v1),before)
        self.assertEqual(self.box.original_bytes(self.v1)[1],raw)
        self.assertEqual(self.versions(),count)
        return before

    def test_a_finding_kept_as_recorded_is_not_changed(self):
        x, _ = sp_finding(self.box, self.v1)
        decide_shown(self.box, self.v1, ids=[x['key']], action='reject', reason='Checked on the floor: 72 is right.',
                                 evidence=['Floor count photo'], request_id='keep')
        v2 = self.automatic_is_paused()
        self.assertEqual(self.cell(v2['file_hash'], 'J5'), 72)
        self.assertNotIn('J5', [c['cell'] for c in (v2.get('fixed_from') or {}).get('changes') or []])

    def test_a_deferred_finding_is_not_changed(self):
        x, _ = sp_finding(self.box, self.v1)
        decide_shown(self.box, self.v1, ids=[x['key']], action='defer', reason='Owner to confirm the count.', owner='List owner', request_id='wait')
        v2 = self.automatic_is_paused()
        self.assertEqual(self.cell(v2['file_hash'], 'J5'), 72)

    def test_a_finding_whose_decision_no_longer_matches_is_left_for_a_person(self):
        x, _ = sp_finding(self.box, self.v1)
        decide_shown(self.box, self.v1, ids=[x['key']], action='defer', reason='Owner to confirm.', owner='List owner', request_id='old')
        reread(self.box, self.v1, 75, 'snap-2')                             # the decision was made on 80; now it says 75
        now, _ = sp_finding(self.box, self.v1)
        self.assertTrue(now.get('previous_decision'))
        v2 = self.automatic_is_paused()
        self.assertEqual(self.cell(v2['file_hash'], 'J5'), 72)

    def test_a_sharepoint_change_without_its_read_item_and_field_is_refused(self):
        bare = dict(sheet='Inspection Detail', cell='J5', before='72', after='80', reason='SharePoint', source='SharePoint', kind='number')
        for auto in (True, False):
            with self.assertRaises(ValueError):
                self.box.apply_fixes(self.v1, [bare], auto=auto, expected_check_hash=self.box.get(self.v1)['check_hash'])
        self.assertFalse(self.box.get(self.v1).get('fixes_applied'))

    def test_auto_may_not_change_a_rejected_or_deferred_cell_even_if_asked_directly(self):
        x, _ = sp_finding(self.box, self.v1)
        decide_shown(self.box, self.v1, ids=[x['key']], action='defer', reason='Owner to confirm.', owner='List owner', request_id='d')
        props = [p for p in self.box.fix_proposals(self.v1)['proposals'] if p['cell'] == 'J5']
        with self.assertRaises(ValueError):
            self.box.apply_fixes(self.v1, [{k: v for k, v in props[0].items() if v is not None}], auto=True, expected_check_hash=self.box.get(self.v1)['check_hash'])
        self.assertFalse(self.box.get(self.v1).get('fixes_applied'))

    def test_a_sharepoint_read_landing_during_the_pass_creates_no_corrected_version(self):
        before = self.versions()
        displayed=self.box.get(self.v1)
        other = SpreadsheetInbox(self.root)
        props = [p for p in self.box.fix_proposals(self.v1)['proposals'] if p['cell'] == 'J5']
        with just_before_the_lock(lambda: reread(other, self.v1, 75, 'snap-2')):
            with self.assertRaises(ValueError):
                self.box.apply_fixes(self.v1, [{k: v for k, v in props[0].items() if v is not None}], expected_check_hash=displayed['check_hash'])
        self.assertFalse(self.box.get(self.v1).get('fixes_applied'))
        self.assertEqual(self.versions(), before)                           # no corrected version was stored
        self.assertEqual(self.cell(self.v1, 'J5'), 72)


def classic(box, h, value, run=1):
    """The classic SharePoint comparison (export check) instead of the live reconciliation: J5 72 here, ``value`` there."""
    def ch(r):
        r.pop('source_reconciliation', None)
        r['sharepoint'] = dict(status='differences', run=run, findings=[dict(code='differs_from_sharepoint', sheet='Inspection Detail', cell='J5', field='inspected',
                                                                     sharepoint_value=str(value), report_value='72', message=f'SharePoint shows {value}, report 72')])
    return box._update(h, ch)


class SecondReview(AutoFixHonoursPeopleAndSources):
    """Holes found by a second, independent review of the fix: every automatic path, the classic SharePoint check,
    frozen state on the older decision routes, and delivery."""

    def setUp(self):
        super().setUp()
        self.x, _ = sp_finding(self.box, self.v1)

    def keep(self, h=None, action='reject'):
        kw = dict(reason='Counted on the floor: 72 is right.', evidence=['Floor photo']) if action == 'reject' else dict(reason='Owner to confirm.', owner='List owner')
        rv = summarize(self.box.get(h or self.v1))
        x = next(i for i in rv['issues'] + rv['dismissed'] if 'SharePoint' in str(i.get('source')) and i.get('cell') == 'J5')
        decide_shown(self.box, h or self.v1, ids=[x['key']], action=action, request_id='k-' + os.urandom(3).hex(), **kw)

    def test_automatic_cleanup_leaves_a_kept_or_deferred_cell(self):
        from hosting import autoformat
        for action in ('reject', 'defer'):
            self.setUp(); self.keep(action=action)
            out = autoformat.run(self.h_, self.v1)
            new = out['file_hash'] if out else self.v1
            self.assertEqual(self.cell(new, 'J5'), 72, action)

    def test_autopilot_leaves_a_kept_cell(self):
        from hosting import autopilot
        self.keep()
        before=self.box.get(self.v1)
        with self.assertRaisesRegex(ValueError,'disabled'):
            autopilot.clean(self.h_, self.v1)
        self.assertEqual(self.box.get(self.v1),before)
        self.assertEqual(self.cell(self.v1, 'J5'), 72)

    def test_accept_all_through_the_api_leaves_a_deferred_cell(self):
        from hosting import public_api
        self.keep(action='defer')
        displayed=self.box.get(self.v1)
        with self.assertRaises(ValueError):
            public_api.op_apply(self.h_, self.v1, accept='all',expected_check_hash=displayed['check_hash'])
        self.assertEqual(self.box.get(self.v1),displayed)
        self.assertEqual(self.cell(self.v1, 'J5'), 72)

    def test_ai_refuses_before_a_model_plan_or_competing_defer_can_run(self):
        from hosting import ai_fix, ai_review
        other = SpreadsheetInbox(self.root)
        def resolve(item, raw, issues, opener=None):
            decide_shown(other, self.v1, ids=[self.x['key']], action='defer', reason='Owner to confirm.', owner='List owner', request_id='mid')
            return dict(model='fake', cells=[dict(ref=self.x['key'], sheet='Inspection Detail', cell='J5', before='72', after='80', reason='SharePoint', confidence='sure')],
                        removes=[], moves=[], fine=[])
        with patch.object(ai_review, 'resolve', side_effect=resolve) as model, self.assertRaises(ai_fix.AIFixError) as ctx:
            ai_fix.run(self.h_, self.v1, keys=[self.x['key']], after=False)
        self.assertIn('disabled', str(ctx.exception))
        model.assert_not_called()
        self.assertFalse(self.box.get(self.v1).get('finding_decisions'))
        self.assertFalse(self.box.get(self.v1).get('fixes_applied'))

    def test_classic_sharepoint_corrections_are_bound_to_their_comparison(self):
        classic(self.box, self.v1, 80)
        p = next(p for p in self.box.fix_proposals(self.v1)['proposals'] if p['cell'] == 'J5')
        self.assertTrue(p['snapshot_id'].startswith('classic:'))
        change = {k: v for k, v in p.items() if v is not None}
        displayed=self.box.get(self.v1)
        classic(self.box, self.v1, 75)                                     # the comparison ran again with another value
        with self.assertRaises(ValueError):
            self.box.apply_fixes(self.v1, [change],expected_check_hash=displayed['check_hash'])
        classic(self.box, self.v1, 80, run=3)                              # a third comparison, 80 again: reviewed afresh below
        p = next(p for p in self.box.fix_proposals(self.v1)['proposals'] if p['cell'] == 'J5')
        v2 = self.box.apply_fixes(self.v1, [{k: v for k, v in p.items() if v is not None}],expected_check_hash=self.box.get(self.v1)['check_hash'])   # a person accepting it works
        self.assertEqual(self.cell(v2['file_hash'], 'J5'), 80)

    def test_the_automatic_pass_is_paused_with_a_classic_sharepoint_correction(self):
        classic(self.box, self.v1, 80)
        v2 = self.automatic_is_paused()
        self.assertEqual(self.cell(v2['file_hash'], 'J5'), 72)

    def test_a_classic_comparison_run_again_after_approval_ends_the_approval(self):
        classic(self.box, self.v1, 80)
        self.keep()
        approve_shown(self.box, self.v1, 'Lauren', note='ok', override=True)
        classic(self.box, self.v1, 75)                                     # the reject was about 80
        self.assertFalse(approval_current(self.box.get(self.v1)))
        with self.assertRaises(ValueError):
            self.box.verify_for_delivery(self.v1)

    def test_keep_as_recorded_and_fine_as_is_are_refused_on_an_approved_or_sent_version(self):
        self.v1 = approval_fixture(self.box)['file_hash']
        rv = summarize(self.box.get(self.v1))
        x = rv['issues'][0]
        approve_shown(self.box, self.v1, 'Lauren', note='ok', override=True)
        for attempt in (lambda: self.box.record_decisions(self.v1, [x['key']], 'keep', '', 'Lauren', {x['key']: x['fp']}, rv['run']),
                        lambda: self.box.mark_fine(self.v1, [dict(code=x['code'], sheet=x['sheet'], row=x.get('row'), key=x['key'], text=x['text'], by_person=True)],
                                                   {x['key']: x['fp']}, rv['run'])):
            with self.assertRaises(ValueError):
                attempt()
        self.box._update(self.v1, lambda r:r.update(sent=[dict(sent=True,to=['qa@example.com'],at='2026-11-08T12:00:00+00:00')]))
        with self.assertRaises(ValueError):
            self.box.record_decisions(self.v1, [x['key']], 'keep', '', 'Lauren', {x['key']: x['fp']}, rv['run'])

    def test_an_approved_version_refuses_a_corrected_copy(self):
        self.v1 = approval_fixture(self.box)['file_hash']
        approve_shown(self.box,self.v1,'Lauren',note='ok',override=True)
        displayed=self.box.get(self.v1)
        before=self.versions()
        p=next(p for p in self.box.fix_proposals(self.v1)['proposals'] if p['cell']=='H6')
        with self.assertRaises(ValueError):
            self.box.apply_fixes(self.v1,[{k:v for k,v in p.items() if v is not None}],expected_check_hash=displayed['check_hash'])
        self.assertEqual(self.box.get(self.v1),displayed)
        self.assertEqual(self.versions(),before)
        self.assertTrue(approval_current(displayed))

    def test_a_changed_client_or_job_after_approval_stops_the_send(self):
        self.v1 = approval_fixture(self.box)['file_hash']
        approve_shown(self.box, self.v1, 'Lauren', note='ok', override=True)
        with patch.object(SpreadsheetInbox, '_assignment_key', return_value=dict(status='resolved', label='Another client', job='9999')):
            with self.assertRaises(ValueError) as ctx:
                self.box.verify_for_delivery(self.v1)
        self.assertIn('paused', str(ctx.exception))

    def test_concurrent_delivery_reservations_and_recording_fail_without_mutation(self):
        from concurrent.futures import ThreadPoolExecutor
        from hosting import mailer
        self.v1 = approval_fixture(self.box)['file_hash']
        approve_shown(self.box,self.v1,'Lauren',note='ok',override=True)
        before=self.box.get(self.v1)
        gate=threading.Barrier(2)
        def attempt():
            box=SpreadsheetInbox(self.root)
            gate.wait()
            with self.assertRaises(ValueError):
                box.verify_for_delivery(self.v1)
        with patch.object(mailer,'deliver',side_effect=AssertionError('No provider calls')) as provider:
            with ThreadPoolExecutor(2) as pool:
                futures=[pool.submit(attempt) for _ in range(2)]
                for future in futures: future.result(timeout=15)
            with self.assertRaises(ValueError):
                self.box.record_send(self.v1,dict(sent=True,to=['qa@example.com']),token='not-a-real-reservation')
            with self.assertRaises(ValueError):
                self.box.verify_for_delivery(self.v1,again=True)
            provider.assert_not_called()
        self.assertEqual(self.box.get(self.v1),before)
        self.assertFalse(before.get('send_in_progress'))
        self.assertFalse(before.get('sent'))


if __name__ == '__main__':
    unittest.main()

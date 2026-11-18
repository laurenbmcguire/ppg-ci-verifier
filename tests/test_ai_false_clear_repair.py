"""Fix with AI must never clear what it could not correct (SYNTHETIC data, real server workflow, no model call).

The reported screen: "2 cells fixed, 0 left as written, 57 errors Dex could not correct ... 0 possible errors still
need you", and a report showing zero errors. The zero was false. An older page, after a Fix with AI run, sent an
automatic "reject" through the reviewer's own session for every finding Dex had no correction for, with the reason
"Dex had no safe change to make ..." and the evidence "Not checked by a person ...". The server stored those as
reviewer decisions, so the findings left the list and the report read as clear.

These tests drive the real inbox: upload, check, the two genuine corrections saved as a new version, then the stored
fallback decisions exactly as that older page left them. They pin that such a decision settles nothing, wherever it is
found (new, already stored, or carried to a later version), that a person's explicit decisions are untouched, and that
nothing is erased from history.
"""
import copy
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(Path(__file__).parent))
from eos_fixture import build as _shape_build
from hosting import review_decisions
from hosting.review_decisions import DecisionError
from hosting.review_summary import check_run, summarize
from hosting.spreadsheet_inbox import SpreadsheetInbox

DAY = '2001-02-03'
PROJECT = '9099-SYNTHETIC-DEMO'
NAME = PROJECT + ' ' + DAY + '.xlsx'


def build(*, rows, overrides=None):
    # Reuse the shape generator, never its customer/day defaults or example data.
    values = {('Summary', 'D3'): PROJECT, ('Summary', 'B1'): 'SYNTHETIC DEMO / END OF SHIFT REPORT'}
    values.update(overrides or {})
    return _shape_build(rows=rows, day=DAY, overrides=values)


def source_csv(rows):
    import csv, io
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(['Date','Shift','Area','Auditor','Identifier','Part Number','Qty Inspected','Qty OK','Qty Reworked','Qty NOK','Defect','Comments','Condition','Picture File'])
    for ident, part, inspected, rejected, defect, comment in rows:
        writer.writerow([DAY,'1','','Synthetic Reviewer',ident,part,inspected,inspected-rejected,'',rejected,defect,comment,'NOK',''])
    return out.getvalue().encode()
LEGACY_HARD = ('Dex had no safe change to make and no correct value to put in, so this was left exactly as written by Fix with AI. Not corrected.',
               ['Not checked by a person. Left as written by Fix with AI: Dex found no correct value for it in the report or in SharePoint.'])
LEGACY_SOFT = ('Dex had no safe change to make, so this was left as written by Fix with AI.', [])
OPEN = (None, 'reopen')


def workbook():
    """57 rows with rejects and no defect reason (nothing Dex can correct) and 2 quantities typed as text (fixable)."""
    rows = [(f'90000{n:05d}', '99000001', 30 + n % 5, 3, '', '') for n in range(57)]
    rows += [('9000100001', '99000001', 30, 0, '', ''), ('9000100002', '99000001', 31, 0, '', '')]
    return build(rows=rows, overrides={('1st Shift', 'G73'): '30', ('1st Shift', 'G74'): '31'})


def real(issues):
    return [i for i in issues if i['severity'] != 'info']


def still_open(summary):
    return [i for i in real(summary['issues']) if (i.get('decision') or {}).get('action') in OPEN]


class FixWithAIFlow(unittest.TestCase):
    def setUp(self):
        self.box = SpreadsheetInbox(tempfile.mkdtemp())
        self.parent = self.box.ingest(NAME, workbook())
        first = summarize(self.parent)
        self.assertEqual((len(real(first['issues'])), first['counts']['medium'], first['counts']['low']), (59, 57, 2))
        # The two genuine corrections, exactly as the app proposes them, saved as a new version through the real route.
        proposals = self.box.fix_proposals(self.parent['file_hash'])['proposals']
        self.assertEqual(len(proposals), 2)
        fixed = self.box.apply_fixes(self.parent['file_hash'], proposals, expected_check_hash=self.box.get(self.parent['file_hash'])['check_hash'])
        self.child_hash = fixed['file_hash']
        self.assertNotEqual(self.child_hash, self.parent['file_hash'])

    def child(self):
        return self.box.get(self.child_hash)

    def summary(self):
        return summarize(self.child())

    def store_legacy_fallback(self, keys, legacy=LEGACY_HARD, by='reviewer@example.invalid'):
        """Leave on the saved version exactly what the older page left: a 'reject' made through the reviewer's session.
        The binding (fingerprints, revision, check run, log entry) is the real one; only the route's new refusal is
        stepped around, because these records already exist where the older page ran."""
        def change(r):
            rv = self.box.review_summary(r)
            current = {x['key']: x for x in rv['issues'] + rv['dismissed']}
            entry = review_decisions.decide(r, current, ids=keys, action='reject', reason='placeholder', by=by, evidence=['placeholder'],
                                            request_id='legacy-%d' % int(r.get('finding_decisions_rev') or 0), expected_rev=int(r.get('finding_decisions_rev') or 0),
                                            expected_run=check_run(r), expected_fps={k: current[k]['fp'] for k in keys})
            for k in keys:
                r['finding_decisions'][k].update(reason=legacy[0], evidence=list(legacy[1]))
            r['finding_decision_log'][-1].update(reason=legacy[0], evidence=list(legacy[1]))
            return entry
        return self.box._update(self.child_hash, change)

    def test_the_two_corrections_are_kept_and_57_remain(self):
        child, s = self.child(), self.summary()
        self.assertEqual(len((child.get('fixed_from') or {}).get('changes') or []), 2)       # the two genuine corrections are on the new version
        self.assertEqual({c['cell'] for c in child['fixed_from']['changes']}, {'G73', 'G74'})
        self.assertEqual((len(still_open(s)), s['counts']['medium'], s['counts']['low'], s['status']), (57, 57, 0, 'check'))
        self.assertEqual(summarize(self.box.get(self.parent['file_hash']))['fixes_applied'], 1)   # the starting version is kept

    def test_stored_fallback_decisions_do_not_clear_the_report(self):
        keys = [i['key'] for i in still_open(self.summary())]
        self.assertEqual(len(keys), 57)
        before = copy.deepcopy(self.child())
        self.store_legacy_fallback(keys)
        child, s = self.child(), self.summary()
        # the false clear: before the repair this read 0 open, status clear, 57 "kept by a person"
        self.assertEqual(len(still_open(s)), 57)
        self.assertEqual((s['counts']['medium'], s['status']), (57, 'check'))
        self.assertEqual((s['reconcile']['open'], s['reconcile']['kept_by_person'], s['reconcile']['ai_fallback_unresolved']), (57, 0, 57))
        self.assertTrue(s['reconcile']['adds_up'])
        self.assertEqual((s['decisions']['rejected'], s['decisions']['ai_fallback_not_applied']), (0, 57))
        self.assertEqual(len(s['dismissed']), 0)
        for i in still_open(s):
            self.assertIs(i['ai_fallback_not_review'], True)
            self.assertIn('That is not a review, so this finding is unresolved', i['previous_decision']['note'])
            self.assertEqual(i['previous_decision']['reason'], LEGACY_HARD[0])            # shown, not erased
        self.assertEqual(len(s['stale_decisions']), 57)
        # nothing in history was rewritten by reading it
        self.assertEqual(child['finding_decisions'], self.child()['finding_decisions'])
        self.assertEqual(len(child['finding_decisions']), 57)
        self.assertEqual(len(child['finding_decision_log']), len(before.get('finding_decision_log') or []) + 1)
        self.assertEqual(child['finding_decision_log'][-1]['reason'], LEGACY_HARD[0])

    def test_the_older_pages_request_is_refused_for_every_kind_of_finding(self):
        s = self.summary()
        keys = [i['key'] for i in still_open(s)]
        fps = {i['key']: i['fp'] for i in still_open(s)}
        for reason, evidence in (LEGACY_HARD, LEGACY_SOFT, ('  DEX HAD NO SAFE CHANGE  ', ['x']), ('Looked fine', ['not checked by a person'])):
            with self.subTest(reason=reason[:30]), self.assertRaises(DecisionError) as refused:
                self.box.decide_findings(self.child_hash, ids=keys, action='reject', reason=reason, evidence=evidence, by='reviewer@example.invalid',
                                         request_id='stale-page-' + str(len(reason)), expected_rev=0, expected_fps=fps, expected_run=check_run(self.child()))
            self.assertIn('remain open', str(refused.exception))
        self.assertEqual(len(still_open(self.summary())), 57)
        self.assertFalse(self.child().get('finding_decisions'))

    def test_every_reading_of_the_version_agrees(self):
        """The detail summary, the public API, the list entry and the checked-copy proof must not disagree."""
        from hosting import public_api
        keys = [i['key'] for i in still_open(self.summary())]
        self.store_legacy_fallback(keys)
        child = self.child()
        api = public_api.report(child, inbox=self.box)
        self.assertEqual(len([i for i in api['issues'] if i.get('severity') != 'info']), 57)
        self.assertEqual((api['dismissed_total'], api['dismissed_by_ai']), (0, 0))
        self.assertNotEqual(api.get('status'), 'clear')
        listed = next(r for r in self.box.list()['spreadsheets'] if r['file_hash'] == self.child_hash)
        review = listed.get('review') or {}
        self.assertEqual((review.get('counts') or {}).get('medium'), 57)
        self.assertNotEqual(review.get('status'), 'clear')
        self.assertEqual(self.box.review_summary(child)['reconcile']['open'], 57)

    def test_replaying_the_older_pages_own_request_changes_nothing_and_clears_nothing(self):
        keys = [i['key'] for i in still_open(self.summary())]
        self.store_legacy_fallback(keys)
        child = self.child()
        entry = child['finding_decision_log'][-1]
        fps = {k: child['finding_decisions'][k]['fp'] for k in keys}
        # the same request arriving again is answered from the record (as any repeated click is); it is not applied again
        try:
            again, replay = self.box.decide_findings(self.child_hash, ids=keys, action='reject', reason=LEGACY_HARD[0], evidence=LEGACY_HARD[1],
                                                     by='reviewer@example.invalid', request_id=entry['request_id'], expected_rev=0, expected_fps=fps,
                                                     expected_run=check_run(child))
        except DecisionError:
            again = self.child()                                                         # refused outright is just as good
        self.assertEqual(again['finding_decisions_rev'], child['finding_decisions_rev'])
        self.assertEqual(len(again['finding_decision_log']), len(child['finding_decision_log']))
        self.assertEqual(len(still_open(self.summary())), 57)

    def test_a_persons_explicit_decisions_are_untouched(self):
        s = self.summary()
        keys = [i['key'] for i in still_open(s)]
        fps = {i['key']: i['fp'] for i in still_open(s)}
        mine, rest = keys[:3], keys[3:]
        self.box.decide_findings(self.child_hash, ids=mine, action='reject', reason='Compared with the defect sheet: reason recorded there.',
                                 evidence=['Defect sheet rows 4 to 6'], by='reviewer@example.invalid', request_id='human-1', expected_rev=0,
                                 expected_fps={k: fps[k] for k in mine}, expected_run=check_run(self.child()))
        self.store_legacy_fallback(rest)
        s = self.summary()
        self.assertEqual((len(still_open(s)), s['reconcile']['kept_by_person'], s['decisions']['rejected'], s['decisions']['ai_fallback_not_applied']), (54, 3, 3, 54))
        self.assertEqual({d['key'] for d in s['dismissed']}, set(mine))
        self.assertTrue(all(d['decision']['reason'].startswith('Compared with the defect sheet') for d in s['dismissed']))
        self.assertTrue(s['reconcile']['adds_up'])

    def test_a_person_can_then_decide_a_finding_the_fallback_touched(self):
        keys = [i['key'] for i in still_open(self.summary())]
        self.store_legacy_fallback(keys)
        s = self.summary()
        one = still_open(s)[0]
        self.box.decide_findings(self.child_hash, ids=[one['key']], action='reject', reason='Checked the part: no defect, quantity entered in error.',
                                 evidence=['Photo 12'], by='reviewer@example.invalid', request_id='human-after', expected_rev=self.child()['finding_decisions_rev'],
                                 expected_fps={one['key']: one['fp']}, expected_run=check_run(self.child()))
        s = self.summary()
        self.assertEqual((len(still_open(s)), s['reconcile']['kept_by_person'], s['decisions']['ai_fallback_not_applied']), (56, 1, 56))



class ExactRecognition(FixWithAIFlow):
    """QA of 4df26d8 (1): only the two exact payloads the older page sent are the fallback. A person's decision that
    merely begins with the same words, or carries real evidence, is theirs and stays applied."""

    QA_REASON = 'Dex had no safe change, but I personally checked SharePoint item 42 and approved the recorded value.'
    QA_EVIDENCE = ['Synthetic item 42 inspected12 OK10 NOK2']

    def stored(self, key, reason, evidence):
        return self.store_legacy_fallback([key], legacy=(reason, evidence))

    def test_the_exact_qa_decision_is_a_persons_and_stays_applied(self):
        first = still_open(self.summary())[0]
        self.stored(first['key'], self.QA_REASON, self.QA_EVIDENCE)
        s = self.summary()
        self.assertEqual((len(still_open(s)), s['reconcile']['kept_by_person'], s['reconcile']['ai_fallback_unresolved']), (56, 1, 0))
        kept = s['dismissed'][0]
        self.assertEqual((kept['key'], kept['decision']['reason'], kept['decision']['evidence']), (first['key'], self.QA_REASON, self.QA_EVIDENCE))
        self.assertNotIn('fallback_wording_review_needed', kept)
        self.assertEqual((s['decisions']['ai_fallback_not_applied'], s['decisions']['fallback_wording_review_needed']), (0, 0))

    def test_same_prefix_and_marker_words_do_not_make_a_fallback(self):
        cases = [(self.QA_REASON, self.QA_EVIDENCE), ('Dex had no safe change to make, so this was left as written by Fix with AI', []),   # no final full stop
                 ('dex had no safe change to make, so this was left as written by fix with ai.', []),                                     # different capitals
                 ('Dex had no safe change to make', ['Photo 4 shows the label']), ('Kept: checked by me', ['The earlier note said "not checked by a person"; I have now checked it.']),
                 ('Left as written by Fix with AI earlier; I compared the part and it is correct.', ['Part compared at the line'])]
        for reason, evidence in cases:
            with self.subTest(reason=reason[:40]):
                d = dict(action='reject', reason=reason, evidence=evidence)
                self.assertFalse(review_decisions.automated_fallback(d))
                self.assertIsNone(review_decisions.fallback_class(d))

    def test_only_the_two_exact_payloads_are_the_fallback(self):
        self.assertEqual(review_decisions.fallback_class(dict(action='reject', reason=LEGACY_HARD[0], evidence=list(LEGACY_HARD[1]))), 'fallback')
        self.assertEqual(review_decisions.fallback_class(dict(action='reject', reason=LEGACY_SOFT[0], evidence=[])), 'fallback')
        self.assertEqual(review_decisions.fallback_class(dict(action='reject', reason='  ' + LEGACY_SOFT[0].replace(' ', '  ') + ' ', evidence=None)), 'fallback')   # only spacing differs
        for action in ('defer', 'confirm', 'reopen', 'undo'):
            self.assertIsNone(review_decisions.fallback_class(dict(action=action, reason=LEGACY_HARD[0], evidence=list(LEGACY_HARD[1]))))


    def test_a_later_explicit_decision_replaces_a_fallback_and_is_kept(self):
        keys = [i['key'] for i in still_open(self.summary())]
        self.store_legacy_fallback(keys)
        one = still_open(self.summary())[0]
        # the write route still refuses a NEW decision that starts with the old page's words (a refused write saves nothing; the person rewords it)
        with self.assertRaises(DecisionError):
            self.box.decide_findings(self.child_hash, ids=[one['key']], action='reject', reason=self.QA_REASON, evidence=self.QA_EVIDENCE, by='reviewer@example.invalid',
                                     request_id='later-human-1', expected_rev=self.child()['finding_decisions_rev'], expected_fps={one['key']: one['fp']}, expected_run=check_run(self.child()))
        self.box.decide_findings(self.child_hash, ids=[one['key']], action='reject', reason='I personally checked SharePoint item 42 and approved the recorded value.',
                                 evidence=self.QA_EVIDENCE, by='reviewer@example.invalid', request_id='later-human-2', expected_rev=self.child()['finding_decisions_rev'],
                                 expected_fps={one['key']: one['fp']}, expected_run=check_run(self.child()))
        s = self.summary()
        self.assertEqual((len(still_open(s)), s['reconcile']['kept_by_person'], s['reconcile']['ai_fallback_unresolved']), (56, 1, 56))
        self.assertEqual(s['dismissed'][0]['key'], one['key'])
        log = self.child()['finding_decision_log']
        self.assertEqual((log[-2]['reason'], log[-1]['evidence']), (LEGACY_HARD[0], self.QA_EVIDENCE))      # both events are in the history


class NothingAnAIDidCanHideARecoveredFinding(FixWithAIFlow):
    """QA of 4df26d8 (2): a finding recovered from the fallback stays open whatever an AI double-check or an AI
    "fine as is" record says about it. Only a person's explicit decision or an actual correction settles it."""

    def recovered(self):
        keys = [i['key'] for i in still_open(self.summary())]
        self.store_legacy_fallback(keys)
        return keys

    def with_ai(self, keys, *, verify=True, fine=True):
        def change(r):
            rv = self.box.review_summary(r)
            by_key = {x['key']: x for x in rv['issues'] + rv['dismissed']}
            if verify:
                r['ai_verify'] = {'status': 'done', 'run': check_run(r), 'verdicts': {k: {'verdict': 'not_an_issue', 'reason': 'Earlier AI said fine'} for k in keys}}
            if fine:
                r['accepted_issues'] = [dict(code=by_key[k]['code'], sheet=by_key[k].get('sheet'), row=by_key[k].get('row'), cell=by_key[k].get('cell'), text=by_key[k].get('text'),
                                             fp=by_key[k]['fp'], run=check_run(r), reason='Dex judged this fine', at='2001-02-03T00:00:00Z') for k in keys]
        self.box._update(self.child_hash, change)

    def assert_all_open(self, n=57):
        child = self.child()
        for s in (summarize(child), self.box.review_summary(child)):
            self.assertEqual(len(still_open(s)), n)
            r = s['reconcile']
            self.assertEqual((r['open'], r['set_aside_by_claude'], r['kept_by_person'], r['ai_fallback_unresolved']), (n, 0, 0, n))
            self.assertTrue(r['adds_up'])
            self.assertEqual((s['counts']['medium'], s['status'], s['verify']['dismissed'], s['coverage']['dismissed']), (n, 'check', 0, 0))
        from hosting import public_api
        api = public_api.report(child, inbox=self.box)
        self.assertEqual((len([i for i in api['issues'] if i.get('severity') != 'info']), api['dismissed_total']), (n, 0))
        listed = next(r for r in self.box.list()['spreadsheets'] if r['file_hash'] == self.child_hash)
        self.assertEqual(((listed['review'].get('counts') or {}).get('medium'), listed['review'].get('status')), (n, 'check'))

    def test_the_exact_qa_reproduction_an_earlier_double_check(self):
        keys = self.recovered()
        self.with_ai(keys, fine=False)
        self.assert_all_open()

    def test_an_ai_fine_as_is_record(self):
        keys = self.recovered()
        self.with_ai(keys, verify=False)
        self.assert_all_open()

    def test_both_together_and_the_ai_views_stay_attached_as_information(self):
        keys = self.recovered()
        self.with_ai(keys)
        self.assert_all_open()
        one = still_open(self.summary())[0]
        self.assertEqual((one['verdict'], one['verdict_reason']), ('not_an_issue', 'Earlier AI said fine'))   # shown, not acted on

    def test_without_a_fallback_the_existing_double_check_rule_is_unchanged(self):
        keys = [i['key'] for i in still_open(self.summary())]
        self.with_ai(keys, fine=False)
        s = self.summary()
        self.assertEqual((len(still_open(s)), s['reconcile']['set_aside_by_claude'], s['reconcile']['ai_fallback_unresolved']), (0, 57, 0))

    def test_a_person_then_settles_one_and_only_that_one(self):
        keys = self.recovered()
        self.with_ai(keys)
        one = still_open(self.summary())[0]
        self.box.decide_findings(self.child_hash, ids=[one['key']], action='reject', reason='Checked the defect sheet myself: reason is recorded there.',
                                 evidence=['Defect sheet row 9'], by='reviewer@example.invalid', request_id='after-ai', expected_rev=self.child()['finding_decisions_rev'],
                                 expected_fps={one['key']: one['fp']}, expected_run=check_run(self.child()))
        s = self.summary()
        self.assertEqual((len(still_open(s)), s['reconcile']['kept_by_person'], s['reconcile']['set_aside_by_claude'], s['reconcile']['ai_fallback_unresolved']), (56, 1, 0, 56))

    def test_low_severity_findings_too(self):
        self.box = SpreadsheetInbox(tempfile.mkdtemp())                                  # an uncorrected copy: 57 medium and 2 low
        self.child_hash = self.box.ingest(NAME, workbook())['file_hash']
        s = self.summary()
        low = [i for i in still_open(s) if i['severity'] == 'low']
        self.assertEqual(len(low), 2)
        self.store_legacy_fallback([i['key'] for i in low], legacy=LEGACY_SOFT)
        self.with_ai([i['key'] for i in low])
        s = self.summary()
        self.assertEqual((s['counts']['low'], s['reconcile']['ai_fallback_unresolved'], s['reconcile']['set_aside_by_claude'], len(still_open(s))), (2, 2, 0, 59))


class TheReportedMix(unittest.TestCase):
    """The reported shape: 15 duplicate rows, 16 duplicate identifiers, 7 identifiers repeated at the source, 17 source
    rows missing from the report and 2 totals that differ, compared by day totals; one event sets all 57 aside."""

    def setUp(self):
        ident = 'EXAMPLE COMPANY'
        report = [(ident, 'PART-200', 36, 0, '', '')] * 16 + [(ident, 'PART-917', 12, 0, '', '')]
        extra = []
        for n in range(6):
            extra += [(f'LOT-{n:03d}', f'PART-{300 + n}', 20 + n, 0, '', ''), (f'LOT-{n:03d}', f'PART-{400 + n}', 40 + n, 0, '', '')]
        extra += [(f'BOX-{n:03d}', f'PART-{500 + n}', 50 + n, 0, '', '') for n in range(5)]
        self.box = SpreadsheetInbox(tempfile.mkdtemp())
        self.hash = self.box.ingest(NAME, build(rows=report))['file_hash']
        self.box.compare_source(self.hash, PROJECT + ' ' + DAY + '.csv', source_csv(report + extra))

    def item(self):
        return self.box.get(self.hash)

    def mix(self, issues):
        from collections import Counter
        return dict(Counter(i['code'] for i in real(issues)))

    EXPECTED = {'duplicate_row': 15, 'duplicate_identifier': 16, 'sharepoint_duplicate_identifier': 7, 'missing_from_report': 17, 'total_differs_from_sharepoint': 2}


    def test_one_event_sets_57_aside_and_all_57_are_read_as_unresolved(self):
        s = summarize(self.item())
        self.assertEqual(self.mix(s['issues']), self.EXPECTED)
        self.assertEqual((s['counts']['high'], s['counts']['medium'], s['status'], s['sharepoint']['status']), (31, 26, 'hold', 'day_level'))
        keys = [i['key'] for i in still_open(s)]

        def change(r):
            rv = self.box.review_summary(r)
            current = {x['key']: x for x in rv['issues'] + rv['dismissed']}
            review_decisions.decide(r, current, ids=keys, action='reject', reason='placeholder', by='reviewer@example.invalid', evidence=['placeholder'], request_id='one-event',
                                    expected_rev=int(r.get('finding_decisions_rev') or 0), expected_run=check_run(r), expected_fps={k: current[k]['fp'] for k in keys})
            for k in keys:
                r['finding_decisions'][k].update(reason=LEGACY_HARD[0], evidence=list(LEGACY_HARD[1]))
            r['finding_decision_log'][-1].update(reason=LEGACY_HARD[0], evidence=list(LEGACY_HARD[1]))
            r['ai_verify'] = {'status': 'done', 'run': check_run(r), 'verdicts': {k: {'verdict': 'not_an_issue', 'reason': 'Earlier AI said fine'} for k in keys}}
        self.box._update(self.hash, change)
        item = self.item()
        self.assertEqual(len(item['finding_decision_log']), 1)                              # a single event
        self.assertEqual(len(item['finding_decision_log'][0]['ids']), 57)
        self.assertTrue(all(v is None for v in item['finding_decision_log'][0]['prev'].values()))
        s = summarize(item)
        self.assertEqual(self.mix(still_open(s)), self.EXPECTED)
        r = s['reconcile']
        self.assertEqual((r['open'], r['kept_by_person'], r['set_aside_by_claude'], r['ai_fallback_unresolved']), (57, 0, 0, 57))
        self.assertTrue(r['adds_up'])
        self.assertEqual((s['counts']['high'], s['counts']['medium'], s['status']), (31, 26, 'hold'))
        from hosting import public_api
        api = public_api.report(item, inbox=self.box)
        self.assertEqual(self.mix(api['issues']), self.EXPECTED)
        listed = next(x for x in self.box.list()['spreadsheets'] if x['file_hash'] == self.hash)
        self.assertEqual((listed['review']['counts']['high'], listed['review']['counts']['medium'], listed['review']['status']), (31, 26, 'hold'))


class TheRule(unittest.TestCase):
    def test_what_counts_as_the_automatic_fallback(self):
        yes = [dict(action='reject', reason=LEGACY_HARD[0], evidence=list(LEGACY_HARD[1])), dict(action='reject', reason=LEGACY_SOFT[0]),
               dict(action='reject', reason=LEGACY_SOFT[0], evidence=[], carried_from='h', by='someone')]
        no = [dict(action='reject', reason='Checked against the source item', evidence=['SharePoint item 19']), dict(action='defer', reason=LEGACY_HARD[0]),
              dict(action='confirm', reason=LEGACY_SOFT[0]), dict(action='reject', reason='Dex was wrong; the value is correct', evidence=['Photo 3']),
              dict(action='reject', reason='x', evidence=['Left as written by Fix with AI']), dict(action='reject', reason='x', evidence='NOT CHECKED BY A PERSON'),
              dict(action='reject', reason='dex had no safe change'), {}, None]
        for d in yes:
            self.assertTrue(review_decisions.automated_fallback(d), d)
        for d in no:
            self.assertFalse(review_decisions.automated_fallback(d), d)

    def test_a_fallback_carried_to_a_later_version_settles_nothing_there_either(self):
        issue = dict(key='k', fp='f', record_key='rec', severity='medium', code='missing_defect_reason', text='t')
        carried = dict(action='reject', reason=LEGACY_HARD[0], evidence=LEGACY_HARD[1], fp='f', record_key='rec', carried_from='parent')
        open_, rejected, stale, counts = review_decisions.apply([dict(issue)], dict(finding_decisions={'k': carried}))
        self.assertEqual((len(open_), len(rejected), counts['ai_fallback_not_applied'], counts['rejected']), (1, 0, 1, 0))
        human = dict(carried, reason='Verified against the source record', evidence=['Item 7'])
        open_, rejected, stale, counts = review_decisions.apply([dict(issue)], dict(finding_decisions={'k': human}))
        self.assertEqual((len(open_), len(rejected), counts['ai_fallback_not_applied']), (0, 1, 0))


if __name__ == '__main__':
    unittest.main()

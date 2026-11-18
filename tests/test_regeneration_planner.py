"""Regeneration planner: one made-up source day that grew after its report was built (SYNTHETIC data only).

The shape mirrors a real observation without using any of it: a report was built when the day had 33 records and 1,226
parts inspected. The source later held 50 records, 2,611 inspected, 2,605 OK, with no NOK value on 48 of them, and 17
records that were not there before. The older report still says 33 / 1,226. Every client, job, site, list and
identifier here is invented. Nothing is read from a network and nothing is written outside a temporary folder.
"""
import copy
import hashlib
import io
import json
import sys
import tempfile
import unittest
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(Path(__file__).parent))
from hosting import regeneration_planner as rp, source_first as sf
from source_first_fixture import MAP, SCOPE, capture

CLIENT = 'synthetic-fixture-co'
PLAN_SCOPE = dict(client_id=CLIENT, **SCOPE)
TAKEN = datetime(2026, 11, 11, 15, 0, tzinfo=timezone.utc)
DATE_COLUMN = '44444444-4444-4444-4444-444444444444'


def record(n, inspected, rejected=None, shift='1', comment=''):
    raw = dict(date='2026-11-10', shift=shift, location='Line 9', part='00009900', identifier=f'SYN{n:07d}', inspected_for='Surface',
               inspected=inspected, condition='NOK' if rejected else 'OK', reject_reason='Scratch' if rejected else None,
               rejected=rejected, certified=inspected - (rejected or 0), comments=comment)
    return dict(id=str(n), version='"1,1"', modified='2026-11-10T12:00:00Z', day='2026-11-10', date_basis='business_date',
                raw={MAP['fields'][k]: v for k, v in raw.items()})


def early_rows():      # 33 records, 1,226 inspected
    return [record(1, 37, rejected=2)] + [record(n, 37) for n in range(2, 33)] + [record(33, 42)]


def late_rows():       # 17 more records: 50 in all, 2,611 inspected, 2,605 OK, a NOK value on 2 records only
    return early_rows() + [record(34, 81, rejected=4)] + [record(n, 81) for n in range(35, 50)] + [record(50, 89)]


def sealed(rows, taken=TAKEN, evidence_age=30, contract='default', **change):
    c = capture(rows)
    c = {k: v for k, v in c.items() if k not in ('snapshot_hash', 'envelope_hash')}
    c['captured_at'] = taken.isoformat()
    since = taken - timedelta(seconds=evidence_age)
    c['date_contract'] = dict(site_id=SCOPE['site_id'], list_id=SCOPE['list_id'], field='Date', format='dateOnly', column_id=DATE_COLUMN,
                              zone='America/Chicago', captured_at=since.isoformat(), expires_at=(since + timedelta(seconds=600)).isoformat()) if contract == 'default' else contract
    if isinstance(c['date_contract'], dict):
        c['date_contract'].update(change)
    return sf.seal(c)


def assignment(**over):
    return dict(dict(verified=True, active=True, client_id=CLIENT, site_id=SCOPE['site_id'], list_id=SCOPE['list_id'], job=SCOPE['job'],
                     business_date=SCOPE['business_date'], zone='America/Chicago', recorded_shifts=['1'], mapping=copy.deepcopy(MAP), header={}), **over)


OLD_REPORT = dict(file_hash='a' * 64, kind='original', filename='synthetic-day.xlsx', rows=33, inspected=1226, **PLAN_SCOPE)


class TheGrownDay(unittest.TestCase):
    def test_the_fixture_is_the_shape_described(self):
        rows, _ = sf.reviewed_rows(sealed(late_rows()), MAP, [])
        self.assertEqual((len(early_rows()), sum(r['raw']['Inspected'] for r in early_rows())), (33, 1226))
        self.assertEqual((len(rows), sum(r['resolved']['inspected'] for r in rows), sum(r['resolved']['certified'] for r in rows)), (50, 2611, 2605))
        self.assertEqual(sum(1 for r in rows if r['resolved']['rejected'] is None), 48)

    def test_an_older_report_with_no_source_binding_is_out_of_date_and_kept(self):
        before = copy.deepcopy(OLD_REPORT)
        plan = rp.plan([assignment()], PLAN_SCOPE, sealed(late_rows()), existing=[OLD_REPORT], previous_capture=sealed(early_rows(), taken=TAKEN - timedelta(hours=20)))
        self.assertEqual((plan['status'], plan['reason'], plan['blockers']), ('draft_proposed', 'existing_report_not_bound_to_source', []))
        self.assertEqual(plan['rows']['in_capture'], 50); self.assertEqual(plan['rows']['in_draft'], 50)
        self.assertEqual(plan['changes']['counts'], dict(added=17, changed=0, removed=0))
        self.assertEqual(plan['changes']['added'], sorted(str(n) for n in range(34, 51)))
        self.assertIn('not a scan time', plan['changes']['meaning'])
        old = plan['existing_versions']
        self.assertEqual([(v['file_hash'], v['kind'], v['action'], v['relation']) for v in old], [('a' * 64, 'original', 'keep_unchanged', 'not_bound_to_a_source_capture')])
        self.assertEqual(old[0]['differs'], '33 row(s) in that version, 50 in this capture')
        self.assertTrue(plan['never_overwrites']); self.assertTrue(plan['writes_nothing'])
        self.assertEqual((plan['day_completion'], plan['client_release'], plan['automated_completion']), ('not_established', 'held', 'not_claimed'))
        self.assertEqual(OLD_REPORT, before)

    def test_missing_nok_is_unknown_and_never_zero(self):
        plan = rp.plan([assignment()], PLAN_SCOPE, sealed(late_rows()))
        q = plan['quantities']
        self.assertEqual((q['inspected']['known_sum'], q['inspected']['complete']), (2611, True))
        self.assertEqual((q['certified']['known_sum'], q['certified']['complete']), (2605, True))
        self.assertEqual((q['rejected']['known_sum'], q['rejected']['known_rows'], q['rejected']['unknown_rows'], q['rejected']['complete']), (6, 2, 48, False))
        self.assertIn('unknown, not zero', q['rejected']['note'])
        self.assertTrue(any(h['code'] == 'unknown_quantities' for h in plan['holds']))
        self.assertEqual(plan['findings']['by_code']['missing_value'], 48)

    def test_no_report_yet_is_a_missing_draft_and_changes_are_not_guessed(self):
        plan = rp.plan([assignment()], PLAN_SCOPE, sealed(late_rows()))
        self.assertEqual((plan['status'], plan['reason'], plan['existing_versions']), ('draft_proposed', 'missing_draft', []))
        self.assertEqual(plan['changes']['basis'], 'not_determined'); self.assertNotIn('added', plan['changes'])

    def test_changed_and_removed_records_are_named(self):
        early = early_rows()
        later = [record(1, 37, rejected=3)] + early[1:31] + [record(60, 10)]          # one changed, two removed, one added
        plan = rp.plan([assignment()], PLAN_SCOPE, sealed(later), previous_capture=sealed(early, taken=TAKEN - timedelta(hours=2)))
        self.assertEqual((plan['changes']['changed'], plan['changes']['removed'], plan['changes']['added']), (['1'], ['32', '33'], ['60']))

    def test_the_binding_ties_a_plan_to_its_capture_mapping_template_and_rows(self):
        cap = sealed(late_rows())
        plan = rp.plan([assignment()], PLAN_SCOPE, cap)
        self.assertEqual(plan['source']['snapshot_hash'], cap['snapshot_hash']); self.assertEqual(plan['source']['captured_at'], TAKEN.isoformat())
        self.assertEqual(plan, rp.plan([assignment()], PLAN_SCOPE, sealed(late_rows())))               # the same inputs give the same plan
        hashes = {plan['report_input_hash'],
                  rp.plan([assignment()], PLAN_SCOPE, sealed(late_rows()[:-1]))['report_input_hash'],                          # other records
                  rp.plan([assignment(mapping=dict(MAP, shifts={'1': '2nd Shift'}))], PLAN_SCOPE, cap)['report_input_hash'],  # other mapping
                  rp.plan([assignment()], PLAN_SCOPE, cap, header=dict(area='Dock 4', evidence='Stated by the made-up supervisor'))['report_input_hash']}
        self.assertEqual(len(hashes), 4)
        later = rp.plan([assignment()], PLAN_SCOPE, sealed(late_rows(), taken=TAKEN + timedelta(hours=3)))
        self.assertEqual(later['source']['snapshot_hash'], plan['source']['snapshot_hash'])             # the same content, read again later
        self.assertEqual(later['report_input_hash'], plan['report_input_hash'])                         # is the same report input
        self.assertNotEqual(later['source']['captured_at'], plan['source']['captured_at'])

    def test_a_draft_already_built_from_exactly_this_input_is_current(self):
        cap = sealed(late_rows())
        first = rp.plan([assignment()], PLAN_SCOPE, cap, existing=[OLD_REPORT])
        draft = dict(file_hash='d' * 64, kind='draft', report_input_hash=first['report_input_hash'], source_snapshot_hash=cap['snapshot_hash'], rows=50, **PLAN_SCOPE)
        again = rp.plan([assignment()], PLAN_SCOPE, cap, existing=[OLD_REPORT, draft])
        self.assertEqual((again['status'], again['reason'], again['proposal']['allowed'], again['proposal']['operation_id']), ('no_change', 'draft_current', False, None))
        grown = rp.plan([assignment()], PLAN_SCOPE, sealed(late_rows() + [record(51, 5)]), existing=[OLD_REPORT, draft])
        self.assertEqual((grown['status'], grown['reason']), ('draft_proposed', 'stale_draft'))
        self.assertEqual([v['relation'] for v in grown['existing_versions']], ['not_bound_to_a_source_capture', 'earlier_or_other_input'])
        elsewhere = dict(draft, job='SYNTHETIC-9999')
        self.assertEqual(rp.plan([assignment()], PLAN_SCOPE, cap, existing=[elsewhere])['reason'], 'missing_draft')   # another job's draft is not this one's

    def test_approved_and_sent_versions_are_listed_and_never_replaced(self):
        cap = sealed(late_rows())
        versions = [dict(file_hash=c * 64, kind=k, rows=33, **PLAN_SCOPE) for c, k in zip('abce', ('original', 'corrected', 'approved', 'sent'))]
        before = copy.deepcopy(versions)
        plan = rp.plan([assignment()], PLAN_SCOPE, cap, existing=versions)
        self.assertEqual([(v['kind'], v['action']) for v in plan['existing_versions']], [(k, 'keep_unchanged') for k in ('original', 'corrected', 'approved', 'sent')])
        self.assertEqual(plan['proposal']['kind'], 'new_held_draft'); self.assertEqual(versions, before)


class Shifts(unittest.TestCase):
    def test_an_unknown_shift_blocks_completion_but_the_known_rows_still_get_a_draft(self):
        rows = late_rows()[:40] + [record(n, 81, shift='B') for n in range(41, 48)] + [record(48, 81, shift=None), record(49, 81), record(50, 89)]
        plan = rp.plan([assignment()], PLAN_SCOPE, sealed(rows))
        self.assertEqual(plan['status'], 'draft_proposed'); self.assertTrue(plan['proposal']['allowed'])
        self.assertEqual(plan['automated_completion'], 'blocked_by_unassigned_shift')
        self.assertEqual((plan['rows']['in_capture'], plan['rows']['in_draft'], plan['rows']['unassigned']), (50, 42, 8))
        self.assertEqual(plan['rows']['unassigned_shifts'], {'B': 7, '(blank)': 1})
        self.assertEqual(plan['rows']['unassigned_source_ids'], sorted(str(n) for n in range(41, 49)))
        hold = next(h for h in plan['holds'] if h['code'] == 'unassigned_shift')
        self.assertIn('never worked out from a time', hold['text']); self.assertIn('in NO draft', hold['text'])
        self.assertEqual(plan['quantities']['inspected']['known_sum'], sum(r['raw']['Inspected'] for r in rows if r['raw']['Shift'] == '1'))

    def test_a_shift_the_assignment_was_not_verified_for_is_not_reported_under_it(self):
        rows = late_rows()[:48] + [record(49, 81, shift='2'), record(50, 89, shift='2')]     # shift 2 has a mapping, but this assignment covers shift 1
        plan = rp.plan([assignment()], PLAN_SCOPE, sealed(rows))
        self.assertEqual((plan['rows']['in_draft'], plan['rows']['unassigned_shifts']), (48, {'2': 2}))

    def test_a_day_with_no_usable_shift_proposes_nothing(self):
        plan = rp.plan([assignment()], PLAN_SCOPE, sealed([record(n, 10, shift='night') for n in range(1, 4)]))
        self.assertEqual((plan['status'], plan['proposal']['allowed'], [b['code'] for b in plan['blockers']]), ('blocked', False, ['nothing_to_draft']))
        self.assertEqual(plan['rows']['unassigned_shifts'], {'night': 3})

    def test_a_clock_time_in_the_record_does_not_assign_a_shift(self):
        rows = [record(1, 10, shift=None), record(2, 10)]
        rows[0]['modified'] = '2026-11-10T23:45:00Z'; rows[0]['raw']['Comments'] = 'scanned 23:45 third shift'
        plan = rp.plan([assignment()], PLAN_SCOPE, sealed(rows))
        self.assertEqual((plan['rows']['in_draft'], plan['rows']['unassigned_shifts']), (1, {'(blank)': 1}))


class Refusals(unittest.TestCase):
    def blocked(self, assignments=None, scope=None, cap=None, **more):
        plan = rp.plan([assignment()] if assignments is None else assignments, PLAN_SCOPE if scope is None else scope, sealed(late_rows()) if cap is None else cap, **more)
        self.assertEqual((plan['status'], plan['proposal']['allowed']), ('blocked', False)); self.assertTrue(plan['writes_nothing'])
        self.assertNotIn('report_input_hash', plan)
        return [b['code'] for b in plan['blockers']]

    def test_mapping_must_be_exact_verified_and_single(self):
        self.assertEqual(self.blocked([assignment(verified=False)]), ['assignment_not_verified'])
        self.assertEqual(self.blocked([assignment(verified='yes')]), ['assignment_not_verified'])
        self.assertEqual(self.blocked([assignment(active=False)]), ['assignment_not_active'])
        self.assertEqual(self.blocked([]), ['scope_not_assigned'])
        self.assertEqual(self.blocked([assignment(list_id='99999999-9999-9999-9999-999999999999')]), ['scope_not_assigned'])
        self.assertEqual(self.blocked(scope=dict(PLAN_SCOPE, job=' SYNTHETIC-1234')), ['scope_incomplete'])
        self.assertEqual(self.blocked(scope={k: v for k, v in PLAN_SCOPE.items() if k != 'client_id'}), ['scope_incomplete'])
        self.assertEqual(self.blocked([assignment(zone=None)]), ['zone_missing'])
        self.assertEqual(self.blocked([assignment(mapping=dict(MAP, fields=dict(MAP['fields'], inspected='NoSuchColumn')))]), ['mapping_not_usable'])

    def test_one_logical_job_cannot_be_reported_twice(self):
        self.assertEqual(self.blocked([assignment(), assignment()]), ['duplicate_logical_job'])
        other_list = assignment(list_id='99999999-9999-9999-9999-999999999999')
        self.assertEqual(self.blocked([assignment(), other_list]), ['duplicate_logical_job'])            # the same job and day through another list
        ok = rp.plan([assignment(), assignment(job='SYNTHETIC-5678'), assignment(business_date='2026-11-11')], PLAN_SCOPE, sealed(late_rows()))
        self.assertEqual(ok['status'], 'draft_proposed')                                                # other jobs and other days are not duplicates

    def test_the_capture_must_be_this_scope_sealed_and_unchanged(self):
        other = capture(late_rows()); other = {k: v for k, v in other.items() if k not in ('snapshot_hash', 'envelope_hash')}
        other['scope'] = dict(other['scope'], job='SYNTHETIC-5678')
        self.assertEqual(self.blocked(cap=sf.seal(other)), ['capture_scope_mismatch'])
        tampered = sealed(late_rows()); tampered['records'][0]['raw']['Inspected'] = 999
        self.assertEqual(self.blocked(cap=tampered), ['capture_not_sealed'])
        self.assertEqual(self.blocked(cap={'scope': SCOPE, 'records': []}), ['capture_not_sealed'])
        uncertain = {k: v for k, v in sealed(late_rows()).items() if k not in ('snapshot_hash', 'envelope_hash')}; uncertain['uncertain_dates'] = [{'id': '77'}]
        self.assertEqual(self.blocked(cap=sf.seal(uncertain)), ['uncertain_dates'])
        self.assertEqual(self.blocked(previous_capture={'not': 'sealed'}), ['earlier_capture_not_usable'])

    def test_date_evidence_must_have_been_current_when_the_capture_was_taken(self):
        self.assertEqual(self.blocked(cap=sealed(late_rows(), contract=None)), ['date_evidence_missing'])
        self.assertEqual(self.blocked(cap=sealed(late_rows(), evidence_age=601)), ['date_evidence_not_current'])        # one second past the limit
        self.assertEqual(self.blocked(cap=sealed(late_rows(), evidence_age=-5)), ['date_evidence_not_current'])         # evidence dated after the capture
        self.assertEqual(rp.plan([assignment()], PLAN_SCOPE, sealed(late_rows(), evidence_age=599))['status'], 'draft_proposed')
        long = (TAKEN - timedelta(seconds=30) + timedelta(seconds=3600)).isoformat()
        self.assertEqual(self.blocked(cap=sealed(late_rows(), expires_at=long)), ['date_evidence_not_current'])         # a stretched validity is not accepted
        for change in (dict(column_id='55555555-5555-5555-5555-555555555555'), dict(zone='America/New_York'), dict(format='dateTime'),
                       dict(site_id='elsewhere'), dict(list_id='elsewhere'), dict(captured_at='2026-11-11T14:59:30'), dict(column_id=None)):
            with self.subTest(change=change):
                self.assertEqual(self.blocked(cap=sealed(late_rows(), **change)), ['date_evidence_not_current'])
        text = rp.plan([assignment()], PLAN_SCOPE, sealed(late_rows(), evidence_age=900))['blockers'][0]['text']
        self.assertIn('not extended', text)

    def test_evidence_is_judged_at_the_capture_and_a_later_clock_changes_nothing(self):
        cap = sealed(late_rows(), taken=datetime(2026, 1, 5, 9, 0, tzinfo=timezone.utc))      # an old frozen run, planned long afterwards
        plan = rp.plan([assignment()], PLAN_SCOPE, cap)
        self.assertEqual(plan['status'], 'draft_proposed'); self.assertEqual(plan['source']['date_evidence']['judged_at'], 'capture')
        self.assertIn('not a live reading', ' '.join(plan['notices']))

    def test_pratt_and_whitney_is_refused_by_name_wherever_it_appears(self):
        for scope in (dict(PLAN_SCOPE, client_id='pratt-whitney'), dict(PLAN_SCOPE, job='PRATT-2207'), dict(PLAN_SCOPE, client_id='P&W Columbus'),
                      dict(PLAN_SCOPE, job='PW-1180'), dict(PLAN_SCOPE, client_id='Whitney Aero')):
            with self.subTest(scope=scope['client_id'] + ' / ' + scope['job']):
                self.assertEqual(self.blocked([assignment(client_id=scope['client_id'], job=scope['job'])], scope=scope), ['excluded_scope'])
        self.assertEqual(self.blocked([assignment(client_name='Pratt & Whitney')]), ['excluded_scope'])

    def test_a_malformed_request_is_an_error_not_a_plan(self):
        with self.assertRaises(rp.PlanError):
            rp.plan(None, PLAN_SCOPE, sealed(late_rows()))
        with self.assertRaises(rp.PlanError):
            rp.plan([assignment()], PLAN_SCOPE, sealed(late_rows()), existing=[dict(file_hash='a' * 64, kind='final', **PLAN_SCOPE)])


class ProposedDraft(unittest.TestCase):
    def test_the_proposal_is_a_new_local_draft_in_the_selected_template_with_its_checklist(self):
        out = Path(tempfile.mkdtemp()).resolve()
        old = out / 'existing' / 'synthetic-day.xlsx'; old.parent.mkdir(); old.write_bytes(b'the older report, 33 rows')
        digest = hashlib.sha256(old.read_bytes()).hexdigest()
        cap = sealed(late_rows()[:40] + [record(n, 81, shift='B') for n in range(41, 51)])
        args = ([assignment()], PLAN_SCOPE, cap)
        existing = [dict(OLD_REPORT, file_hash=digest)]
        plan = rp.plan(*args, existing=existing)
        written = rp.propose(plan, *args, out / 'proposed', existing=existing)
        folder = Path(written['written']['folder'])
        self.assertEqual(folder.parent, out / 'proposed'); self.assertEqual(folder.name, plan['proposal']['operation_id'])
        self.assertEqual(sorted(p.name for p in folder.iterdir()), ['REVIEW-FIRST.txt', 'checklist.json', 'checklist.txt', 'draft.xlsx', 'receipt.json', 'regeneration-plan.json'])
        self.assertEqual(sorted(written['written']['files']), sorted(p.name for p in folder.iterdir()))
        review = (folder / 'REVIEW-FIRST.txt').read_text()
        self.assertIn('10 row(s) of this day have a recorded shift with no approved mapping', review); self.assertIn('unknown', review)
        checklist = json.loads((folder / 'checklist.json').read_text())
        self.assertEqual((checklist['status'], checklist['client_release'], checklist['day_completion']), ('DRAFT', 'held', 'not_established'))
        self.assertEqual(checklist['source_snapshot_hash'], cap['snapshot_hash']); self.assertEqual(checklist['captured_at'], cap['captured_at'])
        self.assertEqual(checklist['source_ids'], plan['rows']['source_ids']); self.assertEqual(len(checklist['source_ids']), 40)
        self.assertEqual(checklist['unassigned_shifts'], {'B': 10})
        self.assertEqual(sum(1 for f in checklist['findings'] if f['code'] == 'missing_value'), 38)     # unknown NOK, each one named
        self.assertEqual(hashlib.sha256((folder / 'draft.xlsx').read_bytes()).hexdigest(), checklist['workbook_sha256'])
        note = (folder / 'checklist.txt').read_text()
        self.assertIn('not approved for client release', note); self.assertIn('10 row(s) with shift "B"', note)
        saved = json.loads((folder / 'regeneration-plan.json').read_text())
        self.assertEqual((saved['report_input_hash'], saved['existing_versions'][0]['action']), (plan['report_input_hash'], 'keep_unchanged'))
        # the template really is the selected daily one, populated: it opens, and holds the source rows
        from hosting import source_first_workbook as wb
        self.assertEqual(saved['template']['sha256'], wb.contract()['clean_sha256'])
        sheets = zipfile.ZipFile(io.BytesIO((folder / 'draft.xlsx').read_bytes())).namelist()
        self.assertTrue(any(n.startswith('xl/worksheets/') for n in sheets))
        # the older report is exactly as it was
        self.assertEqual(hashlib.sha256(old.read_bytes()).hexdigest(), digest); self.assertEqual(sorted(p.name for p in old.parent.iterdir()), ['synthetic-day.xlsx'])

    def test_nothing_is_ever_written_twice_or_for_another_input(self):
        out = Path(tempfile.mkdtemp()).resolve() / 'proposed'
        cap = sealed(late_rows()); args = ([assignment()], PLAN_SCOPE, cap)
        plan = rp.plan(*args)
        first = rp.propose(plan, *args, out)
        before = {p.name: p.read_bytes() for p in Path(first['written']['folder']).iterdir()}
        with self.assertRaises(rp.PlanError):
            rp.propose(plan, *args, out)                                                        # the same proposal again
        self.assertEqual({p.name: p.read_bytes() for p in Path(first['written']['folder']).iterdir()}, before)
        other = sealed(late_rows() + [record(51, 5)])
        with self.assertRaises(rp.PlanError):
            rp.propose(plan, [assignment()], PLAN_SCOPE, other, out)                            # a plan made for another capture
        with self.assertRaises(rp.PlanError):
            rp.propose(dict(plan, report_input_hash='0' * 64), *args, out)
        with self.assertRaises(rp.PlanError):
            rp.propose(plan, *args, 'relative/folder')
        self.assertEqual([p.name for p in out.iterdir()], [plan['proposal']['operation_id']])

    def test_a_plan_that_proposes_nothing_writes_nothing(self):
        out = Path(tempfile.mkdtemp()).resolve() / 'proposed'
        cap = sealed(late_rows())
        for assignments in ([assignment(verified=False)], [assignment(), assignment()]):
            plan = rp.plan(assignments, PLAN_SCOPE, cap)
            with self.assertRaises(rp.PlanError):
                rp.propose(plan, assignments, PLAN_SCOPE, cap, out)
        current = rp.plan([assignment()], PLAN_SCOPE, cap)
        draft = dict(file_hash='d' * 64, kind='draft', report_input_hash=current['report_input_hash'], **PLAN_SCOPE)
        with self.assertRaises(rp.PlanError):
            rp.propose(rp.plan([assignment()], PLAN_SCOPE, cap, existing=[draft]), [assignment()], PLAN_SCOPE, cap, out, existing=[draft])
        self.assertFalse(out.exists())


class ProposedWorkbookCells(unittest.TestCase):
    """What is actually inside the proposed workbook, cell by cell: 40 rows on shift 1, 2 on shift 2, 8 with an
    unapproved shift "B" that must be in no sheet, and a NOK value on two records only."""
    @classmethod
    def setUpClass(cls):
        from openpyxl import load_workbook
        cls.out = Path(tempfile.mkdtemp()).resolve()
        cls.rows = late_rows()[:40] + [record(n, 81, shift='B') for n in range(41, 49)] + [record(49, 81, shift='2'), record(50, 89, shift='2')]
        cls.assignments = [assignment(recorded_shifts=['1', '2'])]
        cls.cap = sealed(cls.rows)
        cls.plan = rp.plan(cls.assignments, PLAN_SCOPE, cls.cap)
        cls.written = rp.propose(cls.plan, cls.assignments, PLAN_SCOPE, cls.cap, cls.out)
        cls.raw = (Path(cls.written['written']['folder']) / 'draft.xlsx').read_bytes()
        cls.book = load_workbook(io.BytesIO(cls.raw))

    def detail(self, sheet):
        ws, out = self.book[sheet], []
        self.assertEqual([c.value for c in ws[15]][:13], ['Date', 'Shift', 'Location', 'Part Number', 'Identifier', 'Inspected For', 'Quantity Inspected',
                                                           'Condition', 'Reject Reason', 'Reject Quantity', 'Quantity Certified', 'Comments', 'Defect Photo'])
        for row in ws.iter_rows(min_row=16):
            if isinstance(row[4].value, str) and row[4].value.startswith('SYN'):
                out.append(row)
        return out

    def test_each_recorded_shift_has_its_own_rows_and_no_other(self):
        first, second, third = self.detail('1st Shift'), self.detail('2nd Shift'), self.detail('3rd Shift')
        self.assertEqual([r[4].value for r in first], [f'SYN{n:07d}' for n in range(1, 41)])
        self.assertEqual([r[4].value for r in second], ['SYN0000049', 'SYN0000050'])
        self.assertEqual(third, [])
        self.assertEqual({r[1].value for r in first}, {'1'}); self.assertEqual({r[1].value for r in second}, {'2'})    # the shift as recorded
        self.assertEqual(self.book['1st Shift']['E10'].value, '1st'); self.assertEqual(self.book['2nd Shift']['E10'].value, '2nd')

    def test_rows_with_an_unapproved_shift_are_in_no_sheet_at_all(self):
        everything = ' '.join(str(c.value) for ws in self.book for row in ws.iter_rows() for c in row if c.value is not None)
        for n in range(41, 49):
            self.assertNotIn(f'SYN{n:07d}', everything)
        self.assertNotIn('"B"', everything)
        checklist = json.loads((Path(self.written['written']['folder']) / 'checklist.json').read_text())
        self.assertEqual(checklist['unassigned_shifts'], {'B': 8})                                    # they are named in the checklist instead
        self.assertEqual(self.plan['rows']['unassigned_source_ids'], [str(n) for n in range(41, 49)])

    def test_quantities_are_the_source_values_and_a_missing_nok_is_an_empty_cell(self):
        rows = self.detail('1st Shift') + self.detail('2nd Shift')
        source = {r['raw']['Identifier']: r['raw'] for r in self.rows}
        for row in rows:
            raw = source[row[4].value]
            self.assertEqual((row[6].value, row[10].value), (raw['Inspected'], raw['Certified']), row[4].value)
            self.assertEqual(row[9].value, raw['Rejected'], row[4].value)                             # 2, 4, or nothing at all
            self.assertIsNot(row[9].value, 0)
        self.assertEqual(sum(1 for r in rows if r[9].value is None), 40); self.assertEqual(sorted(r[9].value for r in rows if r[9].value is not None), [2, 4])

    def test_totals_that_need_an_unknown_value_are_withheld_and_known_ones_are_shown(self):
        s = self.book['Summary']
        self.assertEqual(s['B5'].value, 'DRAFT · UNKNOWN QUANTITIES')
        self.assertEqual((s['B9'].value, s['D9'].value, s['G9'].value), (42, 1963, 1957))             # records, inspected, certified
        self.assertIsNone(s['J9'].value)                                                              # parts rejected: withheld, not 0 and not 6
        self.assertIn(s['M9'].value, (None, ''))                                                      # defect rate: withheld
        self.assertEqual((s['F21'].value, s['G21'].value, s['I21'].value, s['K21'].value), (40, 1793, 1787, None))
        self.assertEqual((s['F22'].value, s['G22'].value, s['I22'].value, s['K22'].value), (2, 170, 170, None))
        self.assertEqual((s['D21'].value, s['D22'].value, s['D23'].value), ('Recorded', 'Recorded', 'No recorded rows'))
        self.assertEqual((s['F24'].value, s['G24'].value, s['I24'].value, s['K24'].value), (42, 1963, 1957, None))
        self.assertIn('UNKNOWN, not zero', s['B25'].value)
        first = self.book['1st Shift']
        totals = next(r for r in first.iter_rows(min_row=16) if r[0].value == 'Records inspected')[0].row + 1
        self.assertEqual((first.cell(totals, 1).value, first.cell(totals, 3).value, first.cell(totals, 5).value, first.cell(totals, 7).value), (40, 1793, 1787, None))
        self.assertEqual((self.plan['quantities']['inspected']['known_sum'], self.plan['quantities']['certified']['known_sum']), (1963, 1957))
        self.assertEqual((self.plan['quantities']['rejected']['known_sum'], self.plan['quantities']['rejected']['unknown_rows']), (6, 40))
        formulas = [c.coordinate for ws in self.book for row in ws.iter_rows() for c in row if isinstance(c.value, str) and c.value.startswith('=')]
        self.assertEqual(formulas, [])                                                                # nothing left for Excel to turn an unknown into 0

    def test_every_row_is_tied_to_its_source_record_version_and_capture(self):
        src = self.book['_Source']
        self.assertEqual(src.sheet_state, 'hidden')
        head = [c.value for c in src[1]]
        self.assertEqual(head[:4], ['source_record_id', 'source_version', 'source_record_hash', 'snapshot_hash'])
        rows = [dict(zip(head, [c.value for c in r])) for r in src.iter_rows(min_row=2) if r[0].value is not None]
        self.assertEqual(sorted(r['source_record_id'] for r in rows), sorted(self.plan['rows']['source_ids']))
        self.assertEqual(len(rows), 42)
        records = {r['id']: r for r in self.cap['records']}
        for r in rows:
            self.assertEqual(r['source_version'], records[r['source_record_id']]['version'])
            self.assertEqual(r['source_record_hash'], sf.digest(records[r['source_record_id']]))
            self.assertEqual(r['snapshot_hash'], self.written['source']['draft_view_snapshot_hash'])
        unknown = [r for r in rows if r['rejected_original_type'] in ('null', 'absent')]
        self.assertEqual(len(unknown), 40)
        self.assertTrue(all(r['rejected_approved_value'] in (None, '') for r in unknown))             # the ledger keeps it missing too
        rejects = self.book['Rejects & Photos']
        self.assertEqual(rejects['I7'].value, 'Source record ID')
        self.assertEqual(sorted(str(rejects.cell(r, 9).value) for r in (8, 9)), ['1', '34'])          # the two known rejects, by source ID
        self.assertIn(self.written['source']['draft_view_snapshot_hash'][:16], self.book['Summary']['B33'].value)

    def test_no_source_link_is_invented_when_the_capture_has_none(self):
        import re
        everything = ' '.join(str(c.value) for ws in self.book for row in ws.iter_rows() for c in row if c.value is not None)
        self.assertEqual(re.findall(r'https?://', everything), [])
        self.assertEqual(self.plan['source']['record_links'], 'not_in_capture')

    def test_template_sheets_logo_and_chart_parts_are_the_selected_templates(self):
        from hosting import source_first_workbook as wb
        contract = wb.contract()
        self.assertEqual(self.book.sheetnames, ['Summary', '1st Shift', '2nd Shift', '3rd Shift', 'Rejects & Photos', '_Calc', '_Source'])
        self.assertEqual(self.book['_Calc'].sheet_state, 'hidden')
        with zipfile.ZipFile(io.BytesIO(self.raw)) as draft, zipfile.ZipFile(wb.ASSETS / contract['clean_filename']) as clean:
            media = {n: hashlib.sha256(draft.read(n)).hexdigest() for n in draft.namelist() if n.startswith('xl/media/')}
            self.assertEqual(sorted(media.values()), sorted(contract['preserve_media_sha256']))       # the logos, byte for byte, and no sample photo
            for image in contract['branding_images']:
                self.assertEqual(media[image['part']], image['sha256'])
            for part in ('xl/charts/chart1.xml', 'xl/theme/theme1.xml'):
                if part in clean.namelist():
                    self.assertIn(part, draft.namelist())
            self.assertEqual(draft.read('xl/theme/theme1.xml'), clean.read('xl/theme/theme1.xml'))
        self.assertEqual(self.written['template']['selected_sha256'], contract['selected_sha256'])

    @unittest.skipUnless(__import__('os').environ.get('PPG_TEMPLATE_DIR'), 'set PPG_TEMPLATE_DIR to the folder holding the selected Daily template (read only)')
    def test_against_the_selected_daily_template_file_itself(self):
        """Read-only check against the real template on this machine: the pinned template is that file, and the draft
        carries its logos and its sheets. The file is never written to and is not in the repository."""
        import os
        from openpyxl import load_workbook
        from hosting import source_first_workbook as wb
        path = Path(os.environ['PPG_TEMPLATE_DIR']) / 'PPG-Quality-Daily-End-of-Shift-Report-No-Start-Here.xlsx'
        raw = path.read_bytes(); digest = hashlib.sha256(raw).hexdigest()
        self.assertEqual(wb.contract()['selected_sha256'], digest)
        with zipfile.ZipFile(io.BytesIO(raw)) as real, zipfile.ZipFile(io.BytesIO(self.raw)) as draft:
            real_media = {hashlib.sha256(real.read(n)).hexdigest() for n in real.namelist() if n.startswith('xl/media/')}
            draft_media = {hashlib.sha256(draft.read(n)).hexdigest() for n in draft.namelist() if n.startswith('xl/media/')}
        self.assertTrue(draft_media and draft_media <= real_media)                                    # only the template's own images, none added
        self.assertEqual(len(real_media - draft_media), 11)                                           # its sample defect photos are not carried over
        self.assertEqual([s for s in self.book.sheetnames if s != '_Source'], load_workbook(io.BytesIO(raw), read_only=True).sheetnames)
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), digest)


class InputsThatChangeTheReport(unittest.TestCase):
    def test_another_header_or_template_is_another_report_input_and_an_old_plan_cannot_be_written(self):
        from unittest import mock
        from hosting import source_first_workbook as wb
        out = Path(tempfile.mkdtemp()).resolve() / 'proposed'
        cap = sealed(late_rows()); args = ([assignment()], PLAN_SCOPE, cap)
        plan = rp.plan(*args)
        header = dict(paying_client='Synthetic Fixture Co', evidence='Stated by the made-up coordinator')
        with_header = rp.plan(*args, header=header)
        self.assertNotEqual(with_header['report_input_hash'], plan['report_input_hash'])
        self.assertNotEqual(with_header['proposal']['operation_id'], plan['proposal']['operation_id'])
        with self.assertRaises(rp.PlanError):
            rp.propose(plan, *args, out, header=header)                                               # a plan made without that header
        real = wb.contract()
        for change in (dict(clean_sha256='0' * 64), dict(selected_sha256='1' * 64), dict(version='another-contract')):
            with self.subTest(change=list(change)[0]), mock.patch.object(wb, 'contract', return_value=dict(real, **change)):
                other = rp.plan(*args)
                self.assertNotEqual(other['report_input_hash'], plan['report_input_hash'])
                with self.assertRaises(rp.PlanError):
                    rp.propose(plan, *args, out)                                                      # a plan made for the other template
        with mock.patch.object(wb, 'generator_hash', return_value='2' * 64):
            self.assertNotEqual(rp.plan(*args)['report_input_hash'], plan['report_input_hash'])       # another version of the writer
        self.assertFalse(out.exists())
        draft = dict(file_hash='d' * 64, kind='draft', report_input_hash=plan['report_input_hash'], source_snapshot_hash=cap['snapshot_hash'], **PLAN_SCOPE)
        stale = rp.plan(*args, existing=[draft], header=header)
        self.assertEqual((stale['status'], stale['reason'], stale['existing_versions'][0]['relation']), ('draft_proposed', 'stale_draft', 'same_source_other_settings'))

    def test_the_same_capture_twice_writes_once_and_protected_files_are_untouched(self):
        root = Path(tempfile.mkdtemp()).resolve()
        protected = {}
        for kind in ('original', 'corrected', 'approved', 'sent'):
            f = root / 'reports' / f'synthetic-day-{kind}.xlsx'; f.parent.mkdir(exist_ok=True); f.write_bytes(('saved ' + kind).encode())
            protected[f] = (hashlib.sha256(f.read_bytes()).hexdigest(), f.stat().st_mtime_ns)
        existing = [dict(file_hash=h, kind=f.stem.rsplit('-', 1)[1], rows=33, **PLAN_SCOPE) for f, (h, _) in protected.items()]
        cap = sealed(late_rows()); args = ([assignment()], PLAN_SCOPE, cap)
        plan = rp.plan(*args, existing=existing)
        first = rp.propose(plan, *args, root / 'proposed', existing=existing)
        folder = Path(first['written']['folder'])
        before = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in folder.iterdir()}
        same_again = sealed(late_rows())                                                              # the same source read a second time
        again = rp.plan([assignment()], PLAN_SCOPE, same_again, existing=existing)
        self.assertEqual(again['proposal']['operation_id'], plan['proposal']['operation_id'])
        with self.assertRaises(rp.PlanError):
            rp.propose(again, [assignment()], PLAN_SCOPE, same_again, root / 'proposed', existing=existing)
        self.assertEqual({p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in folder.iterdir()}, before)
        self.assertEqual([p.name for p in (root / 'proposed').iterdir()], [folder.name])
        for f, (digest, stamp) in protected.items():
            self.assertEqual((hashlib.sha256(f.read_bytes()).hexdigest(), f.stat().st_mtime_ns), (digest, stamp), f.name)
        self.assertEqual(sorted(p.name for p in (root / 'reports').iterdir()), sorted(f.name for f in protected))
        self.assertEqual([(v['kind'], v['action']) for v in first['existing_versions']], [(k, 'keep_unchanged') for k in ('original', 'corrected', 'approved', 'sent')])


class QaBlockers(unittest.TestCase):
    def test_a_changed_writer_or_checklist_format_is_another_input_and_a_stale_plan_is_refused(self):
        """QA 1: patching NativeDraftPublisher.renderer_hash left report_input_hash unchanged."""
        from unittest import mock
        from hosting.last_scan_local import NativeDraftPublisher
        out = Path(tempfile.mkdtemp()).resolve() / 'proposed'
        cap = sealed(late_rows()); args = ([assignment()], PLAN_SCOPE, cap)
        plan = rp.plan(*args)
        self.assertEqual(plan['render_version'], NativeDraftPublisher('.', MAP, {}, binding={k: PLAN_SCOPE[k] for k in ('client_id', 'site_id', 'list_id', 'job')}).render_version())
        for target, value in ((mock.patch.object(NativeDraftPublisher, 'renderer_hash', staticmethod(lambda: 'f' * 64)), None),
                              (mock.patch.object(NativeDraftPublisher, 'FORMAT', 'draft-checklist-v99'), None)):
            with self.subTest(change=str(target.attribute)), target:
                other = rp.plan(*args)
                self.assertNotEqual(other['render_version'], plan['render_version'])
                self.assertNotEqual(other['report_input_hash'], plan['report_input_hash'])
                self.assertNotEqual(other['proposal']['operation_id'], plan['proposal']['operation_id'])
                with self.assertRaises(rp.PlanError):
                    rp.propose(plan, *args, out)                                # the plan was made for the earlier writer
        self.assertFalse(out.exists())
        draft = dict(file_hash='d' * 64, kind='draft', report_input_hash=plan['report_input_hash'], source_snapshot_hash=cap['snapshot_hash'], **PLAN_SCOPE)
        with mock.patch.object(NativeDraftPublisher, 'renderer_hash', staticmethod(lambda: 'f' * 64)):
            later = rp.plan(*args, existing=[draft])
        self.assertEqual((later['status'], later['reason']), ('draft_proposed', 'stale_draft'))   # a draft by the older writer is not "current"

    def test_the_earlier_capture_must_really_be_earlier(self):
        """QA 2: a capture taken a day later was accepted as the earlier one."""
        cap = sealed(late_rows())
        def codes(previous):
            plan = rp.plan([assignment()], PLAN_SCOPE, cap, previous_capture=previous)
            self.assertEqual((plan['status'], plan['proposal']['allowed']), ('blocked', False)); self.assertNotIn('changes', plan)
            return [b['code'] for b in plan['blockers']]
        self.assertEqual(codes(sealed(early_rows(), taken=TAKEN + timedelta(days=1))), ['earlier_capture_not_earlier'])
        self.assertEqual(codes(sealed(early_rows(), taken=TAKEN)), ['earlier_capture_not_earlier'])                    # the same instant is not earlier
        self.assertEqual(codes(sealed(early_rows(), taken=TAKEN + timedelta(seconds=1))), ['earlier_capture_not_earlier'])
        unstated = {k: v for k, v in sealed(early_rows(), taken=TAKEN - timedelta(hours=1)).items() if k not in ('snapshot_hash', 'envelope_hash')}
        for when in ('2026-11-10T09:00:00', 'yesterday', None, ''):                                                    # no offset, or not a time
            with self.subTest(captured_at=when):
                self.assertEqual(codes(sf.seal(dict(unstated, captured_at=when))), ['earlier_capture_time_unknown'])
        ok = rp.plan([assignment()], PLAN_SCOPE, cap, previous_capture=sealed(early_rows(), taken=TAKEN - timedelta(seconds=1)))
        self.assertEqual((ok['changes']['basis'], ok['changes']['counts']['added'], ok['changes']['seconds_between_captures']), ('earlier_sealed_capture', 17, 1))
        none = rp.plan([assignment()], PLAN_SCOPE, cap)
        self.assertEqual(none['changes']['basis'], 'not_determined'); self.assertNotIn('added', none['changes'])      # unknown is said, not guessed

    def test_active_must_be_exactly_true(self):
        """QA 3: active None, "false" and 0 were accepted."""
        cap = sealed(late_rows())
        for value in (None, 'false', 'true', 0, 1, '', 'yes', [], False):
            with self.subTest(active=value):
                plan = rp.plan([assignment(active=value)], PLAN_SCOPE, cap)
                self.assertEqual((plan['status'], [b['code'] for b in plan['blockers']]), ('blocked', ['assignment_not_active']))
        missing = assignment(); del missing['active']
        self.assertEqual([b['code'] for b in rp.plan([missing], PLAN_SCOPE, cap)['blockers']], ['assignment_not_active'])
        self.assertEqual(rp.plan([assignment(active=True)], PLAN_SCOPE, cap)['status'], 'draft_proposed')
        for value in (None, 'true', 1, 'yes'):
            with self.subTest(verified=value):
                self.assertEqual([b['code'] for b in rp.plan([assignment(verified=value)], PLAN_SCOPE, cap)['blockers']], ['assignment_not_verified'])


def png(colour):
    from PIL import Image
    out = io.BytesIO(); Image.new('RGB', (4, 3), colour).save(out, format='PNG')
    return out.getvalue()


def with_photos(rows, photos, **kw):
    """A sealed capture whose records carry captured defect photos: {record id: image bytes}."""
    c = {k: v for k, v in sealed(rows, **kw).items() if k not in ('snapshot_hash', 'envelope_hash')}
    by_id = {r['id']: r for r in c['records']}
    c['photos'] = [dict(record_id=i, record_hash=sf.digest(by_id[i]), field='DefectPhoto', status='supported', sha256=hashlib.sha256(b).hexdigest(), title='Defect', bytes=len(b))
                   for i, b in photos.items()]
    return sf.seal(c)


class CapturedPhotos(unittest.TestCase):
    """QA 4: propose always passed no photo bytes, so a genuinely captured photo could never be placed."""
    def setUp(self):
        self.red, self.blue = png((180, 0, 30)), png((0, 40, 200))
        self.sha = hashlib.sha256(self.red).hexdigest()
        self.cap = with_photos(late_rows(), {'1': self.red, '34': self.blue})
        self.header = dict(photo_choices={'1': self.sha})                       # one photo explicitly selected for record 1
        self.args = ([assignment()], PLAN_SCOPE, self.cap)
        self.out = Path(tempfile.mkdtemp()).resolve() / 'proposed'

    def test_the_selected_captured_photo_is_placed_with_its_exact_bytes(self):
        from openpyxl import load_workbook
        plan = rp.plan(*self.args, header=self.header)
        written = rp.propose(plan, *self.args, self.out, header=self.header, photo_bytes={self.sha: self.red})
        folder = Path(written['written']['folder'])
        self.assertEqual(written['written']['photos_embedded'], [self.sha])
        raw = (folder / 'draft.xlsx').read_bytes()
        with zipfile.ZipFile(io.BytesIO(raw)) as z:
            media = {hashlib.sha256(z.read(n)).hexdigest() for n in z.namelist() if n.startswith('xl/media/')}
        self.assertIn(self.sha, media)                                          # the original bytes, not a re-encoded copy
        self.assertNotIn(hashlib.sha256(self.blue).hexdigest(), media)          # the photo that was not selected is not embedded
        book = load_workbook(io.BytesIO(raw))
        photos = book['Rejects & Photos']
        text = ' '.join(str(c.value) for row in photos.iter_rows() for c in row if c.value is not None)
        self.assertIn('Record 1', text); self.assertIn('ID SYN0000001', text); self.assertIn('Original source photo · ' + self.sha[:12], text)
        self.assertEqual(len(photos._images), 2)                                # the logo and the one selected photo
        checklist = json.loads((folder / 'checklist.json').read_text())
        self.assertEqual([(p['record_id'], p['sha256']) for p in checklist['verification']['photo_provenance']], [('1', self.sha)])
        self.assertEqual(sorted(p['sha256'] for p in checklist['photo_evidence']), sorted([self.sha, hashlib.sha256(self.blue).hexdigest()]))

    def test_missing_wrong_or_unbound_bytes_write_nothing(self):
        plan = rp.plan(*self.args, header=self.header)
        other = hashlib.sha256(self.blue).hexdigest()
        for label, supplied in (('missing', None), ('missing', {}), ('wrong bytes under the right name', {self.sha: self.blue}),
                                ('truncated', {self.sha: self.red[:-1]}), ('a photo that was not selected', {self.sha: self.red, other: self.blue}),
                                ('bytes of nothing in this capture', {self.sha: self.red, 'e' * 64: b'x'}), ('not bytes', {self.sha: 'text'}), ('not a mapping', [self.red])):
            with self.subTest(case=label), self.assertRaises(rp.PlanError):
                rp.propose(plan, *self.args, self.out, header=self.header, photo_bytes=supplied)
        self.assertFalse(self.out.exists())

    def test_a_selection_must_name_a_captured_photo_of_a_record_in_the_draft(self):
        with self.assertRaises(sf.ReviewError):
            rp.plan(*self.args, header=dict(photo_choices={'1': 'e' * 64}))     # not a captured photo
        with self.assertRaises(sf.ReviewError):
            rp.plan(*self.args, header=dict(photo_choices={'2': self.sha}))     # that photo belongs to another record
        rows = [record(1, 37, rejected=2, shift='B')] + late_rows()[1:]
        cap = with_photos(rows, {'1': self.red})
        with self.assertRaises(sf.ReviewError):                                 # its record is not in the draft (unapproved shift)
            rp.plan([assignment()], PLAN_SCOPE, cap, header=self.header)

    def test_a_draft_without_a_selection_needs_and_takes_no_bytes(self):
        plan = rp.plan(*self.args)
        with self.assertRaises(rp.PlanError):
            rp.propose(plan, *self.args, self.out, photo_bytes={self.sha: self.red})   # nothing selected: bytes are not accepted
        written = rp.propose(plan, *self.args, self.out)
        self.assertEqual(written['written']['photos_embedded'], [])
        self.assertNotEqual(plan['report_input_hash'], rp.plan(*self.args, header=self.header)['report_input_hash'])


class SourceLinks(unittest.TestCase):
    def linked(self, links):
        c = {k: v for k, v in sealed(late_rows()).items() if k not in ('snapshot_hash', 'envelope_hash')}
        for r in c['records']:
            if r['id'] in links:
                r.update(links[r['id']])
        return sf.seal(c)

    def test_links_in_the_capture_are_kept_exactly_and_none_is_made_up(self):
        url = lambda n: f'https://example.sharepoint.com/sites/Synthetic/Lists/Scans/DispForm.aspx?ID={n}'
        every = self.linked({str(n): {'web_url': url(n)} for n in range(1, 51)})
        plan = rp.plan([assignment()], PLAN_SCOPE, every)
        self.assertEqual(plan['source']['record_links'], 'in_capture')
        self.assertEqual(plan['source']['links'], {str(n): url(n) for n in range(1, 51)})
        some = rp.plan([assignment()], PLAN_SCOPE, self.linked({'1': {'web_url': url(1)}, '2': {'url': url(2)}}))
        self.assertEqual((some['source']['record_links'], some['source']['links']), ('partly_in_capture', {'1': url(1), '2': url(2)}))
        none = rp.plan([assignment()], PLAN_SCOPE, sealed(late_rows()))
        self.assertEqual((none['source']['record_links'], none['source']['links']), ('not_in_capture', {}))
        self.assertNotIn('https://', json.dumps({k: v for k, v in none.items() if k != 'notices'}))

    def test_anything_that_is_not_a_plain_https_link_is_left_out(self):
        bad = {'1': {'web_url': 'http://example.sharepoint.com/x'}, '2': {'web_url': 'javascript:alert(1)'}, '3': {'web_url': 'https://a b'},
               '4': {'web_url': 17}, '5': {'web_url': ''}, '6': {'web_url': 'https://ok.example/item/6\n'}, '7': {'link': 'https://ok.example/item/7'}}
        plan = rp.plan([assignment()], PLAN_SCOPE, self.linked(bad))
        self.assertEqual((plan['source']['record_links'], plan['source']['links']), ('not_in_capture', {}))

    def test_a_link_is_part_of_the_sealed_capture_and_is_written_with_the_proposal(self):
        url = 'https://example.sharepoint.com/sites/Synthetic/Lists/Scans/DispForm.aspx?ID=1'
        cap = self.linked({'1': {'web_url': url}})
        self.assertNotEqual(cap['snapshot_hash'], sealed(late_rows())['snapshot_hash'])     # changing a link changes the capture
        tampered = copy.deepcopy(cap); tampered['records'][0]['web_url'] = url.replace('ID=1', 'ID=2')
        self.assertEqual([b['code'] for b in rp.plan([assignment()], PLAN_SCOPE, tampered)['blockers']], ['capture_not_sealed'])
        out = Path(tempfile.mkdtemp()).resolve() / 'proposed'
        plan = rp.plan([assignment()], PLAN_SCOPE, cap)
        written = rp.propose(plan, [assignment()], PLAN_SCOPE, cap, out)
        saved = json.loads((Path(written['written']['folder']) / 'regeneration-plan.json').read_text())
        self.assertEqual(saved['source']['links'], {'1': url})


class ReportOnlyCorrections(unittest.TestCase):
    """A person corrected a part number in an earlier report. The source still has it blank. Regenerating from the source
    would quietly put the blank back. That must stop for review, and the corrected value must not be trusted blindly."""
    FIXED = 'c' * 64

    def setUp(self):
        rows = late_rows()
        rows[4]['raw']['Part'] = None                                           # record 5: no part number at the source
        self.cap = sealed(rows)
        self.existing = [dict(file_hash='a' * 64, kind='original', rows=33, **PLAN_SCOPE), dict(file_hash=self.FIXED, kind='corrected', rows=33, **PLAN_SCOPE)]
        self.args = ([assignment()], PLAN_SCOPE, self.cap)
        self.out = Path(tempfile.mkdtemp()).resolve() / 'proposed'

    def correction(self, **over):
        return dict(dict(report_file_hash=self.FIXED, record_id='5', field='part', report_value='00009900', documented_by='Made-up Reviewer',
                         reason='Part number read from the label on the made-up photo',
                         evidence=[dict(kind='report_cell', file_hash=self.FIXED, sheet='1st Shift', cell='D20'), dict(kind='source_item', record_id='5')]), **over)

    def ack(self, plan, **over):
        return dict(dict(correction_id=plan['corrections'][0]['correction_id'], reviewed_by='Made-up Supervisor', decision=rp.ACKNOWLEDGE,
                         note='Compared the report cell, the item and the photo; keep both on file'), **over)

    def test_a_blank_source_does_not_silently_replace_a_documented_correction(self):
        plan = rp.plan(*self.args, existing=self.existing, corrections=[self.correction()])
        self.assertEqual((plan['status'], plan['reason'], plan['proposal']['allowed']), ('review_required', 'report_only_correction_not_reviewed', False))
        self.assertEqual(plan['automated_completion'], 'blocked_by_report_only_correction')
        c = plan['corrections'][0]
        self.assertEqual((c['outcome'], c['source_value_now'], c['report_value']), ('conflict_source_blank', {'type': 'null'}, '00009900'))
        self.assertEqual((c['value_trusted'], c['carried_into_draft'], c['evidence_status'], c['acknowledged']), (False, False, 'exact_report_and_item_named', None))
        self.assertEqual(plan['corrections_needing_review'], [c['correction_id']])
        hold = next(h for h in plan['holds'] if h['code'] == 'report_only_correction_conflict')
        self.assertIn('record 5 part', hold['text']); self.assertIn('not copied in', hold['text'])
        with self.assertRaises(rp.PlanError):
            rp.propose(plan, *self.args, self.out, existing=self.existing, corrections=[self.correction()])
        self.assertFalse(self.out.exists())
        self.assertEqual([(v['kind'], v['action']) for v in plan['existing_versions']], [('original', 'keep_unchanged'), ('corrected', 'keep_unchanged')])

    def test_after_a_named_review_the_draft_shows_the_source_value_and_lists_the_conflict(self):
        from openpyxl import load_workbook
        rows = late_rows(); rows[4]['raw']['Part'] = '00001111'                                         # the source says something else
        args = ([assignment()], PLAN_SCOPE, sealed(rows))
        corrections = [self.correction()]
        first = rp.plan(*args, existing=self.existing, corrections=corrections)
        self.assertEqual((first['status'], first['corrections'][0]['outcome']), ('review_required', 'conflict_source_differs'))
        acks = [self.ack(first)]
        plan = rp.plan(*args, existing=self.existing, corrections=corrections, acknowledged=acks)
        self.assertEqual((plan['status'], plan['proposal']['allowed'], plan['corrections_needing_review']), ('draft_proposed', True, []))
        self.assertEqual(plan['automated_completion'], 'blocked_by_report_only_correction')             # reviewed is not the same as complete
        self.assertNotEqual(plan['report_input_hash'], first['report_input_hash'])
        with self.assertRaises(rp.PlanError):
            rp.propose(first, *args, self.out, existing=self.existing, corrections=corrections, acknowledged=acks)   # the unreviewed plan
        with self.assertRaises(rp.PlanError):
            rp.propose(plan, *args, self.out, existing=self.existing, corrections=corrections)                       # the review left out
        self.assertFalse(self.out.exists())
        written = rp.propose(plan, *args, self.out, existing=self.existing, corrections=corrections, acknowledged=acks)
        folder = Path(written['written']['folder'])
        book = load_workbook(io.BytesIO((folder / 'draft.xlsx').read_bytes()))
        row = next(r for r in book['1st Shift'].iter_rows(min_row=16) if r[4].value == 'SYN0000005')
        self.assertEqual(row[3].value, '00001111')                                                      # the source value. The correction is NOT copied in
        everything = ' '.join(str(c.value) for ws in book for r in ws.iter_rows() for c in r if c.value is not None)
        self.assertEqual(everything.count('00001111') > 0, True)
        review = (folder / 'REVIEW-FIRST.txt').read_text()
        self.assertIn('Record 5, part', review); self.assertIn('says "00009900"', review); self.assertIn('The source now says "00001111".', review)
        self.assertIn('This draft shows the source value. The earlier report is unchanged.', review); self.assertIn('Reviewed by Made-up Supervisor', review)
        saved = json.loads((folder / 'regeneration-plan.json').read_text())
        self.assertEqual((saved['corrections'][0]['carried_into_draft'], saved['corrections'][0]['value_trusted'], saved['corrections'][0]['acknowledged']['reviewed_by']),
                         (False, False, 'Made-up Supervisor'))

    def test_a_blank_part_number_the_writer_cannot_lay_out_fails_closed_after_review(self):
        """Found while testing: the selected-template writer cannot lay out a day in which one record has no part number
        and others do. The planner says so and leaves nothing behind; it does not fill the blank to get a draft."""
        corrections = [self.correction()]
        first = rp.plan(*self.args, existing=self.existing, corrections=corrections)
        acks = [self.ack(first)]
        plan = rp.plan(*self.args, existing=self.existing, corrections=corrections, acknowledged=acks)
        self.assertEqual(plan['status'], 'draft_proposed')
        with self.assertRaises(rp.PlanError) as failure:
            rp.propose(plan, *self.args, self.out, existing=self.existing, corrections=corrections, acknowledged=acks)
        self.assertIn('could not render these source rows', str(failure.exception)); self.assertIn('Nothing was written', str(failure.exception))
        self.assertFalse(self.out.exists())

    def test_each_outcome_is_named_for_what_it_is(self):
        rows = late_rows(); rows[4]['raw']['Part'] = '00001111'
        differs = rp.plan([assignment()], PLAN_SCOPE, sealed(rows), existing=self.existing, corrections=[self.correction()])
        self.assertEqual((differs['status'], differs['corrections'][0]['outcome'], differs['corrections'][0]['source_value_now']['value']), ('review_required', 'conflict_source_differs', '00001111'))
        agrees = rp.plan([assignment()], PLAN_SCOPE, sealed(late_rows()), existing=self.existing, corrections=[self.correction()])
        self.assertEqual((agrees['status'], agrees['corrections'][0]['outcome'], agrees['corrections_needing_review']), ('draft_proposed', 'source_agrees', []))
        self.assertFalse(any(h['code'] == 'report_only_correction_conflict' for h in agrees['holds']))
        self.assertEqual(agrees['automated_completion'], 'not_claimed')
        gone = rp.plan(*self.args, existing=self.existing, corrections=[self.correction(record_id='999', evidence=[dict(kind='report_cell', file_hash=self.FIXED, sheet='1st Shift', cell='D20'), dict(kind='source_item', record_id='999')])])
        self.assertEqual((gone['status'], gone['corrections'][0]['outcome'], gone['corrections'][0]['source_value_now']), ('review_required', 'record_not_in_capture', None))
        absent = late_rows(); del absent[4]['raw']['Part']
        self.assertEqual(rp.plan([assignment()], PLAN_SCOPE, sealed(absent), existing=self.existing, corrections=[self.correction()])['corrections'][0]['source_value_now'], {'type': 'absent'})
        blank = late_rows(); blank[4]['raw']['Part'] = '   '
        self.assertEqual(rp.plan([assignment()], PLAN_SCOPE, sealed(blank), existing=self.existing, corrections=[self.correction()])['corrections'][0]['outcome'], 'conflict_source_blank')

    def test_a_correction_without_exact_evidence_still_stops_the_plan_and_is_marked_not_established(self):
        cases = {'the report version it names is not among the existing versions of this scope': self.correction(report_file_hash='9' * 64, evidence=[dict(kind='report_cell', file_hash='9' * 64, sheet='1st Shift', cell='D20'), dict(kind='source_item', record_id='5')]),
                 'no exact report cell is named': self.correction(evidence=[dict(kind='source_item', record_id='5')]),
                 'no source item is named': self.correction(evidence=[dict(kind='report_cell', file_hash=self.FIXED, sheet='1st Shift', cell='D20')]),
                 'a photo it names is not a captured photo of this record': self.correction(evidence=self.correction()['evidence'] + [dict(kind='photo', sha256='e' * 64)])}
        for gap, correction in cases.items():
            with self.subTest(gap=gap):
                plan = rp.plan(*self.args, existing=self.existing, corrections=[correction])
                c = plan['corrections'][0]
                self.assertEqual((plan['status'], c['evidence_status'], c['value_trusted'], c['carried_into_draft']), ('review_required', 'not_established', False, False))
                self.assertIn(gap, c['evidence_gaps'])
        empty = rp.plan(*self.args, existing=self.existing, corrections=[self.correction(evidence=[])])
        self.assertEqual(len(empty['corrections'][0]['evidence_gaps']), 2)

    def test_a_photo_named_as_evidence_must_be_a_captured_photo_of_that_record(self):
        image = png((10, 120, 10)); sha = hashlib.sha256(image).hexdigest()
        rows = late_rows(); rows[4]['raw']['Part'] = None
        cap = with_photos(rows, {'5': image})
        good = self.correction(evidence=self.correction()['evidence'] + [dict(kind='photo', sha256=sha)])
        plan = rp.plan([assignment()], PLAN_SCOPE, cap, existing=self.existing, corrections=[good])
        self.assertEqual((plan['corrections'][0]['evidence_status'], plan['status'], plan['corrections'][0]['value_trusted']), ('exact_report_and_item_named', 'review_required', False))

    def test_an_acknowledgement_must_be_complete_named_and_about_an_open_conflict(self):
        corrections = [self.correction()]
        first = rp.plan(*self.args, existing=self.existing, corrections=corrections)
        for bad in (self.ack(first, reviewed_by=''), self.ack(first, note='ok'), self.ack(first, decision='use_report_value'), self.ack(first, decision='approve'),
                    self.ack(first, correction_id='corr-' + '0' * 24), {k: v for k, v in self.ack(first).items() if k != 'note'}, dict(self.ack(first), extra=1), 'yes'):
            with self.subTest(ack=str(bad)[:60]), self.assertRaises(rp.PlanError):
                rp.plan(*self.args, existing=self.existing, corrections=corrections, acknowledged=[bad])
        for bad in (dict(self.correction(), field='labor'), {k: v for k, v in self.correction().items() if k != 'reason'}, dict(self.correction(), report_value=9900),
                    dict(self.correction(), evidence=[dict(kind='hearsay')]), dict(self.correction(), extra=True), 'part 5 was fixed'):
            with self.subTest(correction=str(bad)[:60]), self.assertRaises(rp.PlanError):
                rp.plan(*self.args, existing=self.existing, corrections=[bad])
        two = [self.correction(), self.correction(record_id='6', evidence=[dict(kind='report_cell', file_hash=self.FIXED, sheet='1st Shift', cell='D21'), dict(kind='source_item', record_id='6')], report_value='00007777')]
        both = rp.plan(*self.args, existing=self.existing, corrections=two)
        self.assertEqual(len(both['corrections_needing_review']), 2)
        one = rp.plan(*self.args, existing=self.existing, corrections=two, acknowledged=[self.ack(both)])
        self.assertEqual((one['status'], len(one['corrections_needing_review'])), ('review_required', 1))            # one of two reviewed is not enough

    def test_no_corrections_supplied_is_said_not_assumed(self):
        plan = rp.plan(*self.args, existing=self.existing)
        self.assertEqual((plan['corrections'], plan['corrections_needing_review'], plan['status']), ([], [], 'draft_proposed'))


class Isolation(unittest.TestCase):
    def test_nothing_reaches_the_planner_and_it_reaches_no_network(self):
        import re
        source = (ROOT / 'hosting' / 'regeneration_planner.py').read_text()
        for word in ('urllib', 'requests', 'http.client', 'socket', 'subprocess', 'os.environ', 'smtplib', 'graph.microsoft'):
            self.assertNotIn(word, source)
        users = [p.name for p in (ROOT / 'hosting').glob('*.py') if p.name != 'regeneration_planner.py' and re.search(r'regeneration_planner', p.read_text())]
        users += [p.name for folder in ('api', 'app', 'scripts') for p in (ROOT / folder).rglob('*.py') if 'regeneration_planner' in p.read_text()]
        self.assertEqual(users, [])


if __name__ == '__main__':
    unittest.main()

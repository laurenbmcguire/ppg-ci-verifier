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
        self.assertEqual(sorted(p.name for p in folder.iterdir()), ['checklist.json', 'checklist.txt', 'draft.xlsx', 'receipt.json', 'regeneration-plan.json'])
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

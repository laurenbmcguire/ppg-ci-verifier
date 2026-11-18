"""Off-by-default bounded runner: registry gate, budget, cooldown, isolation, run receipts. Synthetic sources only."""
import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from hosting import source_first as sf
from hosting import last_scan_runner as runner
from hosting.last_scan_local import StateStore, NativeDraftPublisher
from hosting.last_scan_service import source_adapter
from hosting.source_first_capture import CaptureFailure
from source_first_fixture import capture, MAP, SCOPE

SITE, LIST, JOB = SCOPE['site_id'], SCOPE['list_id'], SCOPE['job']
ENTRY = dict(client_key='synthetic', client='Synthetic client', site_id=SITE, list_id=LIST, job=JOB, verified=True)


def assignment(**kw):
    return dict(dict(verified=True, active=True, timestamp_semantics_verified=True, client_id='synthetic', job=JOB,
                     site_id=SITE, list_id=LIST, zone='America/Chicago', scan_field='ActualScan', shift_field='Shift',
                     recorded_shifts=['1'], business_dates=['2026-11-10']), **kw)


class Harness(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(); self.addCleanup(self.folder.cleanup)
        self.store = StateStore(Path(self.folder.name) / 'state.db')
        self.now = '2026-11-10T21:35:00Z'; self.fetched = []; self.renders = []; self.fail = {}; self.tick = 0.0

    def clock(self): return self.now

    def fetch(self, scope, settings):
        self.fetched.append((scope['list_id'], scope['business_date'], scope['shift']))
        problem = self.fail.get(scope['shift']) or self.fail.get(scope['list_id'])
        if problem: raise problem
        return dict(scope=scope, rows=[dict(id='1', scan_at='2026-11-10T20:30:00Z')], captured_at=self.now, terminal=True)

    def render(self, snapshot, op):
        self.renders.append(op); return dict(operation_id=op, workbook_sha256='w', checklist_sha256='c')

    def go(self, config, run_id, entries=(ENTRY,), **kw):
        return runner.run_once(self.store, config, fetch=self.fetch, render=self.render, run_id=run_id,
                               entries=None if entries is None else list(entries), clock=self.clock, **kw)


class OffByDefault(Harness):
    def test_nothing_runs_unless_explicitly_enabled(self):
        for config in (None, {}, dict(assignments=[assignment()]), dict(enabled='true', assignments=[assignment()]),
                       dict(enabled=1, assignments=[assignment()])):
            self.assertEqual(self.go(config, 'r'), dict(status='disabled'))
        self.assertEqual(self.fetched, [])
        with self.store.transaction('run:' + sf.digest('r')) as record:
            self.assertEqual(record, {})

    def test_enabled_with_no_assignments_reads_nothing(self):
        out = self.go(dict(enabled=True), 'r1')
        self.assertEqual((out['status'], out['planned'], out['issues'], self.fetched), ('finished', 0, [], []))

    def test_only_verified_registry_matched_assignments_run(self):
        other_list = '99999999-9999-9999-9999-999999999999'
        bad = [assignment(verified=False), assignment(active=None), assignment(timestamp_semantics_verified=False),
               assignment(scan_field='Created'), assignment(scan_field='Date'), assignment(scan_field='lastModifiedDateTime'),
               assignment(scan_field=''), assignment(recorded_shifts=[]), assignment(zone=None),
               assignment(list_id=other_list), assignment(job='OTHER-9'), assignment(externally_built=True),
               assignment(zone='Not/AZone'), assignment(active=False)]
        out = self.go(dict(enabled=True, assignments=bad), 'r1')
        self.assertEqual(self.fetched, [])
        self.assertEqual([i['code'] for i in out['issues']],
                         ['unverified_assignment', 'unverified_assignment', 'scan_time_meaning_not_verified',
                          'scan_field_is_date_or_arrival_metadata', 'scan_field_is_date_or_arrival_metadata',
                          'scan_field_is_date_or_arrival_metadata', 'incomplete_assignment', 'recorded_shifts_missing',
                          'incomplete_assignment', 'not_in_verified_job_registry', 'not_in_verified_job_registry',
                          'built_by_external_process', 'invalid_scope:ZoneInfoNotFoundError'])

    def test_registry_must_be_supplied_and_verified(self):
        config = dict(enabled=True, assignments=[assignment()])
        self.assertEqual(self.go(config, 'a', entries=None)['issues'][0]['code'], 'job_registry_unavailable')
        self.assertEqual(self.go(config, 'b', entries=[dict(ENTRY, verified=False)])['issues'][0]['code'], 'not_in_verified_job_registry')
        self.assertEqual(self.go(config, 'c', entries=[dict(ENTRY, site_id=None, site='Synthetic_Site')])['issues'][0]['code'], 'not_in_verified_job_registry')
        self.assertEqual(self.fetched, [])
        self.assertEqual(self.go(config, 'd')['counts'], {'draft_ready': 1})

    def test_live_registry_reader_uses_saved_entries(self):
        class Inbox:
            def setting(self, key, default): return [ENTRY] if key == 'job_registry' else default
        entries = runner.registry_entries(Inbox())
        self.assertIn(ENTRY, entries)
        self.assertFalse(any(e.get('list_id') == LIST for e in runner.registry_entries(type('I', (), {'setting': lambda s, k, d: d})())))

    def test_dates_default_to_local_yesterday_and_today_across_dst(self):
        a = assignment(business_dates=None)
        for now, expected in [('2026-11-01T04:30:00Z', ['2026-10-30', '2026-10-31']),      # 23:30 CDT
                              ('2026-11-01T05:30:00Z', ['2026-10-31', '2026-11-01']),      # 00:30 CDT
                              ('2026-11-01T07:30:00Z', ['2026-10-31', '2026-11-01']),      # 01:30 CST, repeated hour
                              ('2026-03-08T08:30:00Z', ['2026-03-07', '2026-03-08'])]:     # 03:30 CDT after spring forward
            scopes, issues = runner.plan(dict(assignments=[a]), [ENTRY], now)
            self.assertEqual(([s['scope']['business_date'] for s in sorted(scopes, key=lambda s: s['scope']['business_date'])], issues), (expected, []))


class Bounded(Harness):
    def config(self, shifts):
        return dict(enabled=True, assignments=[assignment(recorded_shifts=shifts)])

    def test_scope_limit_resumes_where_it_stopped(self):
        config = self.config(['1', '2', '3', '4', '5'])
        first = self.go(config, 'r1', max_scopes=2)
        self.assertEqual((first['processed'], first['deferred']), (2, 3))
        second = self.go(config, 'r2', max_scopes=2); third = self.go(config, 'r3', max_scopes=2)
        self.assertEqual((second['processed'], third['processed'], third['deferred']), (2, 2, 3))
        seen = [r['shift'] for out in (first, second, third) for r in out['results']]
        self.assertEqual(sorted(seen[:5]), ['1', '2', '3', '4', '5'])   # everyone got a turn before anyone got two
        self.assertEqual(seen[5], seen[0])

    def test_time_budget_stops_the_pass(self):
        ticks = iter(range(0, 1000, 15))                                 # every look at the clock is 15 seconds later
        out = self.go(self.config(['1', '2', '3', '4', '5']), 'r1', budget_seconds=40, monotonic=lambda: next(ticks))
        self.assertEqual((out['processed'], out['deferred']), (2, 3))

    def test_one_failed_scope_does_not_stop_the_others(self):
        self.fail['2'] = ValueError('partial pagination')
        out = self.go(self.config(['1', '2', '3']), 'r1')
        by = {r['shift']: r for r in out['results']}
        self.assertEqual((by['1']['status'], by['2']['status'], by['2']['error_type'], by['3']['status']),
                         ('draft_ready', 'error', 'ValueError', 'draft_ready'))
        self.assertEqual((out['client_release'], out['notifications_sent']), ('held', 0))

    def test_provider_cooldown_is_respected_across_runs(self):
        config = self.config(['1', '2', '3'])
        self.fail[LIST] = CaptureFailure('Throttled.', dict(status=429, cooldown=dict(retry_after='600', not_before='2026-11-10T21:45:00+00:00', known=True)))
        first = self.go(config, 'r1')
        self.assertEqual(len(self.fetched), 1)                           # one refused read, then the site is left alone
        self.assertEqual(sorted(r['status'] for r in first['results']), ['cooldown', 'cooldown', 'error'])
        self.fail.clear(); self.now = '2026-11-10T21:44:59Z'
        second = self.go(config, 'r2')
        self.assertEqual((len(self.fetched), second['counts']), (1, {'cooldown': 3}))
        self.now = '2026-11-10T21:45:00Z'
        third = self.go(config, 'r3')
        self.assertEqual(third['counts'], {'draft_ready': 3})

    def test_cooldown_without_a_stated_time_waits_the_default(self):
        self.fail[LIST] = CaptureFailure('Unavailable.', dict(status=503, cooldown=dict(retry_after=None, not_before=None, known=False)))
        out = self.go(self.config(['1']), 'r1')
        self.assertEqual(out['results'][0]['cooldown_until'], '2026-11-10T21:50:00+00:00')

    def test_same_run_id_is_replayed_without_reading(self):
        config = self.config(['1', '2'])
        first = self.go(config, 'firing-2026-11-10T21:35')
        reads = len(self.fetched)
        again = self.go(config, 'firing-2026-11-10T21:35')
        self.assertTrue(again.pop('replayed')); self.assertEqual(again, first)
        self.assertEqual((len(self.fetched), len(self.renders)), (reads, 2))
        with self.assertRaises(ValueError):
            self.go(config, ' ')

    def test_interrupted_run_resumes_idempotently(self):
        config = self.config(['1', '2'])
        class Crash(BaseException): pass
        calls = iter([0, 0])            # dies after the first scope
        def dying():
            try: return next(calls)
            except StopIteration: raise Crash()
        with self.assertRaises(Crash):
            self.go(config, 'r1', monotonic=dying)
        with self.store.transaction('run:' + sf.digest('r1')) as record:
            self.assertEqual((record['status'], record['attempts']), ('started', 1))
        out = self.go(config, 'r1')
        self.assertEqual((out['counts'], len(self.renders)), ({'draft_ready': 2}, 2))
        with self.store.transaction('run:' + sf.digest('r1')) as record:
            self.assertEqual((record['status'], record['attempts']), ('finished', 2))


class NativeEndToEnd(unittest.TestCase):
    """Synthetic sealed capture -> runner -> native-template draft + checklist, with an unknown quantity preserved."""
    def test_draft_keeps_unknown_values_and_is_held(self):
        with tempfile.TemporaryDirectory() as folder:
            now = ['2026-11-10T21:00:00Z']; scans = {'1': '2026-11-10T20:30:00Z'}
            def cap(scope):
                c = capture(); c['records'][0]['raw']['ActualScan'] = scans['1']
                c['records'][0]['raw']['Rejected'] = None                 # NOK count not recorded at the source
                c['captured_at'] = now[0]
                return sf.seal({k: v for k, v in c.items() if k not in ('snapshot_hash', 'envelope_hash')}), {}
            fetch = lambda scope, settings: source_adapter(scope, settings, capture_fn=cap)
            store = StateStore(Path(folder) / 'state.db')
            publisher = NativeDraftPublisher(Path(folder) / 'reports', MAP, {})
            config = dict(enabled=True, assignments=[assignment()])
            go = lambda rid: runner.run_once(store, config, fetch=fetch, render=publisher, run_id=rid, entries=[ENTRY], clock=lambda: now[0])
            self.assertEqual(go('r1')['counts'], {'waiting': 1})
            now[0] = '2026-11-10T21:30:00Z'
            done = go('r2')
            self.assertEqual(done['counts'], {'draft_ready': 1})
            op = done['results'][0]['operation_id']
            self.assertEqual(go('r3')['results'][0]['operation_id'], op)
            self.assertEqual(len(list((Path(folder) / 'reports').iterdir())), 1)
            target = Path(folder) / 'reports' / op
            bundle = json.loads((target / 'checklist.json').read_text())
            self.assertEqual((bundle['source_ids'], bundle['client_release'], bundle['status'], bundle['day_completion']),
                             (['1'], 'held', 'DRAFT', 'not_established'))
            self.assertTrue(any(f['field'] == 'rejected' and f['code'] == 'missing_value' for f in bundle['findings']))
            self.assertTrue(bundle['verification']['native_layout_preserved'])
            from openpyxl import load_workbook
            book = load_workbook(target / 'draft.xlsx', data_only=True)
            self.assertIsNone(book['Summary']['K24'].value)               # unknown stays blank, never zero
            self.assertEqual((book['Summary']['G24'].value, book['Summary']['I24'].value), (10, 8))
            self.assertIn('UNKNOWN', book['Summary']['B5'].value)
            book.close()
            with store.transaction('outbox:' + done['results'][0]['scope']) as box:
                self.assertEqual([(k, v['status'], v['audience']) for k, v in box['entries'].items()], [(op, 'pending', 'Lauren')])
            # an interrupted artifact folder is never overwritten or re-rendered
            now[0] = '2026-11-10T21:31:00Z'; scans['1'] = '2026-11-10T20:31:00Z'
            self.assertEqual(go('r4')['counts'], {'waiting': 1})


if __name__ == '__main__':
    unittest.main()

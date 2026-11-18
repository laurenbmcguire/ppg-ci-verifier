"""Off-by-default bounded runner: exact registry binding, per-source renderers, budget, cooldown, isolation, run
receipts. Synthetic sources only."""
import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from hosting import source_first as sf
from hosting import last_scan_runner as runner
from hosting.last_scan_local import StateStore, NativeDraftPublisher, NativeDraftRouter
from hosting.last_scan_service import RendererRouter, ScheduledWorker, binding, source_adapter
from hosting.last_scan_drafts import scope_key
from hosting.source_first_capture import CaptureFailure
from source_first_fixture import capture, MAP, SCOPE

SITE, LIST, JOB = SCOPE['site_id'], SCOPE['list_id'], SCOPE['job']
ENTRY = dict(client_key='synthetic', client='Synthetic client', site_id=SITE, list_id=LIST, job=JOB, verified=True)


def assignment(**kw):
    return dict(dict(verified=True, active=True, timestamp_semantics_verified=True, client_id='synthetic', job=JOB,
                     site_id=SITE, list_id=LIST, zone='America/Chicago', scan_field='ActualScan', shift_field='Shift',
                     recorded_shifts=['1'], business_dates=['2026-11-10']), **kw)


class FakePublisher:
    def __init__(self, harness, bound, version='v1'):
        self.harness, self.binding, self.version = harness, binding(bound), version
    def render_version(self): return self.version
    def __call__(self, snapshot, op):
        self.harness.renders.append((self.binding['client_id'], snapshot['scope']['client_id'], op))
        return dict(operation_id=op, workbook_sha256='w', checklist_sha256='c', binding=self.binding, render_version=self.version)


class Harness(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(); self.addCleanup(self.folder.cleanup)
        self.store = StateStore(Path(self.folder.name) / 'state.db')
        self.now = '2026-11-10T21:35:00Z'; self.fetched = []; self.renders = []; self.fail = {}
        self.router = RendererRouter([FakePublisher(self, assignment())])

    def clock(self): return self.now

    def fetch(self, scope, settings):
        self.fetched.append((scope['list_id'], scope['business_date'], scope['shift']))
        problem = self.fail.get(scope['shift']) or self.fail.get(scope['list_id'])
        if problem: raise problem
        return dict(scope=scope, rows=[dict(id='1', scan_at='2026-11-10T20:30:00Z')], captured_at=self.now, terminal=True)

    def go(self, config, run_id, entries=(ENTRY,), **kw):
        return runner.run_once(self.store, config, fetch=self.fetch, render=kw.pop('router', self.router), run_id=run_id,
                               entries=None if entries is None else list(entries), clock=self.clock, **kw)

    def refused(self, a, entries, code):
        """A mismatch must be reported AND cause zero source reads and zero renders."""
        out = self.go(dict(enabled=True, assignments=[a]), 'r-' + code + str(len(self.fetched)), entries=entries)
        self.assertEqual(([i['code'] for i in out['issues']], out['planned'], self.fetched, self.renders), ([code], 0, [], []))


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

    def test_incomplete_or_unverified_assignments_never_run(self):
        bad = [assignment(verified=False), assignment(active=None), assignment(timestamp_semantics_verified=False),
               assignment(scan_field='Created'), assignment(scan_field='Date'), assignment(scan_field='Date0'),
               assignment(scan_field='lastModifiedDateTime'), assignment(scan_field=''), assignment(recorded_shifts=[]),
               assignment(zone=None), assignment(externally_built=True), assignment(zone='Not/AZone'), assignment(active=False)]
        out = self.go(dict(enabled=True, assignments=bad), 'r1')
        self.assertEqual((self.fetched, self.renders), ([], []))
        self.assertEqual([i['code'] for i in out['issues']],
                         ['unverified_assignment', 'unverified_assignment', 'scan_time_meaning_not_verified',
                          'scan_field_is_date_or_arrival_metadata', 'scan_field_is_date_or_arrival_metadata',
                          'scan_field_is_date_or_arrival_metadata', 'scan_field_is_date_or_arrival_metadata',
                          'incomplete_assignment', 'recorded_shifts_missing', 'incomplete_assignment',
                          'built_by_external_process', 'invalid_scope:ZoneInfoNotFoundError'])

    def test_live_registry_reader_uses_saved_entries(self):
        class Inbox:
            def setting(self, key, default): return [ENTRY] if key == 'job_registry' else default
        self.assertIn(ENTRY, runner.registry_entries(Inbox()))
        self.assertFalse(any(e.get('list_id') == LIST for e in runner.registry_entries(type('I', (), {'setting': lambda s, k, d: d})())))

    def test_dates_default_to_local_yesterday_and_today_across_dst(self):
        a = assignment(business_dates=None)
        for now, expected in [('2026-11-01T04:30:00Z', ['2026-10-30', '2026-10-31']),      # 23:30 CDT
                              ('2026-11-01T05:30:00Z', ['2026-10-31', '2026-11-01']),      # 00:30 CDT
                              ('2026-11-01T07:30:00Z', ['2026-10-31', '2026-11-01']),      # 01:30 CST, repeated hour
                              ('2026-03-08T08:30:00Z', ['2026-03-07', '2026-03-08'])]:     # 03:30 CDT after spring forward
            scopes, issues = runner.plan(dict(assignments=[a]), [ENTRY], now)
            self.assertEqual(([s['scope']['business_date'] for s in sorted(scopes, key=lambda s: s['scope']['business_date'])], issues), (expected, []))


class ExactRegistryBinding(Harness):
    """Client key, job, site id and list id must each match the verified registry exactly."""
    def test_exact_match_runs(self):
        self.assertEqual(self.go(dict(enabled=True, assignments=[assignment()]), 'ok')['counts'], {'draft_ready': 1})

    def test_qa_repro_wrong_client_and_same_number_other_job(self):
        scopes, issues = runner.plan({'assignments': [assignment(client_id='WRONG-CLIENT', job='OTHER-1234')]}, [ENTRY], '2026-11-10T21:35:00Z')
        self.assertEqual((len(scopes), [i['code'] for i in issues]), (0, ['registry_client_mismatch']))

    def test_wrong_client(self):
        self.refused(assignment(client_id='WRONG-CLIENT'), [ENTRY], 'registry_client_mismatch')
        self.refused(assignment(client_id='Synthetic'), [ENTRY], 'registry_client_mismatch')          # case is not forgiven

    def test_same_number_different_job(self):
        for job in ('OTHER-1234', '1234', 'synthetic-1234', 'SYNTHETIC 1234', 'SYNTHETIC-12345'):
            self.refused(assignment(job=job), [ENTRY], 'registry_job_mismatch')

    def test_cross_source(self):
        other_list, other_site = '99999999-9999-9999-9999-999999999999', SITE.replace('1111', '7777')
        self.refused(assignment(list_id=other_list), [ENTRY], 'not_in_verified_job_registry')         # same site, another list
        self.refused(assignment(site_id=other_site), [ENTRY], 'not_in_verified_job_registry')         # same list id, another site
        self.refused(assignment(), [dict(ENTRY, site_id=None, site='Synthetic_Site')], 'not_in_verified_job_registry')   # name only
        self.refused(assignment(), [dict(ENTRY, verified=False)], 'not_in_verified_job_registry')
        self.refused(assignment(), [dict(ENTRY, source='Audits_KN')], 'registry_entry_is_another_source')
        self.refused(assignment(), None, 'job_registry_unavailable')

    def test_ambiguous_registry(self):
        self.refused(assignment(), [ENTRY, dict(ENTRY, client_key='other-client')], 'ambiguous_job_registry')   # two clients claim the list
        self.refused(assignment(), [ENTRY, dict(ENTRY, job='2207')], 'ambiguous_job_registry')                  # two jobs claim the list
        self.assertEqual(self.go(dict(enabled=True, assignments=[assignment()]), 'dup', entries=[ENTRY, dict(ENTRY)])['counts'], {'draft_ready': 1})


class RendererRouting(Harness):
    B = dict(client_id='client-b', site_id=SITE, list_id='55555555-5555-5555-5555-555555555555', job='B-77')
    ENTRY_B = dict(client_key='client-b', site_id=SITE, list_id=B['list_id'], job='B-77', verified=True)

    def test_run_once_requires_a_router(self):
        with self.assertRaises(TypeError):
            self.go(dict(enabled=True, assignments=[assignment()]), 'r', router=FakePublisher(self, assignment()))
        self.assertEqual(self.fetched, [])

    def test_missing_route_fails_before_any_read_or_render(self):
        config = dict(enabled=True, assignments=[assignment(), assignment(**self.B)])
        out = self.go(config, 'r1', entries=[ENTRY, self.ENTRY_B])
        self.assertEqual([(i['code'], i['list_id']) for i in out['issues']], [('no_renderer_for_scope', self.B['list_id'])])
        self.assertEqual(([f[0] for f in self.fetched], [r[:2] for r in self.renders]), ([LIST, LIST], [('synthetic', 'synthetic')]))

    def test_each_scope_gets_only_its_own_publisher(self):
        router = RendererRouter([FakePublisher(self, assignment()), FakePublisher(self, self.B)])
        config = dict(enabled=True, assignments=[assignment(), assignment(**self.B)])
        out = self.go(config, 'r1', entries=[ENTRY, self.ENTRY_B], router=router)
        self.assertEqual(out['counts'], {'draft_ready': 2})
        self.assertEqual(sorted(r[:2] for r in self.renders), [('client-b', 'client-b'), ('synthetic', 'synthetic')])

    def test_router_rejects_duplicates_unbound_and_unversioned_publishers(self):
        with self.assertRaises(ValueError):
            RendererRouter([FakePublisher(self, assignment()), FakePublisher(self, assignment())])
        with self.assertRaises(ValueError):
            RendererRouter([NativeDraftPublisher(self.folder.name, MAP, {})])           # no binding: cannot be routed
        class NoVersion:
            binding = binding(assignment())
            def __call__(self, s, op): pass
        with self.assertRaises(TypeError):
            RendererRouter([NoVersion()])
        with self.assertRaises(LookupError):
            self.router.for_scope(self.B)

    def test_worker_refuses_a_publisher_bound_to_another_scope(self):
        scope_b = scope_key(dict(self.B, business_date='2026-11-10', shift='1', zone='UTC'))
        worker = ScheduledWorker(self.store, self.fetch, FakePublisher(self, assignment()), clock=self.clock, enabled=True)
        self.assertEqual(worker.tick(scope_b, {}), dict(status='error', error_type='RendererBindingMismatch'))
        self.assertEqual((self.fetched, self.renders), ([], []))

    def test_receipt_from_another_binding_or_version_is_refused(self):
        class Lying(FakePublisher):
            def __call__(self, snapshot, op):
                return dict(super().__call__(snapshot, op), binding=binding(RendererRouting.B))
        out = self.go(dict(enabled=True, assignments=[assignment()]), 'r1', router=RendererRouter([Lying(self, assignment())]))
        self.assertEqual(out['counts'], {'uncertain': 1})
        with self.store.transaction('outbox:' + out['results'][0]['scope']) as box:
            self.assertEqual(box, {})

    def test_changed_mapping_header_or_template_is_a_new_operation(self):
        config = dict(enabled=True, assignments=[assignment()])
        first = self.go(config, 'r1')['results'][0]
        changed = RendererRouter([FakePublisher(self, assignment(), version='v2')])
        second = self.go(config, 'r2', router=changed)['results'][0]
        self.assertEqual((first['status'], second['status'], len(self.renders)), ('draft_ready', 'draft_ready', 2))   # rendered again
        self.assertNotEqual(first['operation_id'], second['operation_id'])
        with self.store.transaction('outbox:' + first['scope']) as box:
            self.assertEqual({k: v['status'] for k, v in box['entries'].items()},
                             {first['operation_id']: 'superseded', second['operation_id']: 'pending'})


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
        self.assertEqual(self.go(config, 'r3')['counts'], {'draft_ready': 3})

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


class CursorIsolation(Harness):
    """The rotation cursor belongs to one receipt namespace (hosted: verified principal + approved snapshot)."""
    SHIFTS = ['1', '2', '3', '4', '5']

    def one(self, run_id, namespace, **kw):
        out = self.go(dict(enabled=True, assignments=[assignment(recorded_shifts=self.SHIFTS)]), run_id, max_scopes=1,
                      receipt_namespace=namespace, **kw)
        return out['results'][0]['shift']

    def test_repro_shared_cursor_let_one_caller_starve_another(self):
        # Before: B took one scope per run, and A ran in between. A's runs moved the shared cursor, so B's next run
        # started wherever A had left it. Here A always leaves it in the same place, and B never left that scope.
        order = [self.one('solo-%d' % i, 'namespace-solo') for i in range(5)]
        self.assertEqual(sorted(order), self.SHIFTS)                       # alone, B reaches every scope in five runs
        seen = []
        for i in range(5):
            seen.append(self.one('b-%d' % i, 'namespace-b'))
            for j in range(4):                                             # A: four single-scope runs in between
                self.one('a-%d-%d' % (i, j), 'namespace-a')
        self.assertEqual(sorted(seen), self.SHIFTS)                        # B still reaches every scope
        self.assertEqual(seen, order)                                      # in exactly its own order

    def test_each_namespace_has_its_own_cursor_record_and_none_touches_the_legacy_one(self):
        self.one('x', 'namespace-a'); self.one('y', 'namespace-b')
        import sqlite3
        with sqlite3.connect(self.store.path) as db:
            keys = sorted(k for (k,) in db.execute("SELECT key FROM scan_draft_state WHERE key LIKE 'runner:cursor%' AND value != '{}'"))
        self.assertEqual(len(keys), 2); self.assertNotIn('runner:cursor', keys)
        self.assertEqual(self.one('z', None), self.one('z2', 'namespace-c'))   # a fresh namespace and the legacy cursor both start at the beginning
        with self.store.transaction('runner:cursor') as legacy:
            self.assertTrue(legacy.get('after'))

    def test_cursor_survives_restart_within_its_namespace(self):
        first = self.one('r1', 'namespace-a')
        self.store = StateStore(self.store.path)
        second = self.one('r2', 'namespace-a')
        self.assertNotEqual(first, second)

    def test_provider_cooldown_and_scope_leases_stay_shared_on_purpose(self):
        self.fail[LIST] = CaptureFailure('Throttled.', dict(status=429, cooldown=dict(retry_after='600', not_before='2026-11-10T21:45:00+00:00', known=True)))
        self.one('a1', 'namespace-a')
        self.fail.clear(); reads = len(self.fetched)
        out = self.go(dict(enabled=True, assignments=[assignment(recorded_shifts=self.SHIFTS)]), 'b1', receipt_namespace='namespace-b')
        self.assertEqual((out['counts'], len(self.fetched)), ({'cooldown': 5}, reads))   # the source asked everyone to wait


# ---- two real clients through the real native publisher ----------------------------------------------------------
def native_capture(scope, mapping, values, captured_at, extra):
    """A sealed synthetic capture whose column names follow THIS client's mapping."""
    raw = {mapping['fields'][k]: v for k, v in values.items()}; raw.update(extra)
    columns = {name: {'name': name, 'displayName': name} for name in mapping['fields'].values()}
    columns['Date'].update(id='44444444-4444-4444-4444-444444444444', dateTime={'format': 'dateOnly'})
    rows = [dict(id='1', version='"1,1"', modified=scope['business_date'] + 'T12:00:00Z', day=scope['business_date'], date_basis='business_date', raw=raw)]
    return sf.seal(dict(version=sf.VERSION, scope=sf.scope(scope), captured_at=captured_at, date_contract=None, columns=columns,
                        records=rows, photos=[], uncertain_dates=[], raw_all_records=[],
                        coverage=dict(items_terminal=True, columns_terminal=True, remaining_cursor=None,
                                      pages=[dict(kind=k, status=200, page_hash=sf.digest(k), terminal=True, next_cursor_hash=None) for k in ('items', 'columns')],
                                      meaning='Complete observed traversal only.')))


def cells(path):
    from openpyxl import load_workbook
    book = load_workbook(path, data_only=True)
    try:
        return {sheet.title + '!' + c.coordinate: c.value for sheet in book for row in sheet.iter_rows() for c in row if c.value is not None}
    finally:
        book.close()


MAP_B = deepcopy(MAP)
MAP_B['fields'].update(shift='Crew', inspected='QtyChecked', rejected='QtyBad', certified='QtyGood', comments='Remarks')
A = dict(client_id='client-a', site_id=SITE, list_id=LIST, job='A-100')
B = dict(client_id='client-b', site_id=SITE.replace('2222', '8888'), list_id='55555555-5555-5555-5555-555555555555', job='B-200')
HEADER_A = dict(paying_client='Alpha Stamping Corp', worksite='Alpha Plant 1', evidence='Synthetic test header for client A')
HEADER_B = dict(paying_client='Bravo Castings Inc', worksite='Bravo Foundry', evidence='Synthetic test header for client B')
VALUES_A = dict(date='2026-11-10', shift='1', location='Factory', part='00001234', identifier='0000000123', inspected_for='Surface',
                inspected=10, condition='NOK', reject_reason='Scratch', rejected=2, certified=8, comments='Two scratched parts')
VALUES_B = dict(VALUES_A, part='00009999', identifier='0000000999', inspected=40, rejected=5, certified=35, reject_reason='Porosity', comments='Five porous castings')


class MixedClientsNative(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(); self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name); self.now = '2026-11-10T21:35:00Z'; self.reads = []
        self.a = dict(assignment(**A), mapping=MAP, header=HEADER_A)
        self.b = dict(assignment(**B), mapping=MAP_B, header=HEADER_B, shift_field='Crew', scan_field='ScannedAt')
        self.entries = [dict(client_key='client-a', site_id=A['site_id'], list_id=A['list_id'], job='A-100', verified=True),
                        dict(client_key='client-b', site_id=B['site_id'], list_id=B['list_id'], job='B-200', verified=True)]
        self.store = StateStore(self.root / 'state.db')

    def fetch(self, scope, settings):
        self.reads.append(scope['client_id'])
        mapping, values, scan = (MAP, VALUES_A, 'ActualScan') if scope['client_id'] == 'client-a' else (MAP_B, VALUES_B, 'ScannedAt')
        return source_adapter(scope, settings, capture_fn=lambda s: (native_capture(dict(s, business_date=scope['business_date']), mapping, values, self.now, {scan: '2026-11-10T20:30:00Z'}), {}))

    def go(self, assignments, run_id, router=None):
        router = router or NativeDraftRouter(self.root / 'drafts', assignments)
        return runner.run_once(self.store, dict(enabled=True, assignments=assignments), fetch=self.fetch, render=router,
                               run_id=run_id, entries=self.entries, clock=lambda: self.now), router

    def draft(self, router, bound, op):
        target = router.for_scope(bound).output / op
        return cells(target / 'draft.xlsx'), json.loads((target / 'checklist.json').read_text()), json.loads((target / 'receipt.json').read_text())

    def test_each_client_gets_its_own_header_mapping_and_totals(self):
        out, router = self.go([self.a, self.b], 'r1')
        self.assertEqual((out['counts'], out['issues']), ({'draft_ready': 2}, []))
        by = {r['job']: r['operation_id'] for r in out['results']}
        book_a, check_a, receipt_a = self.draft(router, A, by['A-100'])
        book_b, check_b, receipt_b = self.draft(router, B, by['B-200'])
        text_a, text_b = ' | '.join(str(v) for v in book_a.values()), ' | '.join(str(v) for v in book_b.values())
        self.assertIn('Alpha Stamping Corp', text_a); self.assertIn('Alpha Plant 1', text_a)
        self.assertIn('Bravo Castings Inc', text_b); self.assertIn('Bravo Foundry', text_b)
        for leaked in ('Bravo', 'B-200', 'Porosity', '00009999'):
            self.assertNotIn(leaked, text_a, leaked)
        for leaked in ('Alpha', 'A-100', 'Scratch', '00001234'):
            self.assertNotIn(leaked, text_b, leaked)
        # totals read through each client's OWN column mapping (B's quantities live in QtyChecked/QtyBad/QtyGood)
        self.assertEqual((book_a['Summary!G24'], book_a['Summary!K24'], book_a['Summary!I24']), (10, 2, 8))
        self.assertEqual((book_b['Summary!G24'], book_b['Summary!K24'], book_b['Summary!I24']), (40, 5, 35))
        self.assertEqual((check_a['binding'], check_b['binding']), (A, B))
        self.assertEqual((receipt_a['binding'], receipt_b['binding']), (A, B))
        self.assertNotEqual(receipt_a['render_version'], receipt_b['render_version'])
        self.assertTrue(check_a['verification']['native_layout_preserved'] and check_b['verification']['native_layout_preserved'])
        self.assertNotEqual(router.for_scope(A).output, router.for_scope(B).output)        # separate folders

    def test_assignment_without_its_own_mapping_and_header_is_not_built_with_anothers(self):
        bare_b = {k: v for k, v in self.b.items() if k not in ('mapping', 'header')}
        out, router = self.go([self.a, bare_b], 'r1')
        self.assertEqual([(i['code'], i['job']) for i in out['issues']], [('no_renderer_for_scope', 'B-200')])
        self.assertEqual((out['counts'], self.reads.count('client-b')), ({'draft_ready': 1}, 0))
        with self.assertRaises(LookupError):
            router.for_scope(B)
        self.assertEqual(len(list((self.root / 'drafts').iterdir())), 1)

    def test_qa_repro_one_shared_publisher_is_refused(self):
        shared = NativeDraftPublisher(self.root / 'shared', MAP, HEADER_A)                 # the old way: one object for everyone
        with self.assertRaises(TypeError):
            self.go([self.a, self.b], 'r1', router=shared)
        with self.assertRaises(ValueError):
            RendererRouter([shared])
        self.assertEqual((self.reads, (self.root / 'shared').exists()), ([], False))

    def test_bound_publisher_refuses_another_clients_snapshot_before_writing(self):
        pub_a = NativeDraftPublisher(self.root / 'a', MAP, HEADER_A, binding=A)
        scope_b = scope_key(dict(B, business_date='2026-11-10', shift='1', zone='America/Chicago'))
        snapshot = self.fetch(scope_b, self.b)
        with self.assertRaises(ValueError):
            pub_a(snapshot, 'op-b')
        self.assertFalse((self.root / 'a').exists())

    def test_header_change_makes_a_new_operation_and_never_reuses_the_receipt(self):
        first, router = self.go([self.a], 'r1')
        changed = dict(self.a, header=dict(HEADER_A, worksite='Alpha Plant 2'))
        second, router2 = self.go([changed], 'r2')
        op1, op2 = first['results'][0]['operation_id'], second['results'][0]['operation_id']
        self.assertNotEqual(op1, op2)
        book1, _, r1 = self.draft(router, A, op1); book2, _, r2 = self.draft(router2, A, op2)
        self.assertIn('Alpha Plant 1', ' | '.join(map(str, book1.values())))
        self.assertIn('Alpha Plant 2', ' | '.join(map(str, book2.values())))
        self.assertNotEqual(r1['render_version'], r2['render_version'])
        with self.assertRaises(ValueError):                                                # old draft, new configuration
            router2.for_scope(A)(dict(scope=scope_key(dict(A, business_date='2026-11-10', shift='1', zone='America/Chicago'))), op1)

    def test_unknown_value_is_kept_blank_never_zero(self):
        global VALUES_A
        keep = VALUES_A
        try:
            VALUES_A = dict(keep, rejected=None)                                           # NOK count not recorded at the source
            out, router = self.go([self.a], 'r1')
        finally:
            VALUES_A = keep
        book, check, _ = self.draft(router, A, out['results'][0]['operation_id'])
        self.assertNotIn('Summary!K24', book)
        self.assertEqual((book['Summary!G24'], book['Summary!I24']), (10, 8))
        self.assertTrue(any(f['field'] == 'rejected' and f['code'] == 'missing_value' for f in check['findings']))
        self.assertEqual((check['client_release'], check['status'], check['day_completion']), ('held', 'DRAFT', 'not_established'))
        with self.store.transaction('outbox:' + out['results'][0]['scope']) as box:
            self.assertEqual([(v['status'], v['audience']) for v in box['entries'].values()], [('pending', 'Lauren')])


if __name__ == '__main__':
    unittest.main()

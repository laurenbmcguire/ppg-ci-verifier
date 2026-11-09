"""The inbox on real Postgres tables (as on Supabase), including the one-time copy out of the old snapshot.

Needs a throw-away Postgres: run `bash scripts/pg-test-db.sh start`, then set CI_PG_TEST_URL to the line it prints, e.g.
postgresql://ci_verifier:test@/postgres?host=/var/tmp/pgtest&port=55432
These tests empty every ci_verifier table first, so tests/pg_safety.py refuses any database that is not a local socket /
localhost and not marked with ci_verifier.disposable_test_database.
"""
import base64, json, os, sys, threading, unittest, urllib.error, urllib.request
from concurrent.futures import ThreadPoolExecutor
from http.server import ThreadingHTTPServer
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(__file__))
from eos_fixture import build, ROWS

URL = os.environ.get('CI_PG_TEST_URL')


@unittest.skipUnless(URL, 'set CI_PG_TEST_URL to run against a real Postgres')
class NativeTables(unittest.TestCase):
    def setUp(self):
        import psycopg
        from pg_safety import assert_disposable
        assert_disposable(URL)                                              # never a live database
        admin = URL.replace('ci_verifier:test@', 'postgres@')
        with psycopg.connect(admin, autocommit=True) as c:
            c.execute('TRUNCATE ci_verifier.ci_verifier_state, ci_verifier.ci_verifier_files, ci_verifier.ci_verifier_upload_parts, '
                      'ci_verifier.spreadsheets, ci_verifier.spreadsheet_checks, ci_verifier.spreadsheet_sources, ci_verifier.client_contacts, '
                      'ci_verifier.settings, ci_verifier.vin_index, ci_verifier.vin_indexed, ci_verifier.dashboards, ci_verifier.api_keys RESTART IDENTITY')
        self.env = patch.dict(os.environ, {'CI_LOCAL_DEV': '1', 'VERCEL': '0'}); self.env.start()
        for k in ('MS_TENANT_ID', 'MS_CLIENT_ID', 'MS_CLIENT_SECRET', 'ANTHROPIC_API_KEY'):
            os.environ.pop(k, None)
        from api.index import handler
        from hosting.postgres_storage import PostgresWorkspace
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), handler)
        self.server.workspace_store = PostgresWorkspace(URL)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f'http://127.0.0.1:{self.server.server_port}'

    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.env.stop()

    def call(self, path, body=None):
        req = urllib.request.Request(self.base + path, data=None if body is None else json.dumps(body).encode(),
                                     headers={'Content-Type': 'application/json', 'Origin': self.base, 'X-Review-Token': 'same-origin'})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def upload(self, day, rows=ROWS, name=None):
        return self.call('/api/spreadsheets/import', {'filename': name or f'2230-OWENS-BASCO - EOS - {day}.xlsx',
                                                      'content_base64': base64.b64encode(build([tuple(r) for r in rows], day=day)).decode()})

    def test_copy_from_snapshot_then_tables(self):
        import psycopg
        with patch.dict(os.environ, {'CI_NATIVE_TABLES': '0'}):           # the old way: everything in the snapshot
            status, old = self.upload('2026-10-06')
            self.assertEqual(status, 200, old)
        with urllib.request.urlopen(self.base + '/api/spreadsheets', timeout=30) as r:   # first request on the tables copies it over
            listed = json.loads(r.read())['spreadsheets']
        self.assertEqual([x['file_hash'] for x in listed], [old['item']['file_hash']])
        with psycopg.connect(URL) as c:
            self.assertEqual(c.execute('SELECT count(*) FROM spreadsheets').fetchone()[0], 1)
            self.assertTrue(c.execute("SELECT 1 FROM settings WHERE key='_migrated_from_snapshot'").fetchone())
        # new work lands in the tables
        status, new = self.upload('2026-10-07', [tuple(r) for r in ROWS[:3]] + [tuple(r) for r in ROWS[4:]])
        self.assertEqual(status, 200, new)
        h = new['item']['file_hash']
        with urllib.request.urlopen(self.base + '/api/spreadsheet?id=' + h, timeout=30) as r:
            item = json.loads(r.read())
        self.assertEqual(item['file_hash'], h)
        self.assertTrue(item['other_days'])                                   # VIN index works across the copied and new reports
        status, a = self.call('/api/spreadsheets/approve', {'file_hash': h, 'by': 'Lauren', 'note': '', 'expected_check_hash': item['check_hash'], 'expected_run': item['review']['run']})
        self.assertIn(status, (200, 409), a)
        # Header metadata is source-scoped, never guessed from a job label.
        st, unscoped = self.call('/api/settings/report-header', {'job': '2230-OWENS-BASCO', 'header': {'customer': 'Synthetic'}})
        self.assertEqual(st, 409, unscoped)
        from hosting import registry
        from hosting.spreadsheet_inbox import SpreadsheetInbox
        source = dict(site_id='site-synthetic', list_id='list-synthetic')
        with self.server.workspace_store.session() as directory:
            SpreadsheetInbox(directory).set_setting(registry.REGISTRY_KEY, [dict(
                job='2230-OWENS-BASCO', client='Synthetic', client_key='Synthetic', site='Synthetic',
                verified=True, **source)])
        st, saved = self.call('/api/settings/report-header', {'job': '2230-OWENS-BASCO', 'source': source, 'header': {'customer': 'Synthetic'}})
        self.assertEqual(st, 200, saved)
        self.assertEqual(saved['header']['customer'], 'Synthetic')
        self.assertEqual(saved['source_identity'], source)
        status, hits = self.call('/api/vins/search', {'q': ROWS[0][0]})
        self.assertEqual(status, 200, hits)
        with psycopg.connect(URL) as c:
            self.assertEqual(c.execute('SELECT count(*) FROM spreadsheets').fetchone()[0], 2)
            self.assertGreater(c.execute('SELECT count(*) FROM vin_index').fetchone()[0], 10)
            self.assertEqual(c.execute('SELECT count(*) FROM reports_overview').fetchone()[0], 2)

    def test_parallel_requests_do_not_block_each_other(self):
        status, first = self.upload('2026-10-07')
        self.assertEqual(status, 200, first)
        h = first['item']['file_hash']
        def get(_):
            with urllib.request.urlopen(self.base + '/api/spreadsheet?id=' + h, timeout=60) as r:
                return r.status
        with ThreadPoolExecutor(8) as pool:
            results = list(pool.map(get, range(16)))
            uploads = list(pool.map(lambda d: self.upload(d)[0], ['2026-10-0' + str(i) for i in range(1, 6)]))
        self.assertEqual(set(results), {200}); self.assertEqual(set(uploads), {200})


@unittest.skipUnless(URL, 'set CI_PG_TEST_URL to run against a real Postgres')
class PgDecisions(NativeTables):
    """Review decisions on real Postgres: concurrent tabs, a repeated request and a batch with one bad finding."""

    def setUp(self):
        super().setUp()
        from hosting.spreadsheet_inbox import SpreadsheetInbox
        import sl2200_review_fixture as fx
        self.fx = fx
        with self.server.workspace_store.session() as directory:
            self.h = fx.build(SpreadsheetInbox(directory), complete=True)['file_hash']
        status, view = self.call('/api/spreadsheet?id=' + self.h)
        self.assertEqual(status, 200, view)
        self.L = fx.l_ids(view['review'])
        self.displayed = view
        self.view = view

    def decide(self, ids, rid, rev=0, action='reject'):
        return self.call('/api/spreadsheets/decide', dict(file_hash=self.h, ids=ids, action=action, reason='Rework is not recorded.', request_id=rid, expected_rev=rev, expected_run=self.displayed['review']['run'], expected_fps={x['key']:x['fp'] for x in self.displayed['review']['issues']}))

    def saved(self):
        import psycopg
        with psycopg.connect(URL) as c:
            r = json.loads(c.execute('SELECT result_json FROM spreadsheet_checks WHERE file_hash=%s ORDER BY seq DESC LIMIT 1', (self.h,)).fetchone()[0])
        return r

    def test_two_tabs_at_once_one_wins_and_nothing_is_lost(self):
        with ThreadPoolExecutor(8) as pool:
            out = list(pool.map(lambda n: self.decide([self.L[n]], f'tab-{n}'), range(8)))
        codes = sorted(st for st, _ in out)
        self.assertEqual(codes.count(200), 1, codes)
        self.assertEqual(codes.count(409), 7, codes)
        r = self.saved()
        self.assertEqual((r['finding_decisions_rev'], len(r['finding_decisions']), len(r['finding_decision_log'])), (1, 1, 1))

    def test_the_same_request_at_once_is_recorded_once(self):
        with ThreadPoolExecutor(8) as pool:
            out = list(pool.map(lambda n: self.decide(self.L, 'same-click'), range(8)))
        self.assertTrue(all(st in (200, 409) for st, _ in out), [st for st, _ in out])
        r = self.saved()
        self.assertEqual((r['finding_decisions_rev'], len(r['finding_decision_log'])), (1, 1))
        self.assertEqual(len(r['finding_decisions']), 24)

    def test_one_unknown_finding_saves_nothing(self):
        import psycopg
        with psycopg.connect(URL) as c:
            before = c.execute('SELECT count(*) FROM spreadsheet_checks WHERE file_hash=%s', (self.h,)).fetchone()[0]
        st, out = self.decide(self.L + ['check|missing_cell|Inspection Detail|Z999'], 'bad-batch')
        self.assertEqual(st, 409, out)
        with psycopg.connect(URL) as c:
            self.assertEqual(c.execute('SELECT count(*) FROM spreadsheet_checks WHERE file_hash=%s', (self.h,)).fetchone()[0], before)
        self.assertFalse(self.saved().get('finding_decisions'))

    def test_decisions_survive_a_new_connection(self):
        self.assertEqual(self.decide(self.L, 'durable')[0], 200)
        from hosting.postgres_storage import PostgresWorkspace
        from hosting.review_summary import summarize
        from hosting.spreadsheet_inbox import SpreadsheetInbox
        with PostgresWorkspace(URL).session() as directory:
            rv = summarize(SpreadsheetInbox(directory).get(self.h))
        self.assertEqual(rv['decisions']['rejected'], 24)
        self.assertEqual(len(self.fx.m_open(rv)), 24)

    def test_approve_and_defer_at_the_same_moment_never_leave_an_approved_report_with_an_open_question(self):
        rv = self.view['review']
        clip = next(x['key'] for x in rv['issues'] if x['severity'] == 'high')
        approve = lambda: self.call('/api/spreadsheets/approve', dict(file_hash=self.h, approver='Lauren', note='Clip accepted by the customer.', override=True, expected_check_hash=self.displayed['check_hash'], expected_run=self.displayed['review']['run']))
        defer = lambda: self.call('/api/spreadsheets/decide', dict(file_hash=self.h, ids=[clip], action='defer', reason='Waiting for the owner.',
                                                                   owner='List owner', request_id='race', expected_rev=0, **self.fx.shown(rv, [clip])))
        with ThreadPoolExecutor(2) as pool:
            a, d = pool.submit(approve), pool.submit(defer)
            codes = (a.result()[0], d.result()[0])
        r = self.saved()
        deferred = any(v.get('action') == 'defer' for v in (r.get('finding_decisions') or {}).values())
        self.assertFalse(bool(r.get('approval')) and deferred, codes)
        self.assertEqual(sorted(c == 200 for c in codes), [False, True], codes)   # exactly one went through

    def test_concurrent_sends_are_paused_without_reserving_or_mutating(self):
        from hosting.spreadsheet_inbox import SpreadsheetInbox
        st, out = self.call('/api/spreadsheets/approve', dict(file_hash=self.h, approver='Lauren', note='Clip accepted by the customer.', override=True, expected_check_hash=self.displayed['check_hash'], expected_run=self.displayed['review']['run']))
        self.assertEqual(st, 200, out)
        before = self.saved()
        def reserve(_):
            try:
                with self.server.workspace_store.session() as directory:
                    return SpreadsheetInbox(directory).verify_for_delivery(self.h)[1]
            except ValueError as exc:
                return 'refused: ' + str(exc)
        with ThreadPoolExecutor(6) as pool:
            got = list(pool.map(reserve, range(6)))
        self.assertTrue(all(g.startswith('refused') and 'paused' in g for g in got), got)
        self.assertEqual(self.saved(), before)

    test_copy_from_snapshot_then_tables = None
    test_parallel_requests_do_not_block_each_other = None


@unittest.skipUnless(URL, 'set CI_PG_TEST_URL to run against a real Postgres')
class PgStaleClick(NativeTables):
    """The reviewer saw SharePoint say 80; it was read again (75) before the click: refused, on real Postgres."""

    def test_the_click_made_on_the_old_value_is_refused(self):
        from hosting.spreadsheet_inbox import SpreadsheetInbox
        import sl2200_review_fixture as fx
        from test_review_races import sp_report, reread
        with self.server.workspace_store.session() as directory:
            h = sp_report(SpreadsheetInbox(directory))['file_hash']
        status, view = self.call('/api/spreadsheet?id=' + h)
        x = next(i for i in view['review']['issues'] if i.get('source') == 'SharePoint' and i.get('cell') == 'J5')
        with self.server.workspace_store.session() as directory:
            reread(SpreadsheetInbox(directory), h, 75, 'snap-2')
        st, out = self.call('/api/spreadsheets/decide', dict(file_hash=h, ids=[x['key']], action='defer', reason='Owner to confirm', request_id='c',
                                                             expected_rev=0, **fx.shown(view['review'], [x['key']])))
        self.assertEqual(st, 409, out)
        status, after = self.call('/api/spreadsheet?id=' + h)
        self.assertEqual(after['review']['decisions']['rev'], 0)

    test_copy_from_snapshot_then_tables = None
    test_parallel_requests_do_not_block_each_other = None


if __name__ == '__main__':
    unittest.main()


@unittest.skipUnless(URL, 'set CI_PG_TEST_URL to run against a real Postgres')
class PgSafetyGates(NativeTables):
    """Deterministic overlapping transactions on the guarded disposable PostgreSQL database."""
    test_copy_from_snapshot_then_tables = None
    test_parallel_requests_do_not_block_each_other = None

    def setUp(self):
        super().setUp()  # asserts disposable marker before truncation or any other write
        from hosting.spreadsheet_inbox import SpreadsheetInbox
        with self.server.workspace_store.session() as directory:
            self.item = SpreadsheetInbox(directory).ingest('synthetic.csv', b'Part,Inspected,Rejected\nTEST-001,10,1\n')
        self.h = self.item['file_hash']

    def approval(self, box):
        from hosting.review_summary import check_run
        return box.approve(self.h, 'Synthetic reviewer', note='Synthetic review', override=True,
                           expected_check_hash=self.item['check_hash'], expected_run=check_run(self.item))

    def overlap(self, competing, holder):
        """The competing request attempts the real advisory lock while holder owns it."""
        from hosting.spreadsheet_inbox import SpreadsheetInbox
        from hosting.postgres_storage import PostgresWorkspace
        attempted = threading.Event()
        def worker():
            with PostgresWorkspace(URL).session() as directory:
                box = SpreadsheetInbox(directory)
                begin = box._begin
                def waiting(db, key):
                    attempted.set()
                    begin(db, key)
                box._begin = waiting
                return competing(box)
        with ThreadPoolExecutor(1) as pool:
            with PostgresWorkspace(URL).session() as directory:
                box = SpreadsheetInbox(directory)
                with box.connect() as db:
                    box._begin(db, self.h)
                    future = pool.submit(worker)
                    self.assertTrue(attempted.wait(10), 'Competing transaction never attempted report lock')
                    self.assertFalse(future.done(), 'Competing write bypassed the held report lock')
                    holder(box)
            return future.result(timeout=15)

    def test_approval_waits_then_refuses_new_hold_revision(self):
        with self.assertRaisesRegex(ValueError, 'changed'):
            self.overlap(self.approval, lambda box: box._update(self.h, lambda r: r.update(
                source_reconciliation={'status':'incomplete','coverage':{'notes':['Synthetic new source hold']}})))
        self.assertFalse(self.saved().get('approval'))

    def test_decision_waits_then_refuses_concurrent_approval(self):
        from hosting.review_summary import check_run
        def decide(box):
            return box.decide_findings(self.h, ids=['synthetic'], action='confirm', request_id='r1', expected_rev=0,
                                      expected_fps={'synthetic':'shown'}, expected_run=check_run(self.item))
        with self.assertRaisesRegex(ValueError,'approved'):
            self.overlap(decide, self.approval)
        self.assertTrue(self.saved().get('approval'))
        self.assertFalse(self.saved().get('finding_decisions'))

    def test_correction_waits_then_refuses_concurrent_approval_before_ingest(self):
        def correct(box):
            return box.apply_fixes(self.h, [self.change()], expected_check_hash=self.item['check_hash'])
        with self.assertRaisesRegex(ValueError,'Approved'):
            self.overlap(correct, self.approval)
        self.assertEqual(self.report_count(),1)
        self.assertFalse(self.saved().get('fixes_applied'))

    def test_correction_lineage_failure_rolls_back_all_database_writes(self):
        from hosting.spreadsheet_inbox import SpreadsheetInbox
        with self.server.workspace_store.session() as directory:
            box=SpreadsheetInbox(directory)
            save=box.save_check
            def injected(db,result):
                if result.get('fixed_from'):
                    raise ValueError('Injected lineage failure')
                return save(db,result)
            box.save_check=injected
            with self.assertRaisesRegex(ValueError,'Injected'):
                box.apply_fixes(self.h,[self.change()],expected_check_hash=self.item['check_hash'])
        self.assertEqual(self.report_count(),1)
        self.assertFalse(self.saved().get('fixes_applied'))

    def test_successful_manual_correction_keeps_original_and_atomic_lineage(self):
        from hosting.spreadsheet_inbox import SpreadsheetInbox
        with self.server.workspace_store.session() as directory:
            box=SpreadsheetInbox(directory)
            original=box.original_bytes(self.h)[1]
            new=box.apply_fixes(self.h,[self.change()],expected_check_hash=self.item['check_hash'])
            self.assertEqual(box.original_bytes(self.h)[1],original)
            self.assertEqual(new['fixed_from']['file_hash'],self.h)
        self.assertEqual(self.report_count(),2)
        self.assertEqual(self.saved()['fixes_applied'][0]['fixed_file_hash'],new['file_hash'])

    def saved(self):
        from hosting.spreadsheet_inbox import SpreadsheetInbox
        with self.server.workspace_store.session() as directory:
            return SpreadsheetInbox(directory).get(self.h)

    def report_count(self):
        import psycopg
        with psycopg.connect(URL) as c:
            return c.execute('SELECT count(*) FROM spreadsheets').fetchone()[0]

    @staticmethod
    def change():
        return dict(sheet='CSV',cell='A2',before='TEST-001',after='TEST-002',reason='Synthetic correction')

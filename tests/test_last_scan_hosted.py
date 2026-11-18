"""Hosted entry point in DISABLED mode: authentication, dry run, activation preflight, stored drafts.
Synthetic sources only. Nothing here is registered as a route or schedule."""
import json
import re
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from hosting import source_first as sf
from hosting import last_scan_hosted as hosted
from hosting.last_scan_local import StateStore, StoredDraftRouter, StoredDraftPublisher, BlobArtifacts
from hosting.last_scan_service import source_adapter
from hosting.last_scan_store import SqliteStore
from source_first_fixture import capture, MAP, SCOPE

TOKEN = 'synthetic-test-token-' + 'x' * 24          # not a real secret
AUTH = {'authorization': 'Bearer ' + TOKEN}
ENTRY = dict(client_key='synthetic', site_id=SCOPE['site_id'], list_id=SCOPE['list_id'], job=SCOPE['job'], verified=True)
ROOT = Path(__file__).resolve().parents[1]


def assignment(**kw):
    return dict(dict(verified=True, active=True, timestamp_semantics_verified=True, client_id='synthetic', job=SCOPE['job'],
                     site_id=SCOPE['site_id'], list_id=SCOPE['list_id'], zone='America/Chicago', scan_field='ActualScan',
                     shift_field='Shift', recorded_shifts=['1'], business_dates=['2026-11-10'], mapping=MAP, header={}), **kw)


def approve(config):
    return dict(config, activation=dict(approved_by='synthetic reviewer', approved_at='2026-11-10', config_digest=hosted.config_digest(config)))


class Hosted(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(); self.addCleanup(self.folder.cleanup)
        self.path = Path(self.folder.name) / 'state.db'
        self.store = StateStore(self.path); self.now = '2026-11-10T21:35:00Z'; self.reads = []
        self.config = approve(dict(enabled=True, assignments=[assignment()]))

    def fetch(self, scope, settings):
        self.reads.append(scope['shift'])
        def cap(s):
            c = capture(); c['records'][0]['raw']['ActualScan'] = '2026-11-10T20:30:00Z'; c['records'][0]['raw']['Rejected'] = None
            c['captured_at'] = self.now
            return sf.seal({k: v for k, v in c.items() if k not in ('snapshot_hash', 'envelope_hash')}), {}
        return source_adapter(scope, settings, capture_fn=cap)

    def deps(self, **kw):
        config = kw.pop('config', self.config)
        base = dict(expected_token=TOKEN, config=config, entries=[ENTRY], store=self.store, fetch=self.fetch, clock=lambda: self.now,
                    router=StoredDraftRouter(BlobArtifacts(self.store), (config or {}).get('assignments') or []) if isinstance(config, dict) else None)
        base.update(kw)
        return hosted.Deps(**base)

    def call(self, deps=None, body=None, headers=AUTH, method='POST'):
        return hosted.handle(method, headers, dict(run_id='r1') if body is None else body, deps or self.deps())

    def rows(self):
        import sqlite3
        with sqlite3.connect(self.path) as db:
            return db.execute('SELECT count(*) FROM scan_draft_state').fetchone()[0], db.execute('SELECT count(*) FROM scan_draft_blob').fetchone()[0]

    def failed(self, body):
        return [c['check'] for c in body['preflight'] if not c['ok']]


class Authentication(Hosted):
    def test_no_token_configured_means_nothing_runs(self):
        for token in (None, '', 'short', 12345):
            self.assertEqual(self.call(self.deps(expected_token=token), dict(run_id='r', mode='run')), (503, dict(status='not_configured')))
        self.assertEqual((self.reads, self.rows()), ([], (0, 0)))

    def test_wrong_or_missing_token(self):
        for headers in ({}, None, {'authorization': TOKEN}, {'authorization': 'Bearer ' + TOKEN[:-1]}, {'authorization': 'Bearer '},
                        {'authorization': 'Basic ' + TOKEN}, {'x-token': TOKEN}):
            self.assertEqual(self.call(body=dict(run_id='r', mode='run'), headers=headers), (401, dict(status='unauthorized')))
        self.assertEqual((self.reads, self.rows()), ([], (0, 0)))

    def test_method_and_body_are_bounded(self):
        self.assertEqual(self.call(method='GET')[0], 405)
        for body in ('text', dict(), dict(run_id=''), dict(run_id='x' * 201), dict(run_id='r', mode='send'), dict(run_id='r', mode='run', force=True),
                     dict(run_id='r', budget_seconds=-1), dict(run_id='r', max_scopes=True), dict(run_id='r', config=dict(enabled=True))):
            self.assertEqual(self.call(body=body)[0], 400, body)
        self.assertEqual((self.reads, self.rows()), ([], (0, 0)))
        code, body = self.call(body=dict(run_id='r', budget_seconds=9999, max_scopes=9999))
        self.assertEqual((code, body['limits']), (200, dict(budget_seconds=45, max_scopes=50)))

    def test_responses_never_echo_the_token(self):
        for result in (self.call(), self.call(body=dict(run_id='r', mode='run')), self.call(headers={})):
            self.assertNotIn(TOKEN, json.dumps(result[1]))


class DryRunAndActivation(Hosted):
    def test_dry_run_is_the_default_and_touches_nothing(self):
        code, body = self.call()
        self.assertEqual((code, body['status'], body['mode'], body['sources_read'], body['records_written']), (200, 'dry_run', 'dry_run', 0, 0))
        self.assertEqual((body['ready_to_run'], body['planned_scopes'], body['planned'][0]['job'], body['transport'], body['client_release']),
                         (True, 1, SCOPE['job'], 'disabled', 'held'))
        self.assertEqual((self.reads, self.rows()), ([], (0, 0)))

    def test_unconfigured_or_disabled_cannot_run(self):
        cases = dict(no_configuration=None, empty={}, not_enabled=approve(dict(assignments=[assignment()])),
                     enabled_text=approve(dict(enabled='true', assignments=[assignment()])),
                     enabled_one=approve(dict(enabled=1, assignments=[assignment()])))
        for name, config in cases.items():
            with self.subTest(name):
                code, body = self.call(self.deps(config=config), dict(run_id='r-' + name, mode='run'))
                self.assertEqual((code, body['status'], body['sources_read']), (409, 'not_activated', 0))
                self.assertIn('enabled_is_exactly_true', self.failed(body))
        self.assertEqual((self.reads, self.rows()), ([], (0, 0)))

    def test_enabled_without_matching_approval_cannot_run(self):
        bare = dict(enabled=True, assignments=[assignment()])
        changed = dict(self.config, assignments=[assignment(recorded_shifts=['1', '2'])])          # edited after approval
        forged = dict(bare, activation=dict(approved_by='', approved_at='2026-11-10', config_digest=hosted.config_digest(bare)))
        for name, config in dict(no_approval=bare, changed_after_approval=changed, approver_missing=forged,
                                 approval_true=dict(bare, activation=True)).items():
            with self.subTest(name):
                code, body = self.call(self.deps(config=config), dict(run_id='r-' + name, mode='run'))
                self.assertEqual((code, self.failed(body)), (409, ['approval_matches_this_configuration']))
        self.assertEqual((self.reads, self.rows()), ([], (0, 0)))

    def test_each_missing_dependency_blocks_a_run(self):
        class NotInstalled(SqliteStore):
            def ready(self): return False
        class Broken(SqliteStore):
            def ready(self): raise ConnectionError('database unreachable')
        wrong_entry = [dict(ENTRY, client_key='someone-else')]
        cases = dict(job_registry_available=dict(entries=None), renderer_router_available=dict(router=None),
                     source_adapter_configured=dict(fetch=None), durable_store_ready=dict(store=None),
                     no_assignment_issues=dict(entries=wrong_entry))
        for check, change in cases.items():
            with self.subTest(check):
                code, body = self.call(self.deps(**change), dict(run_id='r-' + check, mode='run'))
                self.assertEqual(code, 409); self.assertIn(check, self.failed(body))
        for store in (NotInstalled(Path(self.folder.name) / 'n.db'), Broken(Path(self.folder.name) / 'b.db')):
            code, body = self.call(self.deps(store=store), dict(run_id='r-store', mode='run'))
            self.assertEqual(code, 409); self.assertIn('durable_store_ready', self.failed(body))
        no_map = approve(dict(enabled=True, assignments=[{k: v for k, v in assignment().items() if k not in ('mapping', 'header')}]))
        code, body = self.call(self.deps(config=no_map), dict(run_id='r-nomap', mode='run'))
        self.assertEqual((code, [i['code'] for i in body['issues']]), (409, ['no_renderer_for_scope']))
        self.assertEqual((self.reads, self.rows()), ([], (0, 0)))

    def test_a_provider_contract_attached_here_blocks_the_run(self):
        from test_last_scan_durable import FAKE_CONTRACT
        import hosting.last_scan_transport as transport
        code, body = self.call(self.deps(provider_contract=FAKE_CONTRACT), dict(run_id='r', mode='run'))
        self.assertEqual(code, 200)                                    # the real registry is empty, so it is not "verified"
        real = transport.VERIFIED_CONTRACTS
        try:
            transport.VERIFIED_CONTRACTS = {(FAKE_CONTRACT.provider, FAKE_CONTRACT.version): FAKE_CONTRACT.digest()}
            code, body = self.call(self.deps(provider_contract=FAKE_CONTRACT), dict(run_id='r2', mode='run'))
        finally:
            transport.VERIFIED_CONTRACTS = real
        self.assertEqual((code, self.failed(body)), (409, ['delivery_transport_disabled']))

    def test_approved_run_builds_a_held_draft_in_private_storage_and_sends_nothing(self):
        deps = self.deps()
        self.assertEqual(self.call(deps)[1]['ready_to_run'], True)
        code, body = self.call(deps, dict(run_id='firing-1', mode='run'))
        summary = body['summary']
        self.assertEqual((code, body['status'], summary['counts'], summary['notifications_sent'], summary['client_release']),
                         (200, 'ran', {'draft_ready': 1}, 0, 'held'))
        op = summary['results'][0]['operation_id']
        publisher = deps.router.for_scope(assignment())
        self.assertIsInstance(publisher, StoredDraftPublisher)
        checklist = json.loads(publisher.read(op, 'checklist.json'))
        self.assertEqual((checklist['status'], checklist['client_release'], checklist['day_completion']), ('DRAFT', 'held', 'not_established'))
        self.assertTrue(any(f['field'] == 'rejected' and f['code'] == 'missing_value' for f in checklist['findings']))   # unknown kept
        self.assertEqual(sf.digest(publisher.read(op, 'draft.xlsx')), publisher.lookup(op)['workbook_sha256'])
        self.assertEqual(list(Path(self.folder.name).iterdir()), [self.path])            # no draft files outside the store
        with self.store.transaction('outbox:' + summary['results'][0]['scope']) as box:
            entry = box['entries'][op]
        self.assertEqual((entry['status'], 'attempts' in entry), ('pending', False))      # queued internally, never attempted
        reads = len(self.reads)
        again = self.call(self.deps(), dict(run_id='firing-1', mode='run'))[1]['summary']  # same firing after a restart
        self.assertTrue(again['replayed']); self.assertEqual(len(self.reads), reads)

    def test_stored_draft_survives_restart_and_is_not_rebuilt_or_overwritten(self):
        self.call(self.deps(), dict(run_id='a', mode='run'))
        blobs = self.rows()[1]
        store2 = StateStore(self.path); self.store = store2
        out = self.call(self.deps(), dict(run_id='b', mode='run'))[1]['summary']
        self.assertEqual(out['counts'], {'draft_ready': 1})
        artifacts = BlobArtifacts(store2)
        key = [k for k in self.keys() if k.startswith('artifact:')][0][len('artifact:'):]
        first = artifacts.lookup(key)
        self.assertEqual(artifacts.commit(key, {'draft.xlsx': b'other bytes'}, dict(operation_id='x')), first)   # first writer wins
        self.assertEqual(artifacts.lookup(key), first)

    def keys(self):
        import sqlite3
        with sqlite3.connect(self.path) as db:
            return [r[0] for r in db.execute('SELECT key FROM scan_draft_state')]


class NotConnected(unittest.TestCase):
    """The prepared code must not be reachable from the deployed app."""
    def test_no_route_cron_or_import_reaches_the_runner(self):
        names = re.compile(r'last_scan_(hosted|runner|transport|service|store|local)')
        for path in list((ROOT / 'api').rglob('*.py')) + [ROOT / 'vercel.json'] + list((ROOT / 'web').rglob('*.js')):
            if path.is_file():
                self.assertIsNone(names.search(path.read_text(errors='ignore')), str(path))
        allowed = {'last_scan_hosted.py', 'last_scan_runner.py', 'last_scan_transport.py', 'last_scan_service.py',
                   'last_scan_store.py', 'last_scan_local.py', 'last_scan_drafts.py'}
        for path in (ROOT / 'hosting').glob('*.py'):
            if path.name not in allowed:
                self.assertIsNone(names.search(path.read_text(errors='ignore')), str(path))
        if (ROOT / 'vercel.json').is_file():
            self.assertNotIn('crons', json.loads((ROOT / 'vercel.json').read_text()))

    def test_hosted_module_has_no_network_client_secret_reader_or_sender(self):
        text = (ROOT / 'hosting' / 'last_scan_hosted.py').read_text() + (ROOT / 'hosting' / 'last_scan_transport.py').read_text()
        for forbidden in ('urllib', 'requests', 'http.client', 'smtplib', 'os.environ', 'getenv', 'psycopg', 'socket', 'subprocess', 'install()'):
            self.assertNotIn(forbidden, text.replace('never runs install()', ''), forbidden)
        self.assertNotIn('dispatch', (ROOT / 'hosting' / 'last_scan_hosted.py').read_text())
        self.assertNotIn('dispatch', (ROOT / 'hosting' / 'last_scan_runner.py').read_text())

    def test_install_is_never_called_outside_tests_and_the_local_store(self):
        for path in (ROOT / 'hosting').glob('*.py'):
            text = path.read_text(errors='ignore')
            if path.name == 'last_scan_store.py':
                self.assertEqual(text.count('self.install()'), 1)             # the local SQLite constructor only
            elif path.name.startswith('last_scan_'):
                self.assertNotIn('.install(', text, path.name)


if __name__ == '__main__':
    unittest.main()

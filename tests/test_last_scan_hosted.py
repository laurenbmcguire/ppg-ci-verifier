"""Hosted entry point in DISABLED mode: verified principal, attested approval, replay protection, quota, dry run,
stored drafts. Synthetic sources and FAKE identity/approval systems only. Nothing here is a route or a schedule."""
import json
import re
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path
from unittest import mock
from hosting import source_first as sf
from hosting import last_scan_hosted as hosted
from hosting.last_scan_hosted import ApprovalAuthority, Authorizer, CaptureAuthority, Deps, Principal, Quota, SchemaProbe
from hosting import last_scan_readiness as readiness
from hosting.last_scan_local import StateStore, StoredDraftRouter, StoredDraftPublisher, BlobArtifacts
from hosting.last_scan_service import source_adapter
from hosting.last_scan_store import SqliteStore
from source_first_fixture import capture, MAP, SCOPE
import readiness_fixture as rf

ENTRY = dict(client_key='synthetic', site_id=SCOPE['site_id'], list_id=SCOPE['list_id'], job=SCOPE['job'], verified=True)
ROOT = Path(__file__).resolve().parents[1]
NOW = '2026-11-10T21:35:00Z'
HEADER = dict(paying_client='Approved Client Corp', worksite='Approved Plant', evidence='Synthetic approved header')


class FakeAuthorizer(Authorizer):
    """TEST ONLY stand-in for an identity system: a header value mapped to a principal."""
    def __init__(self, known=None): self.known = known or {'proof-alice': 'alice', 'proof-bob': 'bob'}; self.seen = []
    def verify(self, method, headers, body_digest):
        self.seen.append(body_digest)
        who = self.known.get(headers.get('x-test-principal-proof'))
        return Principal(who) if who else None


class FakeApprovals(ApprovalAuthority):
    """TEST ONLY stand-in for a separately protected approval record."""
    def __init__(self, records=None): self.records = records or {}
    def attestation(self, snapshot_digest): return deepcopy(self.records.get(snapshot_digest))


COLLECTOR = 'synthetic-collector'


class FakeCaptures(CaptureAuthority):
    """TEST ONLY stand-in for a separately protected capture record. Not a collector and not origin proof."""
    def __init__(self, schemas=(), collector=COLLECTOR):
        self.records = {}
        for s in schemas if isinstance(schemas, list) else []:
            if isinstance(s, dict):
                self.add(s, collector)
    def add(self, schema, collector=COLLECTOR, **override):
        identity = readiness.snapshot_identity(schema)
        self.records[identity] = dict(dict(snapshot_identity=identity, site_id=schema.get('site_id'), list_id=schema.get('list_id'),
                                           schema_digest=readiness.schema_digest(schema), collector=collector, captured_at=schema.get('captured_at')), **override)
        return identity
    def receipt(self, snapshot_identity): return deepcopy(self.records.get(snapshot_identity))


class FakeProbe(SchemaProbe):
    """TEST ONLY stand-in for a read-only live schema look. Answers from synthetic snapshots held in memory."""
    def __init__(self, schemas, clock, log=None):
        self.live = {(s.get('site_id'), s.get('list_id')): deepcopy(s) for s in (schemas if isinstance(schemas, list) else []) if isinstance(s, dict)}
        self.clock, self.calls, self.log = clock, 0, log
    def current(self, site_id, list_id):
        self.calls += 1
        if self.log is not None: self.log.append('probe')
        s = self.live.get((site_id, list_id))
        return None if s is None else dict(deepcopy(s), captured_at=self.clock())


ALICE, BOB = {'x-test-principal-proof': 'proof-alice'}, {'x-test-principal-proof': 'proof-bob'}


def bare_assignment(**kw):
    return dict(dict(verified=True, active=True, timestamp_semantics_verified=True, client_id='synthetic', job=SCOPE['job'],
                     site_id=SCOPE['site_id'], list_id=SCOPE['list_id'], zone='America/Chicago', scan_field='ActualScan',
                     shift_field='Shift', recorded_shifts=['1'], business_dates=['2026-11-10'], mapping=MAP, header=HEADER), **kw)


def assignment(**kw):
    """TEST ONLY: pinned and attested by the synthetic fixture reviewer against the synthetic schema below."""
    return rf.attested(bare_assignment(**kw))[0]


def schemas_for(config):
    """TEST ONLY: one synthetic captured snapshot per distinct site + list in the configuration."""
    out, seen = [], set()
    for a in (config.get('assignments') or []) if isinstance(config, dict) and isinstance(config.get('assignments'), list) else []:
        if isinstance(a, dict) and isinstance(a.get('mapping'), dict) and isinstance(a['mapping'].get('fields'), dict) and (a.get('site_id'), a.get('list_id')) not in seen:
            try:
                out.append(rf.schema_for(a))
            except (KeyError, TypeError):
                continue
            seen.add((a.get('site_id'), a.get('list_id')))
    return out


class Hosted(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(); self.addCleanup(self.folder.cleanup)
        self.path = Path(self.folder.name) / 'state.db'
        self.store = StateStore(self.path); self.now = NOW; self.reads = []; self.n = 0
        self.config = dict(enabled=True, assignments=[assignment()])
        self.approvals = FakeApprovals()
        self.approve(self.config)

    def fetch(self, scope, settings):
        self.reads.append(scope['shift'])
        def cap(s):
            c = capture(); c['records'][0]['raw']['ActualScan'] = '2026-11-10T20:30:00Z'; c['records'][0]['raw']['Rejected'] = None
            c['captured_at'] = self.now
            return sf.seal({k: v for k, v in c.items() if k not in ('snapshot_hash', 'envelope_hash')}), {}
        return source_adapter(scope, settings, capture_fn=cap)

    def deps(self, **kw):
        base = dict(authorizer=FakeAuthorizer(), approvals=self.approvals, quota=Quota(5), config=self.config, entries=[ENTRY],
                    store=self.store, artifacts=BlobArtifacts(self.store), fetch=self.fetch, clock=lambda: self.now)
        base.update(kw)
        base.setdefault('schemas', schemas_for(base['config']))
        base.setdefault('captures', FakeCaptures(base['schemas']))
        base.setdefault('collectors', (COLLECTOR,))
        base.setdefault('schema_probe', FakeProbe(base['schemas'], base['clock']))
        return Deps(**base)

    def body(self, **kw):
        self.n += 1
        return dict(dict(run_id='r%d' % self.n, timestamp=self.now, nonce='nonce-%020d' % self.n), **kw)

    def call(self, deps=None, body=..., headers=ALICE, method='POST'):
        return hosted.handle(method, headers, self.body() if body is ... else body, deps or self.deps())

    def approve(self, config, principals=('alice',), entries=None):
        digest = self.call(self.deps(config=config, entries=[ENTRY] if entries is None else entries))[1]['snapshot_digest']
        self.approvals.records[digest] = dict(snapshot_digest=digest, approved_by='synthetic approver', approved_at='2026-11-10', principals=list(principals))
        return digest

    def rows(self):
        with sqlite3.connect(self.path) as db:
            return db.execute('SELECT count(*) FROM scan_draft_state').fetchone()[0], db.execute('SELECT count(*) FROM scan_draft_blob').fetchone()[0]

    def untouched(self):
        self.assertEqual((self.reads, self.rows()), ([], (0, 0)))

    def failed(self, body):
        return [c['check'] for c in body['preflight'] if not c['ok']]


def go(self, deps=None, headers=ALICE, **kw):
    return self.call(deps, self.body(mode='run', **kw), headers)
Hosted.go = go


class VerifiedPrincipal(Hosted):
    def test_no_authorizer_means_deny(self):
        class Duck:                                              # looks like one, is not one
            def verify(self, *a): return Principal('alice')
        for authorizer in (None, True, 'token', lambda *a: Principal('alice'), Duck()):
            self.assertEqual(self.go(self.deps(authorizer=authorizer)), (503, dict(status='not_configured')))
        self.untouched()

    def test_unverified_callers_are_refused(self):
        class Raises(Authorizer):
            def verify(self, *a): raise RuntimeError('identity system down')
        class Wrong(Authorizer):
            def __init__(self, value): self.value = value
            def verify(self, *a): return self.value
        for authorizer in (Raises(), Wrong(None), Wrong('alice'), Wrong(True), Wrong(dict(id='alice')), Wrong(Principal('')), Wrong(Principal(' ')), Wrong(Principal(7))):
            self.assertEqual(self.go(self.deps(authorizer=authorizer)), (401, dict(status='unauthorized')))
        for headers in ({}, {'x-test-principal-proof': 'guess'}, {'authorization': 'Bearer ' + 'x' * 40}, {'x-token': 'proof-alice'}):
            self.assertEqual(self.go(headers=headers), (401, dict(status='unauthorized')))
        self.untouched()

    def test_malformed_headers_fail_closed(self):
        for headers in (None, [], 'x-test-principal-proof: proof-alice', [('x-test-principal-proof', 'proof-alice')],
                        {1: 'proof-alice'}, {'x-test-principal-proof': b'proof-alice'}, {'x-test-principal-proof': 'proof-alice', 'n': 1}):
            self.assertEqual(self.go(headers=headers), (401, dict(status='unauthorized')))
        self.untouched()

    def test_the_authorizer_is_given_the_hash_of_the_exact_body(self):
        deps = self.deps(); body = self.body()
        self.call(deps, body)
        self.assertEqual(deps.authorizer.seen, [sf.digest(body)])

    def test_method_and_body_are_bounded(self):
        self.assertEqual(self.call(method='GET')[0], 405)
        for body in ('text', None, [], dict(), dict(run_id=''), dict(run_id='x' * 201), dict(run_id=5), dict(run_id='r', mode='send'),
                     dict(run_id='r', mode=['run']), dict(run_id='r', mode='run', force=True), dict(run_id='r', config=dict(enabled=True)),
                     dict(run_id='r', router='mine'), dict(run_id='r', approved=True)):
            body = dict(body, timestamp=NOW, nonce='n' * 20) if isinstance(body, dict) and body else body
            self.assertEqual(self.call(body=body)[0], 400, body)
        self.untouched()

    def test_qa_repro_nan_and_other_non_finite_or_non_whole_limits_are_rejected(self):
        nan, inf = float('nan'), float('inf')
        for bad in (dict(budget_seconds=nan), dict(max_scopes=nan), dict(budget_seconds=inf), dict(max_scopes=inf), dict(budget_seconds=-inf),
                    dict(budget_seconds=0), dict(budget_seconds=-1), dict(max_scopes=0), dict(max_scopes=2.5), dict(max_scopes=3.0),
                    dict(max_scopes='3'), dict(budget_seconds='40'), dict(max_scopes=True), dict(budget_seconds=False),
                    dict(max_scopes=None), dict(budget_seconds=[40]), dict(nonce=nan), dict(run_id='r', mode='run', budget_seconds=nan)):
            for mode in ('dry_run', 'run'):
                code, out = self.call(body=self.body(**dict(dict(mode=mode), **bad)))
                self.assertEqual((code, out['status']), (400, 'bad_request'), bad)
        self.untouched()
        code, out = self.call(body=self.body(budget_seconds=9999, max_scopes=9999))
        self.assertEqual((code, out['limits']), (200, dict(budget_seconds=45, max_scopes=50)))
        code, out = self.call(body=self.body(budget_seconds=0.5, max_scopes=1))
        self.assertEqual((code, out['limits']), (200, dict(budget_seconds=0.5, max_scopes=1)))

    def test_requests_must_be_dated_and_fresh(self):
        for stamp in (None, '', 'yesterday', '2026-11-10T21:35:00', '2026-11-10', '2026-11-10T21:29:59Z', '2026-11-10T21:40:01Z', 1794346500):
            for mode in ('dry_run', 'run'):
                self.assertEqual(self.call(body=self.body(mode=mode, timestamp=stamp)), (401, dict(status='stale_or_undated_request')), stamp)
        self.untouched()
        self.assertEqual(self.call(body=self.body(timestamp='2026-11-10T21:30:00Z'))[0], 200)

    def test_a_run_needs_a_single_use_nonce(self):
        for nonce in (None, '', 'short', 'x' * 201, 12345678901234567890, ['n' * 20]):
            self.assertEqual(self.call(body=self.body(mode='run', nonce=nonce))[0], 400, nonce)
        self.untouched()

    def test_responses_never_echo_credentials(self):
        for result in (self.call(), self.go(), self.go(headers={})):
            self.assertNotIn('proof-alice', json.dumps(result[1]))


class AttestedApproval(Hosted):
    def test_dry_run_is_the_default_and_touches_nothing(self):
        code, body = self.call()
        self.assertEqual((code, body['status'], body['mode'], body['sources_read'], body['records_written']), (200, 'dry_run', 'dry_run', 0, 0))
        self.assertEqual((body['ready_to_run'], body['planned_scopes'], body['planned'][0]['job'], body['transport'], body['client_release'], body['principal']),
                         (True, 1, SCOPE['job'], 'disabled', 'held', 'alice'))
        self.assertEqual(len(body['snapshot_digest']), 64)
        self.untouched()

    def test_unconfigured_or_disabled_cannot_run(self):
        for name, config in dict(no_configuration=None, empty={}, not_enabled=dict(assignments=[assignment()]),
                                 enabled_text=dict(enabled='true', assignments=[assignment()]),
                                 enabled_one=dict(enabled=1, assignments=[assignment()])).items():
            with self.subTest(name):
                if isinstance(config, dict) and config.get('assignments'):
                    self.approve(config)                          # even an attested snapshot does not switch it on
                code, body = self.go(self.deps(config=config))
                self.assertEqual((code, body['status'], body['sources_read']), (409, 'not_activated', 0))
                self.assertIn('enabled_is_exactly_true', self.failed(body))
        self.untouched()

    def test_qa_repro_self_written_activation_hash_is_not_approval(self):
        bare = dict(enabled=True, assignments=[assignment(recorded_shifts=['1', '2'])])     # never attested
        digest = self.call(self.deps(config=bare))[1]['snapshot_digest']
        for activation in (dict(approved_by='me', approved_at='2026-11-10', config_digest=digest, snapshot_digest=digest, principals=['alice']),
                           True, digest):
            code, body = self.go(self.deps(config=dict(bare, activation=activation)))
            self.assertEqual((code, self.failed(body)), (409, ['approval_attested_for_this_snapshot_and_principal']))
        self.untouched()

    def test_approval_authority_missing_wrong_or_unreadable_rejects(self):
        class Duck:
            def attestation(self, d): return dict(snapshot_digest=d, approved_by='x', approved_at='y', principals=['alice'])
        class Down(ApprovalAuthority):
            def attestation(self, d): raise ConnectionError('approval system down')
        class Says(ApprovalAuthority):
            def __init__(self, make): self.make = make
            def attestation(self, d): return self.make(d)
        good = lambda d: dict(snapshot_digest=d, approved_by='approver', approved_at='2026-11-10', principals=['alice'])
        bad = [None, True, Duck(), Down(), Says(lambda d: None), Says(lambda d: True), Says(lambda d: d),
               Says(lambda d: dict(good(d), snapshot_digest='0' * 64)), Says(lambda d: dict(good(d), approved_by='')),
               Says(lambda d: dict(good(d), approved_at=None)), Says(lambda d: dict(good(d), principals=['bob'])),
               Says(lambda d: dict(good(d), principals='alice')), Says(lambda d: dict(good(d), principals=None)),
               Says(lambda d: {k: v for k, v in good(d).items() if k != 'principals'})]
        for approvals in bad:
            code, body = self.go(self.deps(approvals=approvals))
            self.assertEqual((code, self.failed(body)), (409, ['approval_attested_for_this_snapshot_and_principal']), approvals)
        self.untouched()
        self.assertEqual(self.go(self.deps(approvals=Says(good)))[0], 200)

    def test_approval_is_for_named_principals_only(self):
        code, body = self.go(headers=BOB)
        self.assertEqual((code, body['principal'], self.failed(body)), (409, 'bob', ['approval_attested_for_this_snapshot_and_principal']))
        self.untouched()

    def test_qa_repro_altered_header_or_mapping_after_approval_is_rejected_before_any_read(self):
        with self.assertRaises(TypeError):
            Deps(router=StoredDraftRouter(BlobArtifacts(self.store), [assignment(header=dict(HEADER, paying_client='Someone Else Inc'))]))
        drifted = dict(
            header=dict(self.config, assignments=[assignment(header=dict(HEADER, paying_client='Someone Else Inc'))]),
            mapping=dict(self.config, assignments=[assignment(mapping=dict(MAP, fields=dict(MAP['fields'], rejected='Certified')))]),
            shifts=dict(self.config, assignments=[assignment(recorded_shifts=['1', '2'])]),
            scan_field=dict(self.config, assignments=[assignment(scan_field='OtherScan')]),
            extra_assignment=dict(self.config, assignments=[assignment(), assignment(list_id='55555555-5555-5555-5555-555555555555')]),
            extra_key=dict(self.config, note='harmless?'))
        for name, config in drifted.items():
            with self.subTest(name):
                code, body = self.go(self.deps(config=config))
                self.assertEqual(code, 409); self.assertIn('approval_attested_for_this_snapshot_and_principal', self.failed(body))
        self.untouched()

    def test_template_generator_or_registry_change_after_approval_is_rejected(self):
        from hosting import source_first_workbook as wb
        with mock.patch.object(wb, 'generator_hash', lambda: 'changed-generator-code'):
            code, body = self.go()
            self.assertEqual((code, self.failed(body)), (409, ['approval_attested_for_this_snapshot_and_principal']))
        other = dict(client_key='other', site_id='s', list_id='l', job='j', verified=True)
        for entries in ([ENTRY, other], [dict(ENTRY, note='edited')]):
            code, body = self.go(self.deps(entries=entries))
            self.assertEqual((code, self.failed(body)), (409, ['approval_attested_for_this_snapshot_and_principal']))
        self.untouched()
        self.assertEqual(self.go()[0], 200)                         # unchanged snapshot still runs

    def test_each_missing_dependency_blocks_a_run(self):
        class NotInstalled(SqliteStore):
            def ready(self): return False
        class Broken(SqliteStore):
            def ready(self): raise ConnectionError('database unreachable')
        class NotAnArtifactStore:
            def lookup(self, k): return None
        cases = dict(job_registry_available=dict(entries=None), renderers_built_from_configuration=dict(artifacts=None),
                     source_adapter_configured=dict(fetch=None), durable_store_ready=dict(store=None),
                     quota_configured=dict(quota=None))
        for check, change in cases.items():
            with self.subTest(check):
                code, body = self.go(self.deps(**change))
                self.assertEqual(code, 409); self.assertIn(check, self.failed(body))
        for change in (dict(store=NotInstalled(Path(self.folder.name) / 'n.db')), dict(store=Broken(Path(self.folder.name) / 'b.db')),
                       dict(artifacts=NotAnArtifactStore()), dict(quota=5), dict(quota=Quota(0)), dict(quota=Quota(True)), dict(entries=(ENTRY,))):
            code, body = self.go(self.deps(**change))
            self.assertEqual(code, 409, change)
        self.untouched()

    def test_a_verified_provider_contract_attached_here_blocks_the_run(self):
        from test_last_scan_durable import FAKE_CONTRACT, trust
        self.assertEqual(self.go(self.deps(provider_contract=FAKE_CONTRACT))[0], 200)       # not listed: simply not a transport
        trust(self, FAKE_CONTRACT)
        code, body = self.go(self.deps(provider_contract=FAKE_CONTRACT))
        self.assertEqual((code, self.failed(body)), (409, ['delivery_transport_disabled']))

    def test_approved_run_builds_a_held_draft_in_private_storage_and_sends_nothing(self):
        deps = self.deps()
        code, body = self.go(deps)
        summary = body['summary']
        self.assertEqual((code, body['status'], summary['counts'], summary['notifications_sent'], summary['client_release']),
                         (200, 'ran', {'draft_ready': 1}, 0, 'held'))
        op = summary['results'][0]['operation_id']
        publisher = StoredDraftRouter(BlobArtifacts(self.store), self.config['assignments']).for_scope(assignment())
        self.assertIsInstance(publisher, StoredDraftPublisher)
        checklist = json.loads(publisher.read(op, 'checklist.json'))
        self.assertEqual((checklist['status'], checklist['client_release'], checklist['day_completion']), ('DRAFT', 'held', 'not_established'))
        self.assertTrue(any(f['field'] == 'rejected' and f['code'] == 'missing_value' for f in checklist['findings']))   # unknown kept
        from openpyxl import load_workbook
        import io
        book = load_workbook(io.BytesIO(publisher.read(op, 'draft.xlsx')), data_only=True)
        text = ' | '.join(str(c.value) for sheet in book for row in sheet.iter_rows() for c in row if c.value is not None); book.close()
        self.assertIn('Approved Client Corp', text)                                         # the approved header, nothing else
        self.assertEqual(list(Path(self.folder.name).iterdir()), [self.path])               # no draft files outside the store
        with self.store.transaction('outbox:' + summary['results'][0]['scope']) as box:
            entry = box['entries'][op]
        self.assertEqual((entry['status'], 'attempts' in entry), ('pending', False))         # queued internally, never attempted


class ReplayAndQuota(Hosted):
    def test_qa_repro_one_caller_cannot_start_unlimited_runs(self):
        deps = self.deps(quota=Quota(2))
        codes = [self.go(deps)[0] for _ in range(2)]
        reads = len(self.reads)
        code, body = self.go(deps)
        self.assertEqual((codes, code, body['status'], body['sources_read'], len(self.reads)), ([200, 200], 429, 'quota_exceeded', 0, reads))

    def test_same_nonce_is_refused_even_after_restart_and_with_another_run_id(self):
        body = self.body(mode='run')
        self.assertEqual(self.call(body=body)[0], 200)
        reads = len(self.reads)
        code, out = self.call(body=body)
        self.assertEqual((code, out['status'], out['sources_read']), (409, 'replayed_request', 0))
        self.store = StateStore(self.path)                                                   # restart
        code, out = self.call(body=dict(body, run_id='a-different-run'))
        self.assertEqual((code, out['status'], len(self.reads)), (409, 'replayed_request', reads))
        self.assertEqual(self.call(body=dict(body, nonce='another-nonce-000000001'))[0], 200)   # a new nonce is a new request

    def test_nonces_are_per_principal(self):
        digest = self.approve(self.config, principals=('alice', 'bob'))
        body = self.body(mode='run')
        self.assertEqual((self.call(body=body, headers=ALICE)[0], self.call(body=body, headers=BOB)[0]), (200, 200))
        self.assertEqual(self.call(body=body, headers=BOB)[0], 409)

    def test_concurrent_identical_requests_run_once(self):
        body = self.body(mode='run')
        with ThreadPoolExecutor(8) as pool:
            results = [r[1]['status'] for r in pool.map(lambda _: self.call(body=dict(body)), range(8))]
        self.assertEqual((results.count('ran'), results.count('replayed_request')), (1, 7), results)

    def test_concurrent_distinct_requests_cannot_exceed_the_quota(self):
        deps = self.deps(quota=Quota(3))
        bodies = [self.body(mode='run') for _ in range(8)]
        with ThreadPoolExecutor(8) as pool:
            results = [r[1]['status'] for r in pool.map(lambda b: self.call(deps, b), bodies)]
        self.assertEqual((results.count('ran'), results.count('quota_exceeded')), (3, 5), results)

    def test_quota_is_per_principal_per_snapshot_per_hour_and_survives_restart(self):
        self.approve(self.config, principals=('alice', 'bob'))
        q = Quota(1)
        self.assertEqual(self.go(self.deps(quota=q))[0], 200)
        self.store = StateStore(self.path)                                                   # restart
        self.assertEqual(self.go(self.deps(quota=q))[0], 429)
        self.assertEqual(self.go(self.deps(quota=q), headers=BOB)[0], 200)                   # another principal, own allowance
        self.now = '2026-11-10T22:00:00Z'                                                    # next hour
        self.assertEqual(self.go(self.deps(quota=q))[0], 200)

    def test_refused_requests_reserve_nothing(self):
        self.call(); self.call(body=self.body(mode='run', budget_seconds=float('nan')))
        self.go(self.deps(approvals=None)); self.go(headers=BOB); self.go(self.deps(config=dict(self.config, note='drift')))
        self.go(headers={}); self.go(self.deps(quota=None)); self.call(body=self.body(mode='run', timestamp='2026-11-10T20:00:00Z'))
        self.untouched()

    def test_quota_refusal_does_not_burn_the_nonce(self):
        deps = self.deps(quota=Quota(1)); self.go(deps)
        body = self.body(mode='run')
        self.assertEqual(self.call(deps, body)[0], 429)
        self.now = '2026-11-10T22:00:00Z'
        self.assertEqual(self.call(deps, dict(body, timestamp=self.now))[0], 200)

    def test_same_run_id_with_a_fresh_nonce_is_replayed_from_its_receipt_without_reading(self):
        first = self.go(run_id='firing-1')[1]['summary']; reads = len(self.reads)
        again = self.go(run_id='firing-1')[1]['summary']
        self.assertTrue(again['replayed']); self.assertEqual((len(self.reads), again['counts']), (reads, first['counts']))


class SnapshotIsolation(Hosted):
    def test_same_run_id_does_not_reuse_another_principals_receipt(self):
        self.approve(self.config, principals=('alice', 'bob'))
        first = self.go(run_id='shared')[1]['summary']
        reads = len(self.reads)
        second = self.go(headers=BOB, run_id='shared')[1]['summary']
        self.assertNotIn('replayed', second)
        self.assertGreater(len(self.reads), reads)
        self.assertEqual(first['run_id'], second['run_id'])

    def test_same_run_id_does_not_reuse_another_approved_snapshots_receipt(self):
        self.go(run_id='shared')
        changed = dict(enabled=True, assignments=[assignment(recorded_shifts=['2'])])    # re-pinned and re-attested, then approved
        self.approve(changed)
        code, report = self.go(self.deps(config=changed), run_id='shared')
        self.assertEqual(code, 200)
        self.assertNotIn('replayed', report['summary'])
        self.assertEqual(report['summary']['results'][0]['shift'], '2')
        self.assertEqual(self.reads[-1], '2')

    def test_config_mutation_during_approval_cannot_change_execution(self):
        original = self.config
        approvals = self.approvals
        class MutatingApproval(ApprovalAuthority):
            def attestation(self, digest):
                original['assignments'][0]['recorded_shifts'][0] = 'UNAPPROVED-SHIFT'
                return approvals.attestation(digest)
        code, report = self.go(self.deps(approvals=MutatingApproval()))
        self.assertEqual(code, 200)
        self.assertTrue(self.reads)
        self.assertEqual(set(self.reads), {'1'})
        self.assertEqual(report['planned'][0]['shift'], '1')
        self.assertEqual(report['summary']['results'][0]['shift'], '1')

    def test_registry_mutation_during_approval_cannot_change_execution(self):
        entries = [deepcopy(ENTRY)]
        approvals = self.approvals
        class MutatingApproval(ApprovalAuthority):
            def attestation(self, digest):
                entries[0]['verified'] = False
                return approvals.attestation(digest)
        code, report = self.go(self.deps(entries=entries, approvals=MutatingApproval()))
        self.assertEqual(code, 200)
        self.assertEqual(report['summary']['planned'], 1)
        self.assertTrue(self.reads)
        self.assertEqual(set(self.reads), {'1'})

    def test_body_mutation_during_authentication_cannot_escalate_dry_run(self):
        body = self.body(mode='dry_run')
        class MutatingAuthorizer(FakeAuthorizer):
            def verify(self, method, headers, digest):
                body['mode'] = 'run'
                return super().verify(method, headers, digest)
        code, report = self.call(self.deps(authorizer=MutatingAuthorizer()), body)
        self.assertEqual((code, report['status']), (200, 'dry_run'))
        self.untouched()

    def test_config_and_dependency_replacement_during_authentication_use_original_snapshot(self):
        deps = self.deps()
        class MutatingAuthorizer(FakeAuthorizer):
            def verify(self, method, headers, digest):
                deps.config['assignments'][0]['recorded_shifts'][0] = 'UNAPPROVED-SHIFT'
                deps.config = {'enabled': False}
                deps.entries.clear()
                return super().verify(method, headers, digest)
        deps.authorizer = MutatingAuthorizer()
        code, report = self.go(deps)
        self.assertEqual(code, 200)
        self.assertTrue(self.reads)
        self.assertEqual(set(self.reads), {'1'})
        self.assertEqual(report['summary']['planned'], 1)


class HostedCursorIsolation(Hosted):
    def test_one_principals_runs_do_not_move_anothers_starting_point(self):
        config = dict(enabled=True, assignments=[assignment(recorded_shifts=['1', '2', '3'])])    # every recorded shift maps to a template shift
        self.approve(config, principals=('alice', 'bob'))
        deps = lambda: self.deps(config=config, quota=Quota(50))
        shift = lambda headers: self.go(deps(), headers=headers, max_scopes=1)[1]['summary']['results'][0]['shift']
        alone = [shift(BOB) for _ in range(3)]
        self.store = StateStore(Path(self.folder.name) / 'second.db')       # a clean store, same configuration
        mixed = []
        for _ in range(3):
            mixed.append(shift(BOB))
            for _ in range(3):
                shift(ALICE)
        self.assertEqual((sorted(alone), mixed), (['1', '2', '3'], alone))


class NotConnected(unittest.TestCase):
    """The prepared code must not be reachable from the deployed app."""
    def test_no_route_cron_or_import_reaches_the_runner(self):
        names = re.compile(r'last_scan_(hosted|runner|transport|service|store|local)')
        for path in list((ROOT / 'api').rglob('*.py')) + [ROOT / 'vercel.json'] + list((ROOT / 'web').rglob('*.js')):
            if path.is_file():
                self.assertIsNone(names.search(path.read_text(errors='ignore')), str(path))
        allowed = {'last_scan_hosted.py', 'last_scan_runner.py', 'last_scan_transport.py', 'last_scan_service.py',
                   'last_scan_dispatch.py',                     # hosted dispatch boundary: prepared, imported by nothing
                   'last_scan_store.py', 'last_scan_local.py', 'last_scan_drafts.py',
                   'last_scan_readiness.py'}                    # offline validator: reuses the runner's exact registry rule, runs nothing
        for path in (ROOT / 'hosting').glob('*.py'):
            if path.name not in allowed:
                self.assertIsNone(names.search(path.read_text(errors='ignore')), str(path))
        if (ROOT / 'vercel.json').is_file():
            self.assertNotIn('crons', json.loads((ROOT / 'vercel.json').read_text()))

    def test_hosted_module_has_no_network_client_secret_reader_identity_provider_or_sender(self):
        text = ''.join((ROOT / 'hosting' / n).read_text() for n in ('last_scan_hosted.py', 'last_scan_transport.py', 'last_scan_dispatch.py'))
        for forbidden in ('urllib', 'requests', 'http.client', 'smtplib', 'os.environ', 'getenv', 'psycopg', 'socket', 'subprocess',
                          '.install(', 'Bearer', 'compare_digest', 'expected_token', 'jwt'):
            self.assertNotIn(forbidden, text, forbidden)
        self.assertNotIn('dispatch', (ROOT / 'hosting' / 'last_scan_hosted.py').read_text())
        self.assertNotIn('dispatch', (ROOT / 'hosting' / 'last_scan_runner.py').read_text())
        concrete = [n for n in dir(hosted) if isinstance(getattr(hosted, n), type) and issubclass(getattr(hosted, n), (Authorizer, ApprovalAuthority))
                    and getattr(hosted, n) not in (Authorizer, ApprovalAuthority)]
        self.assertEqual(concrete, [])                              # interfaces only: no built-in identity or approval system
        for base in (Authorizer(), ApprovalAuthority()):
            with self.assertRaises(NotImplementedError):
                (base.verify('POST', {}, 'x') if isinstance(base, Authorizer) else base.attestation('x'))

    def test_every_dependency_defaults_to_not_configured(self):
        d = Deps()
        self.assertEqual([getattr(d, n) for n in ('authorizer', 'approvals', 'quota', 'config', 'entries', 'store', 'artifacts', 'fetch', 'provider_contract')], [None] * 9)
        self.assertEqual(hosted.handle('POST', {}, {}, d), (503, dict(status='not_configured')))

    def test_install_is_never_called_outside_tests_and_the_local_store(self):
        for path in (ROOT / 'hosting').glob('*.py'):
            text = path.read_text(errors='ignore')
            if path.name == 'last_scan_store.py':
                self.assertEqual(text.count('self.install()'), 1)             # the local SQLite constructor only
            elif path.name.startswith('last_scan_'):
                self.assertNotIn('.install(', text, path.name)


if __name__ == '__main__':
    unittest.main()

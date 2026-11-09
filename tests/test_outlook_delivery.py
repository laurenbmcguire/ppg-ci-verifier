"""Offline synthetic ledger tests; never contact Outlook or load real reports."""
from contextlib import closing
from dataclasses import replace
import multiprocessing
import copy
import os
import sqlite3
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from hosting.outlook_delivery import (Acceptance, Auth, DeliveryService, DeliveryError,
                                     MAX_ATTACHMENT, canonical, assert_mutation_allowed)


def _dispatch_process(path, intent_id, auth, data, barrier, results, calls):
    """Spawned interpreter: distinct connection and service, fake provider only."""
    def build(current_auth, report_id, draft, db):
        if not db.in_transaction:
            raise AssertionError('Freshness outside transaction')
        return dict(data, **draft)
    def send(snapshot, attachment):
        calls.put(os.getpid())
        return Acceptance('synthetic-process-202')
    service = DeliveryService(path, authenticate=lambda _: auth, build_snapshot=build,
                              transport=send, clock=lambda: 1000)
    barrier.wait(timeout=15)
    try:
        results.put(service.dispatch('server-session', intent_id))
    except DeliveryError:
        results.put('blocked')


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, 'synthetic.sqlite')
        self.now = 1000
        self.calls = []
        self.auth = Auth('synthetic-human', 'synthetic-workspace', 'synthetic-client',
                         'authenticated_human_session', True)
        self.data = dict(report_hash='hash', report_version='v1', check_hash='check', check_run='run',
                         checker_version='checker', approval_identity='reviewer', approval_time='time',
                         approved_state_hash='approved-state', source_revision='revision', assignment='assignment',
                         sender_account_id='mailbox-id', sender='sender@example.test', sender_verified=True,
                         approval_current=True, attachment_name='synthetic.txt', attachment_mime='text/plain',
                         attachment_bytes=b'synthetic only', to=['client@example.test'], cc=['configured@example.test'],
                         bcc=[], subject='Synthetic report', body='Full synthetic message body')
        self.service = self.new()

    def new(self, cls=DeliveryService, transport='fake'):
        return cls(self.path, authenticate=lambda credential: self.auth if credential == 'server-session' else None,
                   build_snapshot=self.build, clock=lambda: self.now,
                   transport=self.send if transport == 'fake' else transport)

    def build(self, auth, report_id, draft, db):
        self.assertTrue(db.in_transaction)
        data = copy.deepcopy(self.data)
        data.update(draft)
        return data

    def send(self, snapshot, attachment):
        self.calls.append((snapshot, attachment))
        return Acceptance('synthetic-202')

    def preview(self):
        return self.service.preview('server-session', 'synthetic-report', {})

    def confirmed(self):
        p = self.preview()
        self.service.confirm('server-session', p['id'], p['digest'])
        return p

    def test_happy_repeat_confirmation_and_no_retry(self):
        p = self.confirmed()
        self.service.confirm('server-session', p['id'], p['digest'])
        self.assertEqual(self.service.dispatch('server-session', p['id']), 'accepted')
        with self.assertRaises(DeliveryError):
            self.new().dispatch('server-session', p['id'])
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0][0]['cc'], ['configured@example.test'])

    def test_concurrent_workers_and_double_click(self):
        p = self.confirmed()
        def run(_):
            try:
                return self.new().dispatch('server-session', p['id'])
            except DeliveryError:
                return 'blocked'
        with ThreadPoolExecutor(max_workers=4) as pool:
            result = list(pool.map(run, range(4)))
        self.assertEqual(result.count('accepted'), 1)
        self.assertEqual(len(self.calls), 1)

    def test_separate_process_dispatch_race(self):
        p = self.confirmed()
        context = multiprocessing.get_context('spawn')
        barrier, results, calls = context.Barrier(4), context.Queue(), context.Queue()
        workers = [context.Process(target=_dispatch_process,
                   args=(self.path, p['id'], self.auth, self.data, barrier, results, calls))
                   for _ in range(3)]
        try:
            for worker in workers:
                worker.start()
            barrier.wait(timeout=15)
            outcomes = [results.get(timeout=15) for _ in workers]
            for worker in workers:
                worker.join(timeout=15)
                self.assertEqual(worker.exitcode, 0)
            self.assertEqual(outcomes.count('accepted'), 1)
            self.assertEqual(outcomes.count('blocked'), 2)
            calls.get(timeout=5)
            from queue import Empty
            with self.assertRaises(Empty):
                calls.get(timeout=0.1)
            self.assertEqual(self.new().status('server-session', p['id']), 'accepted')
        finally:
            for worker in workers:
                if worker.is_alive():
                    worker.terminate()
                    worker.join(timeout=5)
                worker.close()
            results.close()
            calls.close()
            results.join_thread()
            calls.join_thread()

    def test_real_capability_revocation_and_context_changes_at_each_boundary(self):
        original = self.auth
        changes = dict(can_send=False, actor='other-human', workspace='other-workspace', client='other-client')
        for boundary in ('confirm', 'dispatch'):
            for field, value in changes.items():
                with self.subTest(boundary=boundary, field=field):
                    self.auth = original
                    p = self.preview()
                    if boundary == 'dispatch':
                        self.service.confirm('server-session', p['id'], p['digest'])
                    self.auth = replace(original, **{field: value})
                    with self.assertRaises(DeliveryError):
                        if boundary == 'confirm':
                            self.service.confirm('server-session', p['id'], p['digest'])
                        else:
                            self.service.dispatch('server-session', p['id'])
                    self.auth = original
                    self.assertEqual(self.service.status('server-session', p['id']),
                                     'awaiting_confirmation' if boundary == 'confirm' else 'confirmed')
                    self.service.cancel('server-session', p['id'])
        self.assertEqual(self.calls, [])

    def test_report_writer_and_claim_both_transaction_orderings(self):
        for winner in ('writer', 'claim'):
            with self.subTest(winner=winner):
                self.path = os.path.join(self.tmp.name, winner + '.sqlite')
                self.service = self.new()
                with closing(sqlite3.connect(self.path)) as db:
                    db.execute('CREATE TABLE synthetic_report (revision TEXT NOT NULL)')
                    db.execute('INSERT INTO synthetic_report VALUES (?)', ('revision',))
                    db.commit()
                gate, release = threading.Event(), threading.Event()
                writer_started, provider_entered = threading.Event(), threading.Event()
                provider_release = threading.Event()
                armed = [False]
                def builder(auth, report_id, draft, db):
                    data = self.build(auth, report_id, draft, db)
                    data['source_revision'] = db.execute('SELECT revision FROM synthetic_report').fetchone()[0]
                    if armed[0] and winner == 'claim':
                        gate.set()  # Freshness has the writer lock; claim not committed yet.
                        if not release.wait(5):
                            raise AssertionError('Claim release timed out')
                    return data
                self.service.build_snapshot = builder
                def transport(snapshot, attachment):
                    self.calls.append(snapshot)
                    provider_entered.set()
                    if not provider_release.wait(5):
                        raise AssertionError('Provider release timed out')
                    return Acceptance('synthetic-202')
                self.service.transport = transport
                p = self.confirmed()
                armed[0] = True
                def writer():
                    with closing(sqlite3.connect(self.path, timeout=10)) as db:
                        writer_started.set()
                        db.execute('BEGIN IMMEDIATE')
                        try:
                            assert_mutation_allowed(db, self.auth.workspace, self.auth.client, 'synthetic-report')
                            db.execute("UPDATE synthetic_report SET revision='changed'")
                            if winner == 'writer':
                                gate.set()  # Uncommitted mutation holds writer lock.
                                if not release.wait(5):
                                    raise AssertionError('Writer release timed out')
                            db.commit()
                            return 'changed'
                        except DeliveryError:
                            db.rollback()
                            return 'blocked'
                with ThreadPoolExecutor(max_workers=2) as pool:
                    try:
                        if winner == 'writer':
                            write_future = pool.submit(writer)
                            self.assertTrue(gate.wait(5))
                            dispatch_future = pool.submit(self.service.dispatch, 'server-session', p['id'])
                            release.set()
                            self.assertEqual(write_future.result(timeout=5), 'changed')
                            with self.assertRaises(DeliveryError):
                                dispatch_future.result(timeout=5)
                            self.assertEqual(self.calls, [])
                            self.assertEqual(self.service.status('server-session', p['id']), 'rejected_before_send')
                        else:
                            dispatch_future = pool.submit(self.service.dispatch, 'server-session', p['id'])
                            self.assertTrue(gate.wait(5))
                            write_future = pool.submit(writer)
                            self.assertTrue(writer_started.wait(5))
                            release.set()
                            self.assertTrue(provider_entered.wait(5))
                            self.assertEqual(write_future.result(timeout=5), 'blocked')
                            provider_release.set()
                            self.assertEqual(dispatch_future.result(timeout=5), 'accepted')
                            self.assertEqual(len(self.calls), 1)
                    finally:
                        release.set()
                        provider_release.set()
                with closing(sqlite3.connect(self.path)) as db:
                    self.assertEqual(db.execute('SELECT revision FROM synthetic_report').fetchone()[0],
                                     'changed' if winner == 'writer' else 'revision')

    def test_failed_claim_commit_calls_zero(self):
        class Fail(DeliveryService):
            def _commit(self, db, phase):
                if phase == 'claim':
                    raise sqlite3.OperationalError('synthetic commit failure')
                super()._commit(db, phase)
        p = self.confirmed()
        with self.assertRaises(sqlite3.OperationalError):
            self.new(Fail).dispatch('server-session', p['id'])
        self.assertEqual(self.calls, [])

    def test_uncertain_claim_commit_calls_zero_and_stays_blocked(self):
        class Fail(DeliveryService):
            def _commit(self, db, phase):
                super()._commit(db, phase)
                if phase == 'claim':
                    raise sqlite3.OperationalError('ack lost after commit')
        p = self.confirmed()
        with self.assertRaises(sqlite3.OperationalError):
            self.new(Fail).dispatch('server-session', p['id'])
        self.assertEqual(self.calls, [])
        self.assertEqual(self.new().status('server-session', p['id']), 'dispatching')

    def test_provider_acceptance_result_commit_failure_persists_beyond_expiry(self):
        class Fail(DeliveryService):
            def _commit(self, db, phase):
                if phase == 'result':
                    raise sqlite3.OperationalError('synthetic result persistence failure')
                super()._commit(db, phase)
        p = self.confirmed()
        with self.assertRaises(sqlite3.OperationalError):
            self.new(Fail).dispatch('server-session', p['id'])
        self.now += 1000
        with self.assertRaises(DeliveryError):
            self.new().dispatch('server-session', p['id'])
        with self.assertRaises(DeliveryError):
            self.preview()
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.new().status('server-session', p['id']), 'dispatching')

    def test_timeout_and_crash_never_retry(self):
        for exc, expected in [(TimeoutError(), 'unknown'), (SystemExit(), 'dispatching')]:
            with self.subTest(expected=expected):
                self.path = os.path.join(self.tmp.name, expected + '.sqlite')
                def fail(snapshot, attachment):
                    self.calls.append(1)
                    raise exc
                self.service = self.new(transport=fail)
                p = self.confirmed()
                try:
                    self.service.dispatch('server-session', p['id'])
                except SystemExit:
                    pass
                self.assertEqual(self.new().status('server-session', p['id']), expected)
                self.now += 1000
                with self.assertRaises(DeliveryError):
                    self.new().dispatch('server-session', p['id'])
                with self.assertRaises(DeliveryError):
                    self.preview()

    def test_every_snapshot_change_invalidates_confirmation(self):
        for key in self.data:
            with self.subTest(key=key):
                p = self.confirmed()
                old = self.data[key]
                if type(old) is bool:
                    self.data[key] = False
                elif type(old) is bytes:
                    self.data[key] = b'changed'
                elif type(old) is list:
                    self.data[key] = ['changed@example.test']
                elif key == 'sender':
                    self.data[key] = 'changed@example.test'
                else:
                    self.data[key] += '-changed'
                with self.assertRaises(DeliveryError):
                    self.service.dispatch('server-session', p['id'])
                self.data[key] = old
        self.assertEqual(self.calls, [])

    def test_auth_missing_forged_owner_apikey_and_revocation(self):
        for credential in [None, {'actor': 'owner', 'can_send': True}, 'OWNER', 'api-key']:
            with self.assertRaises(DeliveryError):
                self.service.preview(credential, 'synthetic-report', {})
        p = self.confirmed()
        for method in ['OWNER', 'api_key']:
            self.auth = Auth('synthetic-human', 'synthetic-workspace', 'synthetic-client', method, True)
            with self.assertRaises(DeliveryError):
                self.service.dispatch('server-session', p['id'])
        self.assertEqual(self.calls, [])

    def test_preview_edit_and_digest_forgery(self):
        p = self.preview()
        p['snapshot']['body'] = 'browser tampering'
        with self.assertRaises(DeliveryError):
            self.service.confirm('server-session', p['id'], 'forged-digest')
        self.service.confirm('server-session', p['id'], p['digest'])
        self.service.dispatch('server-session', p['id'])
        self.assertEqual(self.calls[0][0]['body'], self.data['body'])

    def test_expiry_cancel_and_duplicate_preview(self):
        p = self.confirmed()
        with self.assertRaises(DeliveryError):
            self.service.preview('server-session', 'synthetic-report', {'body': 'different'})
        self.now += 301
        with self.assertRaises(DeliveryError):
            self.service.dispatch('server-session', p['id'])
        p = self.preview()
        self.service.cancel('server-session', p['id'])
        with self.assertRaises(DeliveryError):
            self.service.dispatch('server-session', p['id'])
        self.preview()
        self.assertEqual(self.calls, [])

    def test_size_serialization_and_no_transport_default(self):
        for value in [float('nan'), {'bad': object()}, {'large': 'x' * 100001}]:
            with self.assertRaises(DeliveryError):
                canonical(value)
        self.data['attachment_bytes'] = b'x' * (MAX_ATTACHMENT + 1)
        with self.assertRaises(DeliveryError):
            self.preview()
        self.data['attachment_bytes'] = b'ok'
        with self.assertRaises(DeliveryError):
            self.service.preview('server-session', 'synthetic-report', {'attachment_bytes': b'forged'})
        p = self.confirmed()
        with self.assertRaises(DeliveryError):
            self.new(transport=None).dispatch('server-session', p['id'])
        self.assertEqual(self.calls, [])

    def test_postgres_schema_state_and_unique_guard_parity(self):
        from pathlib import Path
        from hosting.outlook_delivery import STATES
        sql = (Path(__file__).resolve().parents[1] / 'supabase' / 'outlook_delivery.sql').read_text()
        for state in STATES:
            self.assertIn("'" + state + "'", sql)
        self.assertIn("WHERE state IN ('awaiting_confirmation','confirmed','dispatching','unknown','accepted')", sql)
        self.assertIn('ENABLE ROW LEVEL SECURITY', sql)

    def test_writer_lock_prevents_mutation_after_claim(self):
        entered, release = threading.Event(), threading.Event()
        def transport(snapshot, attachment):
            entered.set()
            self.assertTrue(release.wait(5))
            return Acceptance('synthetic-202')
        self.service.transport = transport
        p = self.confirmed()
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(self.service.dispatch, 'server-session', p['id'])
            try:
                self.assertTrue(entered.wait(5))
                with closing(sqlite3.connect(self.path)) as db:
                    db.execute('BEGIN IMMEDIATE')
                    with self.assertRaises(DeliveryError):
                        assert_mutation_allowed(db, self.auth.workspace, self.auth.client, 'synthetic-report')
                    db.rollback()
            finally:
                release.set()
            self.assertEqual(future.result(), 'accepted')

    def test_mutation_guard_and_cancel_after_dispatch(self):
        self.service.transport = lambda *_: None
        p = self.confirmed()
        self.service.dispatch('server-session', p['id'])
        with self.assertRaises(DeliveryError):
            self.service.cancel('server-session', p['id'])
        with closing(sqlite3.connect(self.path)) as db:
            db.execute('BEGIN IMMEDIATE')
            with self.assertRaises(DeliveryError):
                assert_mutation_allowed(db, self.auth.workspace, self.auth.client, 'synthetic-report')


if __name__ == '__main__':
    unittest.main()

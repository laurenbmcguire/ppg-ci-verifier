"""Durable last-scan worker on the local SQLite store. Synthetic sources only."""
import base64
import sqlite3
import tempfile
import unittest
from pathlib import Path
from hosting import source_first as sf
from hosting.last_scan_local import StateStore
from hosting.last_scan_service import ScheduledWorker
import last_scan_scenarios as sc
from last_scan_scenarios import Env, Publisher, Crash, DUE
from hosting import last_scan_transport as transport
from hosting.last_scan_transport import ProviderContract

# A FAKE provider contract and a FAKE registry that lists it. Tests only: the real registry is empty.
FAKE_CONTRACT = ProviderContract(provider='synthetic-test-transport', version='1', at_most_once_per_idempotency_key=True,
                                 terminal_status_per_attempt=True, honours_attempt_closed=True, sender_timeout_seconds=600,
                                 retry_after_terminal_non_delivery=True, verified_by='test fixture', verified_at='2026-11-10',
                                 evidence='tests/test_last_scan_durable.py')
FAKE_REGISTRY = {(FAKE_CONTRACT.provider, FAKE_CONTRACT.version): FAKE_CONTRACT.digest()}
VERIFIED = dict(provider_contract=FAKE_CONTRACT, contract_registry=FAKE_REGISTRY)


class Base(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(); self.addCleanup(self.folder.cleanup)
        self.path = Path(self.folder.name) / 'state.db'
        self.store = self.make_store()

    def make_store(self):
        return StateStore(self.path)        # overridden to run the same tests on real PostgreSQL

    def restart(self):
        return self.make_store()            # a new process: nothing in memory survives


class SharedScenarios(Base):
    def test_shared_scenarios(self):
        for i, scenario in enumerate(sc.ALL):
            with self.subTest(scenario.__name__):
                scenario(StateStore(Path(self.folder.name) / ('s%d.db' % i)))


class Idempotency(Base):
    def test_exact_retries_render_and_queue_once(self):
        env = Env(self.store); env.now = DUE; w = env.worker()
        outs = [w.tick(env.scope, {}) for _ in range(4)]
        self.assertEqual({o['status'] for o in outs}, {'draft_ready'})
        self.assertEqual(len({o['operation_id'] for o in outs}), 1)
        self.assertEqual((len(env.renders), len(env.entries()), env.fetches), (1, 1, 5))   # 2 captures, then 1 per retry

    def test_requires_atomic_store(self):
        class Old:
            def transaction(self, key): pass
        with self.assertRaises(TypeError):
            ScheduledWorker(Old().transaction, None, None)

    def test_operation_follows_mapping_header_template(self):
        env = Env(self.store); env.now = DUE; pub = Publisher(env, version='template-v1')
        first = env.worker(pub).tick(env.scope, {})
        pub.version = 'template-v2'             # same source rows, different mapping/header/template
        second = env.worker(pub).tick(env.scope, {})
        self.assertEqual((first['status'], second['status']), ('draft_ready', 'draft_ready'))
        self.assertNotEqual(first['operation_id'], second['operation_id'])
        entries = env.entries()
        self.assertEqual(entries[first['operation_id']]['status'], 'superseded')
        self.assertEqual(entries[second['operation_id']]['status'], 'pending')

    def reverted(self):
        """Ready at 21:35 -> a new row seen at 21:36 -> the row deleted again by 21:37 (content is the old version)."""
        env = Env(self.store); env.now = DUE; w = env.worker()
        a = w.tick(env.scope, {})
        env.rows.append(dict(id='2', scan_at='2026-11-10T21:30:00Z')); env.now = '2026-11-10T21:36:00Z'
        self.assertEqual(w.tick(env.scope, {})['status'], 'waiting')
        self.assertEqual(env.entries()[a['operation_id']]['status'], 'superseded')
        env.rows.pop(); env.now = '2026-11-10T21:37:00Z'
        return env, a['operation_id']

    def test_reverted_content_waits_the_full_renewed_hour(self):
        env, op = self.reverted(); w = env.worker()
        back = w.tick(env.scope, {})
        self.assertEqual((back['status'], back['operation_id']), ('waiting', op))     # same content, but NOT ready early
        self.assertTrue(back['due_at'].startswith('2026-11-10T22:37'))
        self.assertEqual(env.entries()[op]['status'], 'superseded')                  # the old outbox entry is not revived
        self.assertEqual(w.dispatch_test(env.scope, op, lambda **kw: self.fail('sent early')), dict(status='not_pending'))
        env.now = '2026-11-10T22:36:59Z'
        self.assertEqual(w.tick(env.scope, {})['status'], 'waiting')
        self.assertEqual(env.entries()[op]['status'], 'superseded')
        env.now = '2026-11-10T22:37:00Z'
        self.assertEqual(w.tick(env.scope, {})['status'], 'draft_ready')              # the cached draft is reused...
        self.assertEqual((len(env.renders), env.entries()[op]['status']), (1, 'pending'))   # ...without a second render

    def test_reverted_content_after_restart_and_under_concurrency(self):
        from concurrent.futures import ThreadPoolExecutor
        env, op = self.reverted()
        env.store = self.restart()                                                   # a new process sees only the database
        self.assertEqual(env.worker().tick(env.scope, {})['status'], 'waiting')
        def burst():
            with ThreadPoolExecutor(6) as pool:
                return [r['status'] for r in pool.map(lambda _: env.worker().tick(env.scope, {}), range(6))]
        env.now = '2026-11-10T21:38:00Z'
        early = burst()
        self.assertTrue(set(early) <= {'waiting', 'busy'} and 'waiting' in early, early)
        self.assertEqual((env.entries()[op]['status'], len(env.renders)), ('superseded', 1))
        env.store = self.restart(); env.now = '2026-11-10T22:37:00Z'
        late = burst()
        self.assertTrue(set(late) <= {'draft_ready', 'busy'} and 'draft_ready' in late, late)
        self.assertEqual(({k: v['status'] for k, v in env.entries().items()}, len(env.renders)), ({op: 'pending'}, 1))

    def test_uncertain_claim_for_reverted_content_also_waits(self):
        env = Env(self.store); env.now = DUE
        def fails(snapshot, op): raise OSError('disk')
        self.assertEqual(env.worker(fails).tick(env.scope, {})['status'], 'uncertain')
        env.rows.append(dict(id='2', scan_at='2026-11-10T21:30:00Z')); env.now = '2026-11-10T21:36:00Z'
        self.assertEqual(env.worker().tick(env.scope, {})['status'], 'waiting')
        env.rows.pop(); env.now = '2026-11-10T21:37:00Z'
        self.assertEqual(env.worker().tick(env.scope, {})['status'], 'waiting')
        env.now = '2026-11-10T22:37:00Z'
        self.assertEqual(env.worker().tick(env.scope, {})['status'], 'uncertain')    # due again: reconcile, never re-render blindly
        self.assertEqual(env.renders, [])


class CrashRestart(Base):
    def test_crash_after_claim_is_reconcile_only(self):
        env = Env(self.store); env.now = DUE
        def dies(snapshot, op): raise Crash()
        with self.assertRaises(Crash):
            env.worker(dies).tick(env.scope, {})
        op = env.state()['operation_id']
        self.assertEqual(env.state()['receipts'][op]['state'], 'uncertain')
        env.store = self.restart()
        again = env.worker().tick(env.scope, {})            # a renderer that cannot say what it published
        self.assertEqual((again['status'], again.get('reconcile')), ('uncertain', 'render_outcome_unknown'))
        self.assertEqual((env.renders, env.entries()), ([], {}))
        cautious = env.worker(Publisher(env, authoritative=False)).tick(env.scope, {})
        self.assertEqual(cautious['status'], 'uncertain'); self.assertEqual(env.renders, [])
        sure = env.worker(Publisher(env, authoritative=True)).tick(env.scope, {})
        self.assertEqual(sure['status'], 'draft_ready'); self.assertEqual(env.renders, [op])
        self.assertEqual(list(env.entries()), [op])

    def test_crash_after_render_reuses_published_draft(self):
        env = Env(self.store); env.now = DUE; pub = Publisher(env)
        def dies(): raise Crash()
        env.before_fetch[2] = dies                           # rendered, then died before the outbox step
        with self.assertRaises(Crash):
            env.worker(pub).tick(env.scope, {})
        self.assertEqual((len(env.renders), env.entries()), (1, {}))
        env.store = self.restart()
        out = env.worker(pub).tick(env.scope, {})
        self.assertEqual(out['status'], 'draft_ready')
        self.assertEqual((len(env.renders), list(env.entries())), (1, [out['operation_id']]))

    def test_render_error_stays_uncertain_and_records_health(self):
        env = Env(self.store); env.now = DUE
        def fails(snapshot, op): raise OSError('disk')
        out = env.worker(fails).tick(env.scope, {})
        self.assertEqual((out['status'], out['error_type']), ('uncertain', 'OSError'))
        self.assertEqual(env.record('health:' + env.key)['error_type'], 'OSError')
        self.assertEqual(env.worker().tick(env.scope, {})['status'], 'uncertain')   # not re-rendered blindly
        self.assertEqual(env.renders, [])

    def test_dead_worker_lease_blocks_until_expiry_then_fence_advances(self):
        env = Env(self.store); env.now = DUE
        with self.store.transaction('lease:' + env.key) as lease:   # a worker that died holding the scope
            lease.update(token='dead', fence=7, owner='dead', expires='2026-11-10T21:40:00Z')
        self.assertEqual(env.worker().tick(env.scope, {}), dict(status='busy'))
        self.assertEqual(env.fetches, 0)
        env.now = '2026-11-10T21:40:01Z'
        self.assertEqual(env.worker().tick(env.scope, {})['status'], 'draft_ready')
        self.assertEqual(env.record('lease:' + env.key)['fence'], 8)


class Outbox(Base):
    def ready(self):
        env = Env(self.store); env.now = DUE; w = env.worker()
        return env, w, w.tick(env.scope, {})['operation_id']

    def test_sender_failure_is_persisted_and_not_retried(self):
        env, w, op = self.ready(); calls = []
        def sender(**kw): calls.append(kw); raise TimeoutError('no answer')
        self.assertEqual(w.dispatch_test(env.scope, op, sender), dict(status='uncertain', delivery_status='uncertain'))
        entry = env.entries()[op]
        self.assertEqual((entry['status'], entry['delivery_status'], entry['delivery_error_type']), ('uncertain', 'uncertain', 'TimeoutError'))
        self.assertEqual(w.dispatch_test(env.scope, op, sender), dict(status='not_pending'))
        self.assertEqual(len(calls), 1)

    def test_superseded_sender_failure_persists_uncertain_delivery(self):
        env, w, op = self.ready()
        def sender(**kw):
            w._supersede(env.key, 'newer-operation')
            raise ConnectionError('dropped')
        self.assertEqual(w.dispatch_test(env.scope, op, sender), dict(status='superseded', delivery_status='uncertain'))
        entry = env.entries()[op]
        self.assertEqual((entry['status'], entry['delivery_status'], entry['superseded_by'], entry['delivery_error_type']),
                         ('superseded', 'uncertain', 'newer-operation', 'ConnectionError'))

    def test_wrong_acknowledgment_is_uncertain(self):
        env, w, op = self.ready()
        out = w.dispatch_test(env.scope, op, lambda **kw: dict(operation_id='something-else'))
        self.assertEqual(out['delivery_status'], 'uncertain'); self.assertEqual(env.entries()[op]['status'], 'uncertain')

    def test_uncertain_entry_is_superseded_by_new_content_and_never_requeued(self):
        env, w, op = self.ready()
        def sender(**kw): raise TimeoutError()
        w.dispatch_test(env.scope, op, sender)
        env.rows.append(dict(id='2', scan_at='2026-11-10T21:30:00Z')); env.now = '2026-11-10T21:36:00Z'
        self.assertEqual(w.tick(env.scope, {})['status'], 'waiting')
        entry = env.entries()[op]
        self.assertEqual((entry['status'], entry['delivery_status']), ('superseded', 'uncertain'))
        env.rows.pop(); env.now = '2026-11-10T21:37:00Z'
        self.assertEqual(w.tick(env.scope, {})['status'], 'waiting')
        env.now = '2026-11-10T22:37:00Z'
        self.assertEqual(w.tick(env.scope, {})['status'], 'draft_ready')
        self.assertEqual(env.entries()[op]['status'], 'superseded')     # delivery outcome unknown: reconcile, do not resend


class ReadinessGate(Base):
    """An outbox entry is deliverable only while the scope's LATEST observed readiness is due."""
    def extended(self):
        env, w, op = sc.ready_entry(self.store)
        env.now = '2026-11-10T21:36:00Z'; env.extra = dict(not_before=sc.EXTENDED)
        return env, w, op

    def test_qa_repro_same_operation_schedule_extension(self):
        env, w, op = self.extended()
        out = w.tick(env.scope, {})
        self.assertEqual((out['status'], out['operation_id'], out['due_at'][:16]), ('waiting', op, '2026-11-10T23:00'))
        self.assertEqual(env.entries()[op]['status'], 'held')
        self.assertEqual(w.dispatch_test(env.scope, op, sc.refuse), dict(status='not_pending'))

    def test_hold_survives_restart_and_is_released_only_at_the_due_time(self):
        env, w, op = self.extended(); w.tick(env.scope, {})
        env.store = self.restart(); fresh = env.worker()
        self.assertEqual(fresh.dispatch_test(env.scope, op, sc.refuse), dict(status='not_pending'))
        env.now = '2026-11-10T22:59:59Z'
        self.assertEqual(fresh.tick(env.scope, {})['status'], 'waiting')
        self.assertEqual(env.entries()[op]['status'], 'held')
        env.store = self.restart(); env.now = sc.EXTENDED
        self.assertEqual(env.worker().tick(env.scope, {})['status'], 'draft_ready')
        entry = env.entries()[op]
        self.assertEqual((entry['status'], entry['released_at'], len(env.renders)), ('pending', sc.EXTENDED, 1))
        self.assertNotIn('held_at', entry)

    def midflight(self, env, w, fail=None):
        calls = []
        def sender(**kw):
            calls.append(kw)
            env.now = '2026-11-10T21:36:00Z'; env.extra = dict(not_before=sc.EXTENDED)   # the wait is extended mid-flight
            assert w.tick(env.scope, {})['status'] == 'waiting'
            if fail: raise fail
            return dict(operation_id=kw['idempotency_key'], provider_receipt='synthetic')
        return calls, sender

    def test_extension_during_an_in_flight_delivery_keeps_the_acknowledgment(self):
        env, w, op = sc.ready_entry(self.store); calls, sender = self.midflight(env, w)
        self.assertEqual(w.dispatch_test(env.scope, op, sender), dict(status='sent', readiness_revoked=True))
        entry = env.entries()[op]
        self.assertEqual((entry['status'], entry['delivery_status'], entry['ack']['provider_receipt'], entry['readiness_revoked_at']),
                         ('sent', 'sent', 'synthetic', '2026-11-10T21:36:00Z'))
        env.now = sc.EXTENDED
        self.assertEqual(w.tick(env.scope, {})['status'], 'draft_ready')
        self.assertEqual(env.entries()[op]['status'], 'sent')
        self.assertEqual(w.dispatch_test(env.scope, op, sender), dict(status='not_pending'))
        self.assertEqual(len(calls), 1)

    def test_extension_during_a_failed_delivery_stays_uncertain_and_is_never_resent(self):
        env, w, op = sc.ready_entry(self.store); calls, sender = self.midflight(env, w, TimeoutError('no answer'))
        self.assertEqual(w.dispatch_test(env.scope, op, sender), dict(status='uncertain', delivery_status='uncertain'))
        entry = env.entries()[op]
        self.assertEqual((entry['status'], entry['delivery_status'], entry['delivery_error_type'], bool(entry['readiness_revoked_at'])),
                         ('uncertain', 'uncertain', 'TimeoutError', True))
        env.now = sc.EXTENDED
        self.assertEqual(w.tick(env.scope, {})['status'], 'draft_ready')
        self.assertEqual(env.entries()[op]['status'], 'uncertain')              # outcome unknown: reconcile, do not re-open
        self.assertEqual(w.dispatch_test(env.scope, op, sender), dict(status='not_pending'))
        self.assertEqual(len(calls), 1)

    def test_held_entry_is_superseded_when_content_then_changes(self):
        env, w, op = self.extended(); w.tick(env.scope, {})
        env.now = '2026-11-10T21:40:00Z'; env.rows.append(dict(id='2', scan_at='2026-11-10T21:39:00Z'))
        self.assertEqual(w.tick(env.scope, {})['status'], 'waiting')
        self.assertEqual(env.entries()[op]['status'], 'superseded')
        env.now = sc.EXTENDED
        new = w.tick(env.scope, {})
        self.assertEqual(new['status'], 'draft_ready')
        self.assertEqual({k: v['status'] for k, v in env.entries().items()}, {op: 'superseded', new['operation_id']: 'pending'})

    def test_no_scans_left_holds_nothing_deliverable(self):
        env, w, op = sc.ready_entry(self.store)
        env.now = '2026-11-10T21:36:00Z'; env.rows.clear()
        self.assertEqual(w.tick(env.scope, {})['status'], 'no_scans')
        self.assertEqual(env.entries()[op]['status'], 'superseded')
        self.assertEqual(w.dispatch_test(env.scope, op, sc.refuse), dict(status='not_pending'))


class FreshDispatch(Base):
    """Dispatch never trusts stored readiness: it recaptures the exact scope and claims in one fenced transaction."""
    def sender(self):
        calls = []
        def send(**kw): calls.append(kw); return dict(operation_id=kw['idempotency_key'], provider_receipt='synthetic')
        return calls, send

    def assert_draft_kept(self, env, op):
        self.assertEqual(env.state()['receipts'][op]['state'], 'draft_ready')     # Lauren's draft and receipt are untouched
        self.assertIn(op, env.published)

    def test_unchanged_source_is_recaptured_then_sent_once(self):
        env, w, op = sc.ready_entry(self.store); calls, send = self.sender(); reads = env.fetches
        env.now = '2026-11-10T23:50:00Z'                                          # polling stopped long ago; still unchanged
        self.assertEqual(w.dispatch_test(env.scope, op, send), dict(status='sent'))
        entry = env.entries()[op]
        self.assertEqual((env.fetches - reads, len(calls), entry['claim_captured_at'], entry['claim_fence']), (1, 1, env.now, 2))
        self.assertEqual(env.state()['snapshot']['captured_at'], env.now)         # the claim is bound to the fresh capture

    def test_stopped_polling_changed_rows_photos_and_resumed_scanning_block_dispatch(self):
        changes = dict(
            new_row=lambda env: env.rows.append(dict(id='2', scan_at='2026-11-10T22:10:00Z')),          # scanning resumed
            edited_row=lambda env: env.rows[0].update(source=dict(edited='quantity corrected')),
            deleted_row=lambda env: env.rows.clear(),
            new_photo=lambda env: env.photos.append(dict(record_id='1', name='late.jpg', sha256='c' * 64)))
        for i, (name, change) in enumerate(changes.items()):
            with self.subTest(name):
                env, w, op = sc.ready_entry(StateStore(Path(self.folder.name) / ('c%d.db' % i)))
                env.now = '2026-11-10T22:15:00Z'; change(env)                     # no poll has seen this
                out = w.dispatch_test(env.scope, op, sc.refuse)
                self.assertEqual((out['status'], out['reason']), ('superseded', 'source_changed_or_not_due'))
                self.assertEqual(env.entries()[op]['status'], 'superseded')
                self.assertNotEqual(env.state()['operation_id'], op)              # the timer was updated from what was seen
                self.assertEqual(env.state()['status'], 'no_scans' if name == 'deleted_row' else 'waiting')
                self.assert_draft_kept(env, op)
                self.assertEqual(w.dispatch_test(env.scope, op, sc.refuse), dict(status='not_pending'))

    def test_stopped_polling_schedule_extension_holds(self):
        env, w, op = sc.ready_entry(self.store)
        env.now = '2026-11-10T21:50:00Z'; env.extra = dict(not_before=sc.EXTENDED)   # same rows, later verified shift end
        out = w.dispatch_test(env.scope, op, sc.refuse)
        self.assertEqual((out['status'], out['current_status'], out['due_at'][:16]), ('held', 'waiting', '2026-11-10T23:00'))
        self.assertEqual((env.entries()[op]['status'], env.state()['status']), ('held', 'waiting'))
        calls, send = self.sender(); env.now = sc.EXTENDED
        self.assertEqual(w.dispatch_test(env.scope, op, send), dict(status='not_pending'))   # only a poll releases a hold
        self.assertEqual(w.tick(env.scope, {})['status'], 'draft_ready')
        self.assertEqual(w.dispatch_test(env.scope, op, send), dict(status='sent')); self.assertEqual(len(calls), 1)

    def test_changed_mapping_header_or_template_blocks_dispatch(self):
        env = Env(self.store); env.now = DUE; pub = Publisher(env, version='template-v1')
        op = env.worker(pub).tick(env.scope, {})['operation_id']
        pub.version = 'template-v2'
        out = env.worker(pub).dispatch_test(env.scope, op, sc.refuse)
        self.assertEqual((out['status'], env.entries()[op]['status']), ('superseded', 'superseded'))
        self.assert_draft_kept(env, op)

    def test_source_failure_holds_and_survives_restart(self):
        env, w, op = sc.ready_entry(self.store)
        def outage(): raise ConnectionError('source unavailable')
        env.before_fetch[env.fetches + 1] = outage
        self.assertEqual(w.dispatch_test(env.scope, op, sc.refuse), dict(status='held', reason='recapture_failed', error_type='ConnectionError'))
        entry = env.entries()[op]
        self.assertEqual((entry['status'], entry['held_reason'], entry['held_error_type']), ('held', 'recapture_failed', 'ConnectionError'))
        self.assertEqual(env.record('health:' + env.key)['error_type'], 'ConnectionError')
        self.assert_draft_kept(env, op)
        env.store = self.restart(); fresh = env.worker()
        env.before_fetch[env.fetches + 1] = outage                                # still down after the restart
        self.assertEqual(fresh.dispatch_test(env.scope, op, sc.refuse)['status'], 'held')
        calls, send = self.sender()                                               # source back: a NEW recapture decides
        self.assertEqual(fresh.dispatch_test(env.scope, op, send), dict(status='sent')); self.assertEqual(len(calls), 1)
        self.assertNotIn('held_reason', env.entries()[op])

    def test_source_back_but_changed_after_outage_does_not_send(self):
        env, w, op = sc.ready_entry(self.store)
        def outage(): raise ConnectionError('source unavailable')
        env.before_fetch[env.fetches + 1] = outage
        w.dispatch_test(env.scope, op, sc.refuse)
        env.now = '2026-11-10T21:50:00Z'; env.extra = dict(not_before=sc.EXTENDED)
        self.assertEqual(w.dispatch_test(env.scope, op, sc.refuse)['status'], 'held')
        entry = env.entries()[op]
        self.assertEqual((entry['status'], entry.get('held_reason'), entry['held_until'][:16]), ('held', None, '2026-11-10T23:00'))

    def test_partial_stale_older_and_wrong_scope_captures_hold(self):
        other = dict(SCOPE_OTHER)
        cases = dict(partial=dict(terminal=False), stale=dict(captured_at='2026-11-10T21:40:00Z'),
                     older_than_state=dict(captured_at='2026-11-10T21:30:00Z'), future=dict(captured_at='2026-11-10T22:30:00Z'),
                     wrong_scope=dict(scope=other))
        for i, (name, extra) in enumerate(cases.items()):
            with self.subTest(name):
                env, w, op = sc.ready_entry(StateStore(Path(self.folder.name) / ('p%d.db' % i)))
                before = env.record(env.key)
                env.now = '2026-11-10T21:50:00Z'; env.extra = extra
                out = w.dispatch_test(env.scope, op, sc.refuse)
                self.assertEqual((out['status'], out['reason'], out['error_type']), ('held', 'recapture_failed', 'ValueError'))
                self.assertEqual(env.entries()[op]['status'], 'held')
                self.assertEqual(env.record(env.key), before)                     # a bad capture never moves the timer
                self.assert_draft_kept(env, op)

    def test_change_between_recapture_and_claim_loses_the_fence(self):
        """The dispatcher stalls after its recapture; its lease expires; a poll sees a new scan. The claim must fail."""
        env, w, op = sc.ready_entry(self.store); out = {}
        def stalled():
            env.now = '2026-11-10T21:41:00Z'                                      # past the dispatcher's 300 second lease
            env.rows.append(dict(id='2', scan_at='2026-11-10T21:40:00Z'))
            out['poll'] = env.worker(owner='poller').tick(env.scope, {})
        env.before_fetch[env.fetches + 1] = stalled
        self.assertEqual(w.dispatch_test(env.scope, op, sc.refuse), dict(status='lease_lost'))
        self.assertEqual((out['poll']['status'], env.entries()[op]['status']), ('waiting', 'superseded'))
        self.assertNotEqual(env.state()['operation_id'], op)

    def test_busy_scope_is_not_dispatched(self):
        env, w, op = sc.ready_entry(self.store)
        with self.store.transaction('lease:' + env.key) as lease:
            lease.update(token='poller', fence=9, expires='2026-11-10T21:40:00Z')
        reads = env.fetches
        self.assertEqual(w.dispatch_test(env.scope, op, sc.refuse), dict(status='busy'))
        self.assertEqual((env.fetches, env.entries()[op]['status']), (reads, 'pending'))

    def test_publisher_bound_to_another_scope_cannot_dispatch(self):
        env, w, op = sc.ready_entry(self.store)
        class Other:
            binding = dict(client_id='someone-else', site_id='s', list_id='l', job='j')
        reads = env.fetches
        self.assertEqual(env.worker(Other()).dispatch_test(env.scope, op, sc.refuse), dict(status='error', error_type='RendererBindingMismatch'))
        self.assertEqual(env.fetches, reads)

    def test_unknown_outcome_survives_restart_and_is_never_resent(self):
        env, w, op = sc.ready_entry(self.store); attempts = []
        def lost(**kw): attempts.append(kw); raise TimeoutError('no answer')
        self.assertEqual(w.dispatch_test(env.scope, op, lost), dict(status='uncertain', delivery_status='uncertain'))
        env.store = self.restart(); w = env.worker()
        self.assertEqual(w.dispatch_test(env.scope, op, lost), dict(status='not_pending'))
        env.now = '2026-11-10T21:40:00Z'
        self.assertEqual(w.tick(env.scope, {})['status'], 'draft_ready')
        self.assertEqual((env.entries()[op]['status'], len(attempts)), ('uncertain', 1))


class AttemptFencing(Base):
    """Every delivery attempt has its own ID. Only a terminal, attempt-bound proof from the transport can lead to a
    second attempt, and late callbacks of an old attempt can never change a newer one. Test transport only, and
    only under the FAKE verified provider contract; without one nothing is ever retried (see NoRetryByDefault)."""
    worker_kw = VERIFIED
    def setUp(self):
        super().setUp()
        self.env, self.w, self.op = sc.ready_entry(self.store, **self.worker_kw)
        self.calls = []

    def entry(self): return self.env.entries()[self.op]
    def aid(self): return self.entry()['attempt_id']

    def ok(self, **kw):
        self.calls.append(kw); return dict(operation_id=kw['idempotency_key'], attempt_id=kw['attempt_id'], provider_receipt='synthetic-' + str(len(self.calls)))

    def lost(self, **kw):
        self.calls.append(kw); raise TimeoutError('no answer')

    def proof(self, **change):
        def lookup(**kw):
            return dict(dict(operation_id=kw['idempotency_key'], attempt_id=kw['attempt_id'], delivered=False, terminal=True,
                             attempt_closed=True, proof_id='synthetic-provider-record-1'), **change)
        return lookup

    def blocked(self, finish):
        """Start a dispatch whose sender blocks inside the transport until released."""
        import threading
        entered, release, result = threading.Event(), threading.Event(), {}
        def sender(**kw):
            self.calls.append(kw); entered.set(); release.wait(10)
            return finish(kw)
        thread = threading.Thread(target=lambda: result.update(out=self.w.dispatch_test(self.env.scope, self.op, sender)))
        thread.start(); self.assertTrue(entered.wait(10))
        def let_go():
            release.set(); thread.join(10); return result['out']
        return let_go

    def test_qa_repro_blocked_sender_reconcile_poll_second_dispatch(self):
        let_go = self.blocked(lambda kw: dict(operation_id=kw['idempotency_key'], attempt_id=kw['attempt_id'], provider_receipt='first'))
        w, env, op = self.env.worker(), self.env, self.op
        bare = w.reconcile_test(env.scope, op, lambda **kw: dict(operation_id=op, delivered=False))    # "not arrived yet"
        self.assertEqual(bare, dict(status='uncertain', reason='no_terminal_proof_for_this_attempt'))
        full = w.reconcile_test(env.scope, op, self.proof())                                           # even full proof, mid-flight
        self.assertEqual((full['status'], full['reason']), ('uncertain', 'attempt_may_still_be_in_flight'))
        env.now = '2026-11-10T21:40:00Z'
        self.assertEqual(w.tick(env.scope, {})['status'], 'draft_ready')                               # a fresh poll
        self.assertEqual((self.entry()['status'], self.entry()['delivery_status']), ('uncertain', 'uncertain'))
        self.assertEqual(w.dispatch_test(env.scope, op, self.ok), dict(status='not_pending'))          # second dispatch
        self.assertEqual(let_go(), dict(status='sent'))                                                # first is NOT called superseded
        entry = self.entry()
        self.assertEqual((len(self.calls), entry['status'], entry['ack']['provider_receipt'], len(entry['attempts'])), (1, 'sent', 'first', 1))

    def test_delivered_false_variants_are_never_proof(self):
        self.w.dispatch_test(self.env.scope, self.op, self.lost)
        aid, op = self.aid(), self.op
        vague = [lambda **kw: None, lambda **kw: 'not found', lambda **kw: dict(operation_id=op, delivered=False),
                 lambda **kw: dict(operation_id=op, attempt_id=aid, delivered=False),
                 lambda **kw: dict(operation_id=op, attempt_id=aid, delivered=False, terminal=True),
                 lambda **kw: dict(operation_id=op, attempt_id=aid, delivered=False, attempt_closed=True, proof_id='x'),
                 self.proof(proof_id=''), self.proof(proof_id=None), self.proof(terminal='yes'), self.proof(attempt_closed=1),
                 self.proof(delivered=None), self.proof(delivered='false'), self.proof(operation_id='another-operation'),
                 self.proof(attempt_id=None), self.proof(attempt_id='0' * 32),
                 lambda **kw: dict(operation_id=op, attempt_id=aid, delivered='maybe')]
        for i, lookup in enumerate(vague):
            with self.subTest(i):
                out = self.w.reconcile_test(self.env.scope, op, lookup)
                self.assertEqual(out, dict(status='uncertain', reason='no_terminal_proof_for_this_attempt'))
        entry = self.entry()
        self.assertEqual((entry['status'], entry['delivery_status'], entry['attempts'][0]['reconcile_checks']), ('uncertain', 'uncertain', len(vague)))
        self.env.now = '2026-11-10T21:40:00Z'; self.w.tick(self.env.scope, {})
        self.assertEqual(self.w.dispatch_test(self.env.scope, op, self.ok), dict(status='not_pending'))
        self.assertEqual(len(self.calls), 1)

    def test_provider_lookup_outage_stays_uncertain(self):
        self.w.dispatch_test(self.env.scope, self.op, self.lost)
        def down(**kw): raise ConnectionError('provider lookup unavailable')
        self.assertEqual(self.w.reconcile_test(self.env.scope, self.op, down), dict(status='uncertain', reason='lookup_failed', error_type='ConnectionError'))
        self.assertEqual(self.entry()['status'], 'uncertain')

    def test_delivered_true_bound_to_the_attempt_is_recorded_as_sent(self):
        self.w.dispatch_test(self.env.scope, self.op, self.lost)
        out = self.w.reconcile_test(self.env.scope, self.op, lambda **kw: dict(operation_id=self.op, attempt_id=kw['attempt_id'], delivered=True, provider_receipt='found'))
        self.assertEqual(out, dict(status='sent', delivery_status='sent'))
        entry = self.entry()
        self.assertEqual((entry['ack']['provider_receipt'], entry['attempts'][0]['state'], entry['attempts'][0]['error_type']), ('found', 'acked', 'TimeoutError'))
        self.assertEqual(self.w.reconcile_test(self.env.scope, self.op, self.proof()), dict(status='nothing_to_reconcile'))
        self.assertEqual(self.w.dispatch_test(self.env.scope, self.op, self.ok), dict(status='not_pending'))

    def retried(self):
        """Attempt 1 returned an error; terminal proof closes it; a fresh poll + recapture allows attempt 2."""
        self.w.dispatch_test(self.env.scope, self.op, self.lost)
        first = self.aid()
        self.assertEqual(self.w.reconcile_test(self.env.scope, self.op, self.proof()), dict(status='held', delivery_status='not_delivered'))
        self.assertEqual(self.w.dispatch_test(self.env.scope, self.op, self.ok), dict(status='not_pending'))   # not sendable straight away
        self.env.now = '2026-11-10T21:40:00Z'
        self.assertEqual(self.w.tick(self.env.scope, {})['status'], 'draft_ready')
        return first

    def test_terminal_proof_then_fresh_poll_and_recapture_allow_one_new_attempt(self):
        first = self.retried(); reads = self.env.fetches
        self.assertEqual(self.w.dispatch_test(self.env.scope, self.op, self.ok), dict(status='sent'))
        entry = self.entry(); one, two = entry['attempts']
        self.assertEqual((self.env.fetches - reads, len(self.calls), one['id'], two['id'] != first, two['number']), (1, 2, first, True, 2))
        self.assertEqual((one['state'], one['closed_proof']['proof_id'], one['error_type'], two['state']),
                         ('closed_not_delivered', 'synthetic-provider-record-1', 'TimeoutError', 'acked'))   # audit kept
        self.assertEqual(self.calls[0]['idempotency_key'], self.calls[1]['idempotency_key'])
        self.assertNotEqual(self.calls[0]['attempt_id'], self.calls[1]['attempt_id'])

    def test_proof_for_an_older_attempt_cannot_reopen_a_newer_one(self):
        first = self.retried()
        self.w.dispatch_test(self.env.scope, self.op, self.lost)                    # attempt 2 also has an unknown outcome
        second = self.aid(); before = self.entry()
        out = self.w.reconcile_test(self.env.scope, self.op, self.proof(attempt_id=first))   # proof names attempt 1
        self.assertEqual(out, dict(status='uncertain', reason='no_terminal_proof_for_this_attempt'))
        after = self.entry()
        self.assertEqual((after['status'], after['attempt_id'], after['attempts'][1]['state']), ('uncertain', second, 'returned_error'))
        self.assertEqual(before['attempts'][0], after['attempts'][0])

    def late(self, finish):
        """Attempt 1 hangs past its deadline, is closed by terminal proof, attempt 2 is sent, THEN attempt 1 returns."""
        let_go = self.blocked(finish)
        first = self.aid(); w = self.env.worker()
        self.env.now = '2026-11-10T21:51:00Z'                                       # past the 15 minute attempt deadline
        self.assertEqual(w.reconcile_test(self.env.scope, self.op, self.proof()), dict(status='held', delivery_status='not_delivered'))
        self.assertEqual(w.tick(self.env.scope, {})['status'], 'draft_ready')
        self.assertEqual(w.dispatch_test(self.env.scope, self.op, self.ok), dict(status='sent'))
        settled = self.entry()
        return first, settled, let_go()

    def test_late_ack_of_a_closed_attempt_cannot_corrupt_the_newer_attempt(self):
        first, settled, out = self.late(lambda kw: dict(operation_id=kw['idempotency_key'], attempt_id=kw['attempt_id'], provider_receipt='late-first'))
        self.assertEqual(out, dict(status='stale_attempt', delivery_status='sent'))
        entry = self.entry(); one, two = entry['attempts']
        self.assertEqual((entry['status'], entry['ack'], entry['attempt_id']), ('sent', settled['ack'], settled['attempt_id']))
        self.assertEqual((one['late_ack']['provider_receipt'], one['closed_proof']['proof_id'], entry['duplicate_delivery_evidence'], two), ('late-first', 'synthetic-provider-record-1', first, settled['attempts'][1]))

    def test_late_failure_of_a_closed_attempt_cannot_corrupt_the_newer_attempt(self):
        def fail(kw): raise ConnectionError('late failure')
        first, settled, out = self.late(fail)
        self.assertEqual(out, dict(status='stale_attempt', delivery_status='uncertain'))
        entry = self.entry()
        self.assertEqual((entry['status'], entry['delivery_status'], entry['ack']), ('sent', 'sent', settled['ack']))
        self.assertNotIn('delivery_error_type', entry)
        self.assertEqual((entry['attempts'][0]['late_error_type'], entry['attempts'][0]['state'], entry['attempts'][1]), ('ConnectionError', 'closed_not_delivered', settled['attempts'][1]))

    def test_late_ack_while_on_hold_cancels_the_retry(self):
        let_go = self.blocked(lambda kw: dict(operation_id=kw['idempotency_key'], attempt_id=kw['attempt_id'], provider_receipt='late-first'))
        w = self.env.worker(); self.env.now = '2026-11-10T21:51:00Z'
        self.assertEqual(w.reconcile_test(self.env.scope, self.op, self.proof())['status'], 'held')
        self.assertEqual(let_go(), dict(status='sent'))                              # it was delivered after all
        entry = self.entry()
        self.assertEqual((entry['status'], entry['proof_contradicted_by_late_ack'], entry['attempts'][0]['closed_proof']['proof_id']), ('sent', True, 'synthetic-provider-record-1'))
        self.assertEqual(w.tick(self.env.scope, {})['status'], 'draft_ready')
        self.assertEqual(w.dispatch_test(self.env.scope, self.op, self.ok), dict(status='not_pending'))
        self.assertEqual(len(self.calls), 1)

    def test_crash_mid_send_then_restart_needs_deadline_and_proof(self):
        def dies(**kw): self.calls.append(kw); raise Crash()
        with self.assertRaises(Crash):
            self.w.dispatch_test(self.env.scope, self.op, dies)
        self.env.store = self.restart(); w = self.env.worker()
        entry = self.entry()
        self.assertEqual((entry['status'], entry['attempts'][0]['state']), ('uncertain', 'in_flight'))
        self.assertEqual(w.dispatch_test(self.env.scope, self.op, self.ok), dict(status='not_pending'))
        self.env.now = '2026-11-10T21:49:59Z'
        self.assertEqual(w.reconcile_test(self.env.scope, self.op, self.proof())['reason'], 'attempt_may_still_be_in_flight')
        self.env.now = '2026-11-10T21:50:00Z'
        self.assertEqual(w.reconcile_test(self.env.scope, self.op, lambda **kw: dict(operation_id=self.op, delivered=False))['status'], 'uncertain')
        self.assertEqual(w.reconcile_test(self.env.scope, self.op, self.proof()), dict(status='held', delivery_status='not_delivered'))
        self.env.store = self.restart(); w = self.env.worker()
        self.assertEqual(w.tick(self.env.scope, {})['status'], 'draft_ready')
        self.assertEqual(w.dispatch_test(self.env.scope, self.op, self.ok), dict(status='sent'))
        self.assertEqual((len(self.calls), [a['state'] for a in self.entry()['attempts']]), (2, ['closed_not_delivered', 'acked']))

    def test_ack_naming_another_attempt_is_not_accepted(self):
        out = self.w.dispatch_test(self.env.scope, self.op, lambda **kw: dict(operation_id=kw['idempotency_key'], attempt_id='0' * 32))
        self.assertEqual(out, dict(status='uncertain', delivery_status='uncertain'))
        self.assertEqual((self.entry()['status'], self.entry()['attempts'][0]['error_type']), ('uncertain', 'ValueError'))

    def test_draft_and_receipt_survive_every_delivery_outcome(self):
        self.retried(); self.w.dispatch_test(self.env.scope, self.op, self.lost)
        self.assertEqual(self.env.state()['receipts'][self.op]['state'], 'draft_ready')
        self.assertIn(self.op, self.env.published)
        self.assertEqual(self.entry()['receipt'], self.env.published[self.op])


class NoRetryByDefault(Base):
    """Safest default: with no provider contract verified in code, an uncertain delivery is never retried, the
    provider is never queried, and nothing a caller supplies at run time changes that."""
    def uncertain(self, **worker_kw):
        env, w, op = sc.ready_entry(self.store, **worker_kw); calls = []
        def lost(**kw): calls.append(kw); raise TimeoutError('no answer')
        self.assertEqual(w.dispatch_test(env.scope, op, lost), dict(status='uncertain', delivery_status='uncertain'))
        return env, w, op, calls

    def full_proof(self, **kw):
        return dict(operation_id=kw['idempotency_key'], attempt_id=kw['attempt_id'], delivered=False, terminal=True,
                    attempt_closed=True, proof_id='synthetic-provider-record-1')

    def never_retried(self, env, w, op, calls, reason, queried):
        asked = []
        def lookup(**kw): asked.append(kw); return self.full_proof(**kw)
        for at in ('2026-11-10T21:36:00Z', '2026-11-10T21:51:00Z', '2026-11-11T21:35:00Z'):      # before and long after any deadline
            env.now = at
            self.assertEqual(w.reconcile_test(env.scope, op, lookup), dict(status='uncertain', reason=reason))
        self.assertEqual(bool(asked), queried)
        entry = env.entries()[op]
        self.assertEqual((entry['status'], entry['delivery_status'], entry['attempts'][0]['state'], len(entry['attempts'])),
                         ('uncertain', 'uncertain', 'returned_error', 1))
        env.rows = list(env.rows)
        env.now = '2026-11-11T21:36:00Z'
        self.assertEqual(w.tick(env.scope, {})['status'], 'draft_ready')
        self.assertEqual(w.dispatch_test(env.scope, op, lambda **kw: self.fail('retried')), dict(status='not_pending'))
        self.assertEqual(len(calls), 1)
        self.assertEqual(env.state()['receipts'][op]['state'], 'draft_ready'); self.assertIn(op, env.published)   # draft kept
        return entry

    def test_real_registry_is_empty(self):
        self.assertEqual(dict(transport.VERIFIED_CONTRACTS), {})
        with self.assertRaises(TypeError):
            transport.VERIFIED_CONTRACTS['x'] = 'y'
        self.assertFalse(transport.verified(FAKE_CONTRACT)); self.assertFalse(transport.permits_retry(FAKE_CONTRACT, None, 900))

    def test_no_contract_full_looking_proof_and_passed_deadline_change_nothing(self):
        self.never_retried(*self.uncertain(), reason='no_verified_provider_contract', queried=False)

    def test_delivered_true_is_not_trusted_without_a_contract_either(self):
        env, w, op, calls = self.uncertain()
        out = w.reconcile_test(env.scope, op, lambda **kw: dict(operation_id=op, attempt_id=kw['attempt_id'], delivered=True))
        self.assertEqual(out, dict(status='uncertain', reason='no_verified_provider_contract'))
        self.assertEqual(env.entries()[op]['status'], 'uncertain'); self.assertNotIn('ack', env.entries()[op])

    def test_unknown_or_unlisted_or_altered_contracts_do_not_count(self):
        from dataclasses import replace
        cases = dict(
            contract_but_default_registry=dict(provider_contract=FAKE_CONTRACT),
            caller_boolean=dict(provider_contract=True, contract_registry=FAKE_REGISTRY),
            plain_dict=dict(provider_contract=dict(retry_after_terminal_non_delivery=True), contract_registry=FAKE_REGISTRY),
            unlisted_provider=dict(provider_contract=replace(FAKE_CONTRACT, provider='unknown-provider'), contract_registry=FAKE_REGISTRY),
            altered_after_review=dict(provider_contract=replace(FAKE_CONTRACT, sender_timeout_seconds=601), contract_registry=FAKE_REGISTRY),
            registry_not_a_mapping=dict(provider_contract=FAKE_CONTRACT, contract_registry=[FAKE_CONTRACT.digest()]),
            registry_says_true=dict(provider_contract=FAKE_CONTRACT, contract_registry={(FAKE_CONTRACT.provider, '1'): True}))
        for i, (name, kw) in enumerate(cases.items()):
            with self.subTest(name):
                self.store = StateStore(Path(self.folder.name) / ('u%d.db' % i))
                self.never_retried(*self.uncertain(**kw), reason='no_verified_provider_contract', queried=False)

    def test_verified_contract_that_does_not_permit_retry_keeps_proof_as_evidence_only(self):
        from dataclasses import replace
        weak = dict(no_retry_permission=replace(FAKE_CONTRACT, retry_after_terminal_non_delivery=False),
                    no_terminal_status=replace(FAKE_CONTRACT, terminal_status_per_attempt=False),
                    no_attempt_close=replace(FAKE_CONTRACT, honours_attempt_closed=False),
                    no_sender_timeout=replace(FAKE_CONTRACT, sender_timeout_seconds=0),
                    timeout_longer_than_attempt=replace(FAKE_CONTRACT, sender_timeout_seconds=901))
        for i, (name, contract) in enumerate(weak.items()):
            with self.subTest(name):
                self.store = StateStore(Path(self.folder.name) / ('w%d.db' % i))
                registry = {(contract.provider, contract.version): contract.digest()}
                entry = self.never_retried(*self.uncertain(provider_contract=contract, contract_registry=registry),
                                           reason='retry_not_permitted_by_provider_contract', queried=True)
                self.assertEqual(entry['attempts'][0]['terminal_proof_not_acted_on']['proof_id'], 'synthetic-provider-record-1')

    def test_contract_without_at_most_once_is_not_verified_at_all(self):
        from dataclasses import replace
        contract = replace(FAKE_CONTRACT, at_most_once_per_idempotency_key=False)
        registry = {(contract.provider, contract.version): contract.digest()}
        self.assertFalse(transport.verified(contract, registry))
        for blank in ('verified_by', 'verified_at', 'evidence'):
            c = replace(FAKE_CONTRACT, **{blank: ' '})
            self.assertFalse(transport.verified(c, {(c.provider, c.version): c.digest()}), blank)

    def test_deliver_refuses_to_send_without_a_verified_contract(self):
        env, w, op = sc.ready_entry(self.store); reads = env.fetches
        for worker in (w, env.worker(provider_contract=FAKE_CONTRACT), env.worker(provider_contract=True, contract_registry=FAKE_REGISTRY)):
            self.assertEqual(transport.deliver(worker, env.scope, op, lambda **kw: self.fail('sent')), dict(status='transport_disabled'))
        self.assertEqual((env.fetches, env.entries()[op]['status']), (reads, 'pending'))     # nothing read, nothing claimed
        sent = []
        def send(**kw): sent.append(kw); return dict(operation_id=kw['idempotency_key'], attempt_id=kw['attempt_id'])
        self.assertEqual(transport.deliver(env.worker(**VERIFIED), env.scope, op, send), dict(status='sent'))   # fake contract only
        self.assertEqual(len(sent), 1)

    def test_deadline_alone_never_closes_an_attempt_even_with_a_contract(self):
        env, w, op = sc.ready_entry(self.store, **VERIFIED)
        with self.assertRaises(Crash):
            w.dispatch_test(env.scope, op, lambda **kw: (_ for _ in ()).throw(Crash()))
        env.now = '2026-11-12T00:00:00Z'                                             # far past the deadline, no proof at all
        for lookup in (lambda **kw: None, lambda **kw: dict(operation_id=op, attempt_id=kw['attempt_id'], delivered=False)):
            self.assertEqual(w.reconcile_test(env.scope, op, lookup), dict(status='uncertain', reason='no_terminal_proof_for_this_attempt'))
        self.assertEqual(w.tick(env.scope, {})['status'], 'draft_ready')
        self.assertEqual((env.entries()[op]['status'], env.entries()[op]['attempts'][0]['state']), ('uncertain', 'in_flight'))


SCOPE_OTHER = dict(job='OTHER-JOB', site_id='other-site', list_id='other-list', business_date='2026-11-10',
                   client_id='client', shift='1', zone='America/Chicago')


class Timer(Base):
    def test_out_of_order_capture_changes_nothing(self):
        env = Env(self.store); w = env.worker()
        self.assertEqual(w.tick(env.scope, {})['status'], 'waiting')
        before = env.record(env.key)
        env.now = '2026-11-10T21:01:00Z'
        env.extra = dict(captured_at='2026-11-10T20:59:30Z')     # an older capture arriving late
        env.rows.append(dict(id='2', scan_at='2026-11-10T20:40:00Z'))
        out = w.tick(env.scope, {})
        self.assertEqual((out['status'], out['error_type']), ('error', 'ValueError'))
        self.assertEqual(env.record(env.key), before)

    def test_capture_taking_time_is_not_a_future_observation(self):
        env = Env(self.store)
        env.before_fetch[1] = lambda: setattr(env, 'now', '2026-11-10T21:00:03Z')   # the read itself took 3 seconds
        self.assertEqual(env.worker().tick(env.scope, {})['status'], 'waiting')

    def test_other_shift_photo_does_not_restart_timer(self):
        env = Env(self.store); w = env.worker()
        first = w.tick(env.scope, {})
        env.now = '2026-11-10T21:10:00Z'
        env.photos.append(dict(record_id='99', name='other-shift.jpg', sha256='a' * 64))
        second = w.tick(env.scope, {})
        self.assertEqual((second['due_at'], second['revision'], second['operation_id']),
                         (first['due_at'], first['revision'], first['operation_id']))
        env.now = '2026-11-10T21:20:00Z'
        env.photos.append(dict(record_id='1', name='late-upload.jpg', sha256='b' * 64))
        third = w.tick(env.scope, {})
        self.assertEqual(third['revision'], first['revision'] + 1)
        self.assertTrue(third['due_at'].startswith('2026-11-10T22:20'))

    def test_resumed_scans_edits_and_deletes_restart_the_hour(self):
        env = Env(self.store); w = env.worker()
        self.assertTrue(w.tick(env.scope, {})['due_at'].startswith('2026-11-10T21:30'))
        env.now = '2026-11-10T21:25:00Z'; env.rows.append(dict(id='2', scan_at='2026-11-10T21:24:00Z'))   # scanning resumed
        self.assertTrue(w.tick(env.scope, {})['due_at'].startswith('2026-11-10T22:25'))
        env.now = '2026-11-10T22:24:59Z'
        self.assertEqual(w.tick(env.scope, {})['status'], 'waiting')
        env.now = '2026-11-10T22:25:00Z'
        ready = w.tick(env.scope, {})
        self.assertEqual(ready['status'], 'draft_ready')
        env.now = '2026-11-10T22:30:00Z'; env.rows[0]['source'] = dict(edited='quantity corrected')        # late edit
        edited = w.tick(env.scope, {})
        self.assertEqual(edited['status'], 'waiting'); self.assertTrue(edited['due_at'].startswith('2026-11-10T23:30'))
        self.assertEqual(env.entries()[ready['operation_id']]['status'], 'superseded')
        env.now = '2026-11-10T22:40:00Z'; env.rows.pop()                                                   # late delete
        deleted = w.tick(env.scope, {})
        self.assertEqual(deleted['status'], 'waiting'); self.assertTrue(deleted['due_at'].startswith('2026-11-10T23:40'))
        env.now = '2026-11-10T22:45:00Z'; env.rows.clear()
        self.assertEqual(w.tick(env.scope, {})['status'], 'no_scans')
        self.assertEqual(len(env.renders), 1)

    def test_quiet_hour_is_provisional_and_schedule_end_is_honoured(self):
        env = Env(self.store); w = env.worker()
        first = w.tick(env.scope, {})
        self.assertEqual((first['timing_basis'], first['day_completion'], first['client_release']),
                         ('provisional_quiet_window_schedule_unknown', 'not_established', 'held'))
        other = Env(StateStore(Path(self.folder.name) / 'b.db')); other.now = DUE
        other.extra = dict(not_before='2026-11-10T23:00:00Z')          # verified scheduled end of the shift
        held = other.worker().tick(other.scope, {})
        self.assertEqual((held['status'], held['timing_basis']), ('waiting', 'verified_schedule'))
        self.assertTrue(held['due_at'].startswith('2026-11-10T23:00'))
        other.now = '2026-11-10T23:00:00Z'
        self.assertEqual(other.worker().tick(other.scope, {})['status'], 'draft_ready')

    def test_dst_fall_back_and_overnight(self):
        env = Env(self.store)                                         # America/Chicago leaves DST at 07:00Z on 2026-11-01
        env.rows = [dict(id='1', scan_at='2026-11-01T06:40:00Z')]     # 01:40 CDT, before the repeated hour
        env.now = '2026-11-01T07:39:59Z'                              # 01:39 CST: the wall clock reads EARLIER than the scan
        w = env.worker()
        self.assertEqual(w.tick(env.scope, {})['status'], 'waiting')
        env.now = '2026-11-01T07:40:00Z'
        self.assertEqual(w.tick(env.scope, {})['status'], 'draft_ready')
        night = Env(StateStore(Path(self.folder.name) / 'n.db'), shift='3')
        night.rows = [dict(id='1', scan_at='2026-11-11T07:30:00Z')]   # 01:30 local on the next calendar day, same business date
        night.now = '2026-11-11T08:30:00Z'
        self.assertEqual(night.worker().tick(night.scope, {})['status'], 'draft_ready')

    def test_naive_or_future_scan_times_never_start_a_timer(self):
        for bad in ('2026-11-10T20:30:00', '2026-11-10', '2026-11-10T21:30:00Z'):
            with self.subTest(bad):
                env = Env(StateStore(Path(self.folder.name) / (sf.digest(bad)[:8] + '.db')))
                env.rows = [dict(id='1', scan_at=bad)]
                out = env.worker().tick(env.scope, {})
                self.assertEqual(out['status'], 'error'); self.assertIsNone(env.state())

    def test_partial_pagination_and_failed_capture_leave_state_unchanged(self):
        env = Env(self.store); w = env.worker()
        env.extra = dict(terminal=False)
        self.assertEqual(w.tick(env.scope, {})['status'], 'error'); self.assertIsNone(env.state())
        env.extra = {}
        w.tick(env.scope, {}); before = env.record(env.key)
        env.now = '2026-11-10T21:05:00Z'; env.rows.append(dict(id='2', scan_at='2026-11-10T21:04:00Z'))
        env.extra = dict(terminal=False)
        self.assertEqual(w.tick(env.scope, {})['status'], 'error')
        def broken(): raise ConnectionError('page 3 of 7 failed')
        env.extra = {}; env.before_fetch[env.fetches + 1] = broken
        out = w.tick(env.scope, {})
        self.assertEqual((out['status'], out['error_type']), ('error', 'ConnectionError'))
        self.assertEqual(env.record(env.key), before)
        self.assertEqual(env.record('lease:' + env.key)['expires'], env.now)    # released, not left held

    def test_repeated_or_missing_ids_rejected(self):
        env = Env(self.store); env.rows = [dict(id='1', scan_at='2026-11-10T20:30:00Z'), dict(id='1', scan_at='2026-11-10T20:31:00Z')]
        self.assertEqual(env.worker().tick(env.scope, {})['status'], 'error'); self.assertIsNone(env.state())


class Blobs(Base):
    def test_photo_bytes_stay_out_of_the_state_row(self):
        env = Env(self.store); env.now = DUE
        photo = bytes(range(256)) * 8192                               # 2 MB synthetic photo
        env.blobs = {'photo-1': base64.b64encode(photo).decode()}
        env.photos = [dict(record_id='1', name='photo-1', sha256=sf.digest(photo))]
        seen = {}
        def render(snapshot, op):
            seen['bytes'] = base64.b64decode(snapshot['photo_blobs']['photo-1']); return env.render(snapshot, op)
        self.assertEqual(env.worker(render).tick(env.scope, {})['status'], 'draft_ready')
        self.assertEqual(seen['bytes'], photo)                         # the renderer still gets the real photo
        with sqlite3.connect(self.path) as db:
            sizes = dict(db.execute("SELECT key, length(value) FROM scan_draft_state").fetchall())
            blobs = db.execute("SELECT count(*), sum(length(body)) FROM scan_draft_blob").fetchone()
        self.assertLess(max(sizes.values()), 4000, sizes)
        self.assertGreaterEqual(blobs[1], len(photo))
        stub = env.state()['snapshot']
        import json
        slim = json.loads(self.store.get_blob(stub['ref']))
        self.assertEqual(self.store.get_blob(slim['photo_blob_refs']['photo-1']), photo)
        self.assertNotIn('photo_blobs', slim)
        for _ in range(3):
            env.worker(render).tick(env.scope, {})
        with sqlite3.connect(self.path) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM scan_draft_blob WHERE length(body) > 1000000").fetchone()[0], 1)

    def test_tampered_blob_is_refused(self):
        sha = self.store.put_blob(b'original')
        with sqlite3.connect(self.path) as db:
            db.execute("UPDATE scan_draft_blob SET body=? WHERE sha256=?", (b'changed', sha))
        with self.assertRaises(ValueError):
            self.store.get_blob(sha)


if __name__ == '__main__':
    unittest.main()

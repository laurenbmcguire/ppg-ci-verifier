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


class Base(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(); self.addCleanup(self.folder.cleanup)
        self.path = Path(self.folder.name) / 'state.db'
        self.store = StateStore(self.path)

    def restart(self):
        return StateStore(self.path)        # a new process: nothing in memory survives


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

    def test_unknown_outcome_is_reconciled_by_asking_never_by_resending(self):
        env, w, op = sc.ready_entry(self.store); attempts = []
        def lost(**kw): attempts.append(kw); raise TimeoutError('no answer')
        self.assertEqual(w.dispatch_test(env.scope, op, lost), dict(status='uncertain', delivery_status='uncertain'))
        env.store = self.restart(); w = env.worker()
        self.assertEqual(w.dispatch_test(env.scope, op, lost), dict(status='not_pending'))
        for vague in (lambda **kw: None, lambda **kw: dict(operation_id='other', delivered=True),
                      lambda **kw: dict(operation_id=op, delivered='maybe')):
            self.assertEqual(w.reconcile_test(env.scope, op, vague), dict(status='uncertain'))
        def down(**kw): raise ConnectionError()
        self.assertEqual(w.reconcile_test(env.scope, op, down), dict(status='uncertain', error_type='ConnectionError'))
        self.assertEqual(env.entries()[op]['status'], 'uncertain')
        found = w.reconcile_test(env.scope, op, lambda **kw: dict(operation_id=op, delivered=True, provider_receipt='synthetic'))
        self.assertEqual(found, dict(status='sent', delivery_status='sent'))
        self.assertEqual(env.entries()[op]['ack']['provider_receipt'], 'synthetic')
        self.assertEqual(w.reconcile_test(env.scope, op, down), dict(status='nothing_to_reconcile'))
        self.assertEqual(w.dispatch_test(env.scope, op, lost), dict(status='not_pending'))
        self.assertEqual(len(attempts), 1)

    def test_confirmed_not_delivered_needs_a_fresh_poll_and_recapture_before_any_resend(self):
        env, w, op = sc.ready_entry(self.store); calls, send = self.sender()
        def lost(**kw): raise TimeoutError()
        w.dispatch_test(env.scope, op, lost)
        self.assertEqual(w.reconcile_test(env.scope, op, lambda **kw: dict(operation_id=op, delivered=False)),
                         dict(status='held', delivery_status='not_delivered'))
        self.assertEqual(w.dispatch_test(env.scope, op, send), dict(status='not_pending'))   # not sendable straight away
        env.now = '2026-11-10T21:40:00Z'
        self.assertEqual(w.tick(env.scope, {})['status'], 'draft_ready')                     # a fresh poll releases it
        self.assertEqual(w.dispatch_test(env.scope, op, send), dict(status='sent'))          # and dispatch recaptures again
        self.assertEqual(len(calls), 1)


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

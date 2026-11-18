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

    def test_reverted_content_requeues_only_if_never_attempted(self):
        env = Env(self.store); env.now = DUE; w = env.worker()
        a = w.tick(env.scope, {})
        env.rows.append(dict(id='2', scan_at='2026-11-10T21:30:00Z')); env.now = '2026-11-10T21:36:00Z'
        self.assertEqual(w.tick(env.scope, {})['status'], 'waiting')
        self.assertEqual(env.entries()[a['operation_id']]['status'], 'superseded')
        env.rows.pop(); env.now = '2026-11-10T21:37:00Z'     # the late row is deleted again
        self.assertEqual(w.tick(env.scope, {})['status'], 'draft_ready')   # known finished content: no second render
        self.assertEqual(env.entries()[a['operation_id']]['status'], 'pending')
        self.assertEqual(len(env.renders), 1)


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
        self.assertEqual(w.tick(env.scope, {})['status'], 'draft_ready')
        self.assertEqual(env.entries()[op]['status'], 'superseded')     # delivery outcome unknown: reconcile, do not resend


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

"""A stored draft is a HISTORICAL record of one capture. Reusing it for a later, content-identical observation
must never present it as regenerated, current or freshly verified, and must never rewrite it.

Synthetic DEMO-CLIENT / TEST-9001 source only (tests/demo_client_fixture.py)."""
import io
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock
import demo_client_fixture as demo
from hosting import source_first as sf, last_scan_runner as runner
from hosting.last_scan_local import StateStore, StoredDraftRouter, BlobArtifacts
from hosting.last_scan_service import ScheduledWorker, source_adapter
from hosting.last_scan_drafts import scope_key

T1, T2, T3, T4, T5, T6 = ('2026-11-10T21:35:00+00:00', '2026-11-10T21:45:00+00:00', '2026-11-10T21:55:00+00:00',
                          '2026-11-10T22:05:00+00:00', '2026-11-10T22:10:00+00:00', '2026-11-10T23:10:00+00:00')
SCAN = '2026-11-10T20:30:00+00:00'
FORBIDDEN_WORDS = ('regenerated', 'fresh', 'current', 'verified', 'up to date', 'latest')


class Historical(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(); self.addCleanup(self.folder.cleanup)
        self.path = Path(self.folder.name) / 'state.db'
        self.now = T1
        clock = mock.patch.object(sf, 'now', lambda: self.now); clock.start(); self.addCleanup(clock.stop)
        self.source = demo.Source(rows=demo.items(scanned_at=SCAN))
        self.assignment = demo.assignment()                                 # shifts 1, 2 and 3 are configured; these tests follow shift 1
        self.scope = scope_key(dict(demo.SCOPE, client_id=demo.CLIENT, shift='1', zone='America/Chicago'))
        self.open()

    def open(self):
        """A new process: new store object, new router, new worker."""
        self.store = StateStore(self.path)
        self.router = StoredDraftRouter(BlobArtifacts(self.store), [self.assignment])
        self.publisher = self.router.for_scope(self.assignment)
        fetch = lambda scope, settings: source_adapter(scope, settings, capture_fn=lambda s: self.source.capture(s))
        self.worker = ScheduledWorker(self.store, fetch, self.publisher, clock=lambda: self.now, enabled=True)

    def tick(self, at, status=None, body=None):
        self.now = at
        if status is not None:
            self.source.photo = lambda url, s=status, b=body or b'': (s, b)
        return self.worker.tick(self.scope, self.assignment)

    def stored(self, op):
        return {n: self.publisher.read(op, n) for n in ('draft.xlsx', 'checklist.json', 'checklist.txt')}

    def test_403_then_404_then_500_reuse_one_artifact_labelled_historical(self):
        first = self.tick(T1, 403)
        op = first['operation_id']
        self.assertEqual((first['status'], first['artifact_state'], first['evidence_as_of'], first['observed_at']),
                         ('draft_ready', 'built_this_run', T1, T1))
        frozen = self.stored(op)
        checklist = json.loads(frozen['checklist.json'])
        self.assertEqual((checklist['evidence_as_of'], checklist['format']), (T1, 'draft-checklist-v3'))
        self.assertIn('HISTORICAL RECORD', checklist['historical_notice']); self.assertIn(T1, checklist['historical_notice'])
        self.assertIn('HISTORICAL RECORD', frozen['checklist.txt'].decode())
        self.assertEqual({p['http_status'] for p in checklist['photo_evidence']}, {403})

        for at, status in ((T2, 404), (T3, 500)):
            again = self.tick(at, status)
            self.assertEqual((again['status'], again['operation_id'], again['artifact_state'], again['evidence_as_of'], again['observed_at']),
                             ('draft_ready', op, 'historical_reused', T1, at))
            self.assertEqual(self.stored(op), frozen)                       # nothing was rewritten
            p = self.worker.provenance(self.scope, op)
            self.assertEqual((p['label'], p['freshly_verified'], p['regenerated'], p['relation'], p['photo_comparison']),
                             ('HISTORICAL', False, False, 'same_content_reused', 'compared'))
            self.assertEqual((p['artifact']['evidence_as_of'], p['latest_observation']['captured_at'], p['generated_at']), (T1, at, at))
            differences = {d['record_id']: d for d in p['photo_evidence_differences']}
            self.assertEqual(sorted(differences), ['3', '8'])               # the two photo rows of shift 1
            for d in differences.values():
                self.assertEqual((d['in_this_draft']['http_status'], d['latest_observation']['http_status']), (403, status))
                self.assertEqual((d['in_this_draft']['status'], d['latest_observation']['status']), ('unavailable', 'unavailable'))
        with self.store.transaction('outbox:' + sf.digest(self.scope)) as box:
            self.assertEqual(list(box['entries']), [op])                    # still one draft, one entry

    def test_reuse_is_never_described_as_new_and_export_returns_the_original_bytes(self):
        op = self.tick(T1, 403)['operation_id']; frozen = self.stored(op)
        reused = self.tick(T2, 404)
        export = self.publisher.export(op)
        self.assertEqual(export['files'], frozen)
        self.assertEqual((export['provenance']['label'], export['provenance']['evidence_as_of'], export['provenance']['freshly_verified'],
                          export['provenance']['regenerated'], export['provenance']['client_release']), ('HISTORICAL', T1, False, False, 'held'))
        self.assertEqual(sf.digest(export['files']['draft.xlsx']), export['provenance']['workbook_sha256'])
        provenance = self.worker.provenance(self.scope, op)
        for text in (json.dumps({k: v for k, v in reused.items() if k != 'receipt'}), json.dumps(export['provenance']),
                     json.dumps({k: provenance[k] for k in ('label', 'relation', 'artifact')})):
            lowered = text.lower().replace('freshly_verified', '').replace('"regenerated": false', '')
            for word in FORBIDDEN_WORDS:
                self.assertNotIn(word, lowered, (word, text))

    def test_restart_keeps_the_same_history_and_bytes(self):
        op = self.tick(T1, 403)['operation_id']; frozen = self.stored(op)
        self.tick(T2, 404)
        before = self.worker.provenance(self.scope, op)
        self.open()                                                         # restart
        self.assertEqual(self.stored(op), frozen)
        self.assertEqual(self.publisher.export(op)['provenance']['evidence_as_of'], T1)
        after = self.worker.provenance(self.scope, op)
        self.assertEqual(after, before)
        again = self.tick(T3, 500)
        self.assertEqual((again['artifact_state'], again['evidence_as_of'], again['observed_at']), ('historical_reused', T1, T3))
        self.assertEqual(self.stored(op), frozen)

    def test_a_later_successful_photo_is_a_new_operation_and_the_old_draft_stays_as_it_was(self):
        from PIL import Image
        out = io.BytesIO(); Image.new('RGB', (4, 3), (200, 30, 30)).save(out, format='PNG'); image = out.getvalue()
        old = self.tick(T1, 403)['operation_id']; frozen = self.stored(old)
        self.tick(T2, 404); self.tick(T3, 500)
        changed = self.tick(T4, 200, image)                                 # the photo can now be downloaded
        self.assertEqual(changed['status'], 'waiting'); self.assertNotEqual(changed['operation_id'], old)
        self.assertTrue(changed['due_at'].startswith('2026-11-10T23:05'))   # new evidence: a full hour again
        p = self.worker.provenance(self.scope, old)
        self.assertEqual((p['label'], p['relation'], p['artifact']['evidence_as_of'], p['latest_observation']['captured_at'],
                          p['latest_observation']['operation_id']), ('HISTORICAL', 'superseded_by_newer_operation', T1, T4, changed['operation_id']))
        self.assertTrue(all(d['latest_observation']['status'] == 'supported' and d['in_this_draft']['status'] == 'unavailable'
                            for d in p['photo_evidence_differences']))
        self.assertEqual(self.stored(old), frozen)
        with self.store.transaction('outbox:' + sf.digest(self.scope)) as box:
            self.assertEqual(box['entries'][old]['status'], 'superseded')
        self.assertEqual(self.tick(T5, 200, image)['status'], 'waiting')
        new = self.tick(T6, 200, image)
        self.assertEqual((new['status'], new['artifact_state'], new['evidence_as_of']), ('draft_ready', 'built_this_run', T6))
        fresh = json.loads(self.publisher.read(new['operation_id'], 'checklist.json'))
        self.assertEqual(({p['status'] for p in fresh['photo_evidence']}, fresh['evidence_as_of']), ({'supported'}, T6))
        self.assertEqual(self.stored(old), frozen)                          # the first draft is still downloadable, unchanged
        self.assertEqual(self.publisher.export(old)['provenance']['evidence_as_of'], T1)
        self.assertEqual(self.worker.provenance(self.scope, new['operation_id'])['photo_evidence_differences'], [])

    def test_provenance_reads_only_and_handles_unknown_operations(self):
        op = self.tick(T1, 403)['operation_id']
        with sqlite3.connect(self.path) as db:
            before = db.execute('SELECT key, value FROM scan_draft_state ORDER BY key').fetchall()
            blobs = db.execute('SELECT count(*) FROM scan_draft_blob').fetchone()[0]
        requests = len(self.source.requests)
        self.worker.provenance(self.scope, op); self.publisher.export(op)
        self.assertEqual(self.worker.provenance(self.scope, 'no-such-operation'), dict(status='no_such_draft', operation_id='no-such-operation'))
        with self.assertRaises(LookupError):
            self.publisher.export('no-such-operation')
        with sqlite3.connect(self.path) as db:
            self.assertEqual(db.execute('SELECT key, value FROM scan_draft_state ORDER BY key').fetchall(), before)
            self.assertEqual(db.execute('SELECT count(*) FROM scan_draft_blob').fetchone()[0], blobs)
        self.assertEqual(len(self.source.requests), requests)               # no source read either

    def test_runner_receipt_carries_the_same_distinction(self):
        config = dict(enabled=True, assignments=[self.assignment])
        fetch = lambda scope, settings: source_adapter(scope, settings, capture_fn=lambda s: self.source.capture(s))
        go = lambda rid: [r for r in runner.run_once(self.store, config, fetch=fetch, render=self.router, run_id=rid, entries=demo.REGISTRY,
                                                     clock=lambda: self.now)['results'] if r['shift'] == '1'][0]
        self.source.photo = lambda url: (403, b'')
        first = go('r1')
        self.now = T2; self.source.photo = lambda url: (404, b'')
        second = go('r2')
        self.assertEqual((first['artifact_state'], first['evidence_as_of'], first['observed_at']), ('built_this_run', T1, T1))
        self.assertEqual((second['artifact_state'], second['evidence_as_of'], second['observed_at'], second['operation_id']),
                         ('historical_reused', T1, T2, first['operation_id']))

    def old_version(self):
        """Build a draft as an OLDER renderer would have (format v2, different renderer code)."""
        import contextlib
        from hosting.last_scan_local import NativeDraftPublisher
        stack = contextlib.ExitStack()
        stack.enter_context(mock.patch.object(NativeDraftPublisher, 'FORMAT', 'draft-checklist-v2'))
        stack.enter_context(mock.patch.object(NativeDraftPublisher, 'renderer_hash', staticmethod(lambda: 'older-renderer-code')))
        return stack

    def test_old_cached_v2_draft_is_never_reused_as_the_newer_warned_version(self):
        rows = demo.items(scanned_at=SCAN)
        for i in (30, 31, 32):
            rows[i]['fields'][demo.MAP['fields']['shift']] = 'Night'        # rows in no configured shift
        self.source.rows = rows
        with self.old_version():
            old = self.tick(T1, 403)
        op_old = old['operation_id']; frozen = self.stored(op_old)
        self.assertEqual(json.loads(frozen['checklist.json'])['format'], 'draft-checklist-v2')
        self.open()                                                         # the newer code starts
        new = self.tick(T2, 403)                                            # same source, nothing changed there
        self.assertNotEqual(new['operation_id'], op_old)                    # NOT a reuse of the old draft
        self.assertEqual((new['status'], new['artifact_state'], new['evidence_as_of'], new['unassigned_shifts']),
                         ('draft_ready', 'built_this_run', T2, {'Night': 3}))
        new_checklist = json.loads(self.publisher.read(new['operation_id'], 'checklist.json'))
        self.assertEqual((new_checklist['format'], new_checklist['unassigned_shifts']), ('draft-checklist-v3', {'Night': 3}))
        self.assertIn('3 row(s) with shift "Night"', self.publisher.read(new['operation_id'], 'checklist.txt').decode())
        # the old draft: untouched, still readable, and described by ITS OWN version and capture time
        self.assertEqual(self.stored(op_old), frozen)
        p = self.worker.provenance(self.scope, op_old)
        self.assertEqual((p['label'], p['relation'], p['artifact']['format'], p['artifact']['evidence_as_of'], p['latest_observation']['operation_id']),
                         ('HISTORICAL', 'superseded_by_newer_operation', 'draft-checklist-v2', T1, new['operation_id']))
        self.assertNotEqual(p['artifact']['render_version'], self.worker.provenance(self.scope, new['operation_id'])['artifact']['render_version'])
        export = self.publisher.export(op_old)['provenance']
        self.assertEqual((export['format'], export['evidence_as_of'], export['label']), ('draft-checklist-v2', T1, 'HISTORICAL'))
        with self.store.transaction('outbox:' + sf.digest(self.scope)) as box:
            self.assertEqual({k: v['status'] for k, v in box['entries'].items()}, {op_old: 'superseded', new['operation_id']: 'pending'})
        with self.assertRaises(ValueError):                                 # asked to render under the old ID, the newer code refuses
            self.publisher(dict(scope=self.scope), op_old)
        again = self.tick(T3, 403)
        self.assertEqual((again['operation_id'], again['artifact_state'], again['evidence_as_of']), (new['operation_id'], 'historical_reused', T2))

    def test_renderer_code_and_format_are_both_part_of_the_render_version(self):
        import hosting.last_scan_local as local
        from hosting.last_scan_local import NativeDraftPublisher
        base = self.publisher.render_version()
        with mock.patch.object(NativeDraftPublisher, 'FORMAT', 'draft-checklist-v4'):
            self.assertNotEqual(self.publisher.render_version(), base)
        with mock.patch.object(NativeDraftPublisher, 'renderer_hash', staticmethod(lambda: 'edited')):
            self.assertNotEqual(self.publisher.render_version(), base)
        self.assertEqual(self.publisher.render_version(), base)
        self.assertEqual(NativeDraftPublisher.FORMAT, 'draft-checklist-v3')
        self.assertEqual(NativeDraftPublisher.renderer_hash(), sf.digest(Path(local.__file__).read_bytes()))

    def test_a_newly_appearing_unassigned_row_is_a_new_operation_with_the_warning(self):
        first = self.tick(T1, 403)
        self.assertNotIn('unassigned_shifts', first)
        clean = json.loads(self.publisher.read(first['operation_id'], 'checklist.json'))
        self.assertEqual(clean['unassigned_shifts'], {})
        self.source.rows[30]['fields'][demo.MAP['fields']['shift']] = 'Night'   # a row of another shift gets an unknown label
        changed = self.tick(T2, 403)
        self.assertNotEqual(changed['operation_id'], first['operation_id'])     # shift 1's own rows did not change, the warning did
        self.assertEqual((changed['status'], changed['unassigned_shifts']), ('waiting', {'Night': 1}))
        self.assertEqual(json.loads(self.publisher.read(first['operation_id'], 'checklist.json')), clean)   # old draft not rewritten

    def test_changed_checklist_format_is_a_new_operation_not_a_rewrite(self):
        op = self.tick(T1, 403)['operation_id']; frozen = self.stored(op)
        with mock.patch.object(type(self.publisher), 'FORMAT', 'draft-checklist-v4'):
            later = self.tick(T2, 403)
            self.assertNotEqual(later['operation_id'], op)
            self.assertEqual(later['artifact_state'], 'built_this_run')
        self.assertEqual(self.stored(op), frozen)


if __name__ == '__main__':
    unittest.main()

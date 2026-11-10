"""Synthetic ancestry, transaction guard and access regressions. No providers."""
import copy
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(__file__))
from test_version_reuse import RAW, NAME, save, change
import test_version_reuse as reuse
from hosting.spreadsheet_inbox import SpreadsheetInbox
from hosting.working_copy import WorkingCopies


def parallel(box, parent, before, after):
    # Seed a branch produced by the old release, before stale-parent protection.
    class OldRelease:
        def resolve(self, key):
            return {'status': 'current'}
    with patch.object(box, '_working_copies', return_value=OldRelease()):
        return save(box, parent, before, after)['file_hash']

class WorkingCopyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.box = SpreadsheetInbox(self.temp.name)
        self.a = self.box.ingest(NAME, RAW)['file_hash']
        self.b = save(self.box, self.a, 'original', 'second')['file_hash']
        self.c = save(self.box, self.b, 'second', 'third')['file_hash']

    def test_full_chain_and_exact_download(self):
        for node in (self.a, self.b, self.c):
            result = self.box.working_copy(node)
            self.assertEqual(result['working_hash'], self.c)
            self.assertEqual(result['status'], 'current' if node == self.c else 'superseded')
        self.assertEqual([r['file_hash'] for r in self.box.working_copy(self.c)['history']], [self.b, self.a])
        self.assertEqual(self.box.download(self.a)[1], RAW)
        rows = self.box.list(working=True)['spreadsheets']
        self.assertEqual([r['file_hash'] for r in rows], [self.c])

    def test_parallel_branches_need_choice_and_remain_separate(self):
        d = parallel(self.box, self.a, 'original', 'parallel')
        result = self.box.working_copy(self.a)
        self.assertEqual(result['status'], 'choice'); self.assertIsNone(result['working_hash'])
        self.assertEqual({r['file_hash'] for r in result['choices']}, {self.c, d})
        self.assertEqual({r['file_hash'] for r in self.box.list(working=True)['spreadsheets']}, {self.c, d})
        self.assertEqual(self.box.working_copy(self.b)['working_hash'], self.c)

    def test_revoked_destination_and_parent_do_not_leak(self):
        for denied in (self.a, self.b, self.c):
            result = self.box.working_copy(self.a, lambda item: item['file_hash'] != denied)
            self.assertEqual(result['status'], 'unavailable')
            self.assertEqual(result['choices'], []); self.assertEqual(result['history'], [])
            self.assertIsNone(result['working_hash'])
        d = parallel(self.box, self.a, 'original', 'parallel')
        result = self.box.working_copy(self.a, lambda item: item['file_hash'] != d)
        self.assertEqual(result['status'], 'unavailable'); self.assertEqual(result['choices'], [])

    def test_stale_parent_cannot_create_new_child_but_exact_reuse_is_truthful(self):
        before = self.box.list()['count']
        with self.assertRaisesRegex(ValueError, 'superseded'):
            save(self.box, self.a, 'original', 'different')
        self.assertEqual(self.box.list()['count'], before)
        existing = save(self.box, self.a, 'original', 'third')
        self.assertEqual(existing['file_hash'], self.c); self.assertTrue(existing['already_applied'])
        self.assertEqual(save(self.box, self.c, 'third', 'original')['file_hash'], self.a)

    def test_child_arrives_after_preparation_before_transaction(self):
        # _check_changes first runs outside the transaction, then runs locked.
        box = self.box; original = box._check_changes; called = False
        def race(*args, **kwargs):
            nonlocal called
            out = original(*args, **kwargs)
            if not called:
                called = True
                save(box, self.c, 'third', 'concurrent')
            return out
        reviewed = box.get(self.c)['check_hash']
        with patch.object(box, '_check_changes', side_effect=race):
            with self.assertRaisesRegex(ValueError, 'changed|superseded'):
                box.apply_fixes(self.c, [change('third', 'my draft')], expected_check_hash=reviewed)
        self.assertEqual(box.list()['count'], 4)
        self.assertNotEqual(box.working_copy(self.c)['working_hash'], self.c)

    def test_complete_inventory_is_not_the_bounded_list(self):
        # Old timestamps and root hints cannot make an ancestor the working copy.
        self.box._update(self.c, lambda r: r.update(received_at='1900-01-01', root_hash='f'*64))
        self.box._update(self.a, lambda r: r.update(received_at='2099-01-01'))
        self.assertEqual(self.box.working_copy(self.a)['working_hash'], self.c)
        self.assertEqual(self.box.list(working=True)['count'], 1)

    def test_missing_cyclic_conflicting_or_partial_history_fails_closed(self):
        with self.box.connect() as db:
            graph = self.box._working_copies(db)
        bases = graph.items
        with self.box.connect() as db:
            histories = {}
            for row in db.execute('SELECT file_hash,result_json FROM spreadsheet_checks ORDER BY seq ASC'):
                histories.setdefault(row['file_hash'], []).append(json.loads(row['result_json']))
        scenarios = []
        missing = copy.deepcopy(histories); missing.pop(self.b); scenarios.append((bases, missing))
        conflict = copy.deepcopy(histories); conflict[self.c].append(dict(conflict[self.c][-1], fixed_from={'file_hash': self.a})); scenarios.append((bases, conflict))
        cycle = copy.deepcopy(histories); cycle[self.a].append(dict(cycle[self.a][-1], fixed_from={'file_hash': self.c})); scenarios.append((bases, cycle))
        partial = dict(bases); partial.pop(self.c); scenarios.append((partial, histories))
        for inventory, checks in scenarios:
            with self.subTest():
                self.assertEqual(WorkingCopies(inventory, checks).resolve(self.a)['status'], 'unavailable')

    def test_more_than_one_hundred_uploads_do_not_hide_current_descendant(self):
        # These independent copies push the old family outside the bounded list.
        for i in range(101):
            self.box.ingest('unrelated.csv', RAW.replace(b'original', ('unrelated %d' % i).encode()))
        self.assertTrue(self.box.list()['has_more'])
        self.assertNotIn(self.c, [r['file_hash'] for r in self.box.list()['spreadsheets']])
        self.assertEqual(self.box.working_copy(self.a)['working_hash'], self.c)
        working = self.box.list(working=True)
        self.assertEqual(working['count'], 102)
        self.assertTrue(working['has_more'])

    def test_legacy_self_cycle_explains_history_without_repairing_it(self):
        self.box._update(self.c, lambda r: r.update(fixed_from=dict(r['fixed_from'], file_hash=self.c), fixes_applied=[dict(fixed_file_hash=self.c)]))
        original = self.box.get(self.c)
        raw = self.box.download(self.c)[1]
        result = self.box.working_copy(self.c)
        self.assertEqual(result['status'], 'unavailable')
        self.assertEqual(result['unavailable_reason'], 'history_inconsistent')
        with self.assertRaisesRegex(ValueError, 'inconsistent saved version history'):
            self.box.proposal_inputs(self.c, original['check_hash'])
        self.assertEqual(self.box.get(self.c), original)
        self.assertEqual(self.box.download(self.c)[1], raw)
        self.assertNotIn('unavailable_reason', self.box.working_copy(self.c, lambda item: False))

    def test_approval_and_send_reasons_do_not_change_state(self):
        for marker, reason in (({'approval': {'by': 'person'}}, 'approved'), ({'sent': [{'sent': True}]}, 'sent'), ({'send_pending': True}, 'sending')):
            result = self.box.get(self.c); result.update(marker)
            with self.box.connect() as db:
                graph = self.box._working_copies(db)
            graph.items[self.c] = result
            self.assertEqual(graph.resolve(self.a)['read_only_reason'], reason)

class WorkingCopyHTTPTests(unittest.TestCase):
    setUp = reuse.VersionReuseHTTPTests.setUp
    tearDown = reuse.VersionReuseHTTPTests.tearDown
    request = reuse.VersionReuseHTTPTests.request
    def test_current_resolution_and_exact_history(self):
        status, payload = self.request('/api/spreadsheets/working-copy?id=' + self.a)
        self.assertEqual(status, 200); self.assertEqual(payload['working_hash'], self.c)
        status, payload = self.request('/api/spreadsheet?id=' + self.a)
        self.assertEqual(status, 200); self.assertEqual(payload['file_hash'], self.a)
        status, payload = self.request('/api/spreadsheets')
        self.assertEqual(status, 200); self.assertTrue(payload['working_copies'])
        self.assertEqual([r['file_hash'] for r in payload['spreadsheets']], [self.c])

    def test_access_revoked_during_resolution_is_generic(self):
        from api.index import handler
        original = handler.can_stored_report
        def revoked(handler, inbox, item):
            return item['file_hash'] != self.c and original(handler, inbox, item)
        with patch.object(handler, 'can_stored_report', revoked):
            status, payload = self.request('/api/spreadsheets/working-copy?id=' + self.a)
        self.assertEqual(status, 200)
        self.assertEqual(payload['status'], 'unavailable')
        self.assertEqual(payload['choices'], [])
        self.assertEqual(payload['history'], [])
        self.assertNotIn(self.c, json.dumps(payload))

if __name__ == '__main__': unittest.main()

"""Synthetic route snippets only. No server, provider, credentials or persisted data."""
import ast
import json
from pathlib import Path
import re
import unittest
from unittest.mock import Mock, patch
from hosting import delivery


class Conflict(Exception):
    pass


def approval_route():
    tree = ast.parse(Path('api/index.py').read_text())
    node = next(n for n in ast.walk(tree) if isinstance(n, ast.If)
                and isinstance(n.test, ast.Compare)
                and ast.unparse(n.test) == "route in ('/api/spreadsheets/approve', '/api/spreadsheets/send-info', '/api/spreadsheets/send')")
    fn = ast.parse('def run(self, route, raw):\n    pass').body[0]
    fn.body = node.body
    module = ast.fix_missing_locations(ast.Module(body=[fn], type_ignores=[]))
    scope = dict(json=json, re=re, Conflict=Conflict, encode_json=lambda x: x)
    exec(compile(module, 'synthetic-route', 'exec'), scope)
    return scope['run']


class RequestGuards(unittest.TestCase):
    def test_all_delivery_routes_check_access_before_side_effect(self):
        run = approval_route()
        handler = Mock()
        handler.can_hash.return_value = False
        with patch.object(delivery, 'approve') as approve, patch.object(delivery, 'send') as send, patch.object(delivery, 'info') as info:
            for route in ('approve', 'send-info', 'send'):
                run(handler, '/api/spreadsheets/' + route, json.dumps({'file_hash': 'a' * 64}))
            approve.assert_not_called(); send.assert_not_called(); info.assert_not_called()
        self.assertEqual(handler.fail.call_count, 3)
        handler.store.assert_not_called()

    def test_api_passes_binding_without_inventing_defaults(self):
        run = approval_route(); handler = Mock(); handler.can_hash.return_value = True
        with patch.object(delivery, 'approve', return_value={}) as approve:
            for binding in ({}, {'expected_check_hash': 'old-check', 'expected_run': {'checker': 'old'}}):
                run(handler, '/api/spreadsheets/approve', json.dumps({'file_hash': 'a' * 64, 'withdraw': True, **binding}))
                self.assertEqual(approve.call_args.kwargs, {'expected_check_hash': binding.get('expected_check_hash'), 'expected_run': binding.get('expected_run')})

    def test_delivery_wrapper_preserves_binding(self):
        store = Mock(); store.session.return_value.__enter__ = Mock(return_value='synthetic')
        store.session.return_value.__exit__ = Mock(return_value=False)
        with patch.object(delivery, 'SpreadsheetInbox') as inbox:
            delivery.approve(store, 'a' * 64, 'Reviewer', expected_check_hash='seen', expected_run={'checker': 'seen'})
            self.assertEqual(inbox.return_value.approve.call_args.kwargs,
                             {'expected_check_hash': 'seen', 'expected_run': {'checker': 'seen'}})

    def test_send_and_draft_fail_before_provider_or_storage(self):
        with patch.object(delivery.mailer, 'deliver') as provider, patch.object(delivery, '_load') as load:
            for mode in ('send', 'draft'):
                with self.assertRaisesRegex(ValueError, 'paused'):
                    delivery.send(Mock(), 'a' * 64, {'mode': mode, 'again': True})
            provider.assert_not_called(); load.assert_not_called()

    def test_decision_api_forwards_all_guard_fields(self):
        tree = ast.parse(Path('api/index.py').read_text())
        calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == 'decide_findings']
        self.assertTrue(calls)
        for call in calls:
            args = {k.arg: ast.unparse(k.value) for k in call.keywords}
            for key in ('expected_rev', 'expected_fps', 'expected_run'):
                self.assertIn(args[key], (f"payload.get('{key}')", f"payload.get('{key}', " + ('seen)' if key == 'expected_fps' else 'run)')))
            self.assertEqual(args['expected_rev'], "payload.get('expected_rev')")


if __name__ == '__main__':
    unittest.main()

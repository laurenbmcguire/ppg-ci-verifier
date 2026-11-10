"""Synthetic exact-byte hosting ceilings and truthful durable held receipts."""
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).parent))
from test_source_first_daily import Durable as _Durable
from source_first_fixture import MAP
from hosting import source_first as sf, source_first_http as http, source_first_service as svc
from hosting import source_first_workbook as wb, source_first_layout as lx
from hosting.spreadsheet_inbox import SpreadsheetInbox
from hosting.files import files_for


class HostedLimits(unittest.TestCase):
    setUp = _Durable.setUp
    tearDown = _Durable.tearDown
    rid = _Durable.rid
    new = _Durable.new
    action = _Durable.action
    approved = _Durable.approved

    def test_json_ceiling_uses_exact_serialized_bytes(self):
        self.assertEqual(sf.MAX_RESPONSE_BYTES, 3 * 1024 * 1024)
        self.assertEqual(lx.MAX_WORKBOOK_BYTES, sf.MAX_RESPONSE_BYTES)
        value = {'value': 'x' * (sf.MAX_RESPONSE_BYTES - len(json.dumps({'value': ''}).encode()))}
        self.assertEqual(len(http.response_json(value)), sf.MAX_RESPONSE_BYTES)
        value['value'] += 'x'
        with self.assertRaisesRegex(sf.ReviewError, 'may already be saved'):
            http.response_json(value)
        with self.assertRaises(sf.ReviewError):
            http.response_json({'value': '中' * (sf.MAX_RESPONSE_BYTES // 6)})

    def test_mapping_save_returns_truthful_compact_receipt_and_keeps_full_source(self):
        state = self.new()
        with patch.object(sf, 'MAX_RESPONSE_BYTES', 2000):
            out = self.action(state, 'mapping', mapping=MAP)
        self.assertTrue(out['view_limited'])
        self.assertFalse(out['approved'])
        self.assertEqual(out['release'], 'held')
        self.assertNotIn('rows', out)
        self.assertEqual(out['receipt']['last_operation']['action'], 'mapping')
        self.assertEqual(out['receipt']['last_operation']['status'], 'saved')
        self.assertLess(len(json.dumps(out).encode()), 2000)
        persisted = svc.view(self.h, out['id'])
        self.assertEqual(len(persisted['rows']), 2)
        self.assertEqual(persisted['snapshot_hash'], state['snapshot_hash'])
        self.assertEqual(persisted['mapping']['fields'], MAP['fields'])

    def test_multibyte_rows_can_exceed_host_limit_without_many_source_bytes(self):
        from test_source_first_expansion import fixture
        capture, _, _ = fixture(750)
        for i, row in enumerate(capture['records']):
            for field in ('Comments', 'Location', 'InspectedFor', 'Condition', 'RejectReason'):
                row['raw'][field] = '中' * 48
            row['raw']['Identifier'] = ('ID' + str(i)).ljust(96, 'x')
        capture = sf.seal({k: v for k, v in capture.items() if k not in ('snapshot_hash', 'envelope_hash')})
        rows, _ = sf.reviewed_rows(capture, MAP, [])
        self.assertGreater(len(json.dumps({'rows': rows}, allow_nan=False).encode()), 4.5 * 1024 * 1024)
        with self.assertRaises(sf.ReviewError):
            http.response_json({'rows': rows})

    def test_approval_cannot_skip_undisplayable_review(self):
        state = self.new()
        state = self.action(state, 'mapping', mapping=MAP)
        state = self.action(state, 'header', header={})
        with patch.object(sf, 'MAX_RESPONSE_BYTES', 2000):
            with self.assertRaisesRegex(sf.ReviewError, 'complete review'):
                self.action(state, 'approve', reviewed=True)
        out = svc.view(self.h, state['id'])
        self.assertFalse(out['approved'])
        self.assertFalse(any(o['action'] == 'approve' for o in out['operations']))

    def test_generation_cannot_skip_undisplayable_review(self):
        state = self.approved()
        with patch.object(sf, 'MAX_RESPONSE_BYTES', 2000), patch.object(wb, 'build') as build:
            with self.assertRaisesRegex(sf.ReviewError, 'complete review'):
                svc.generate(self.h, state['id'], dict(request_id=self.rid(), binding_hash=state['binding_hash']))
            build.assert_not_called()
        self.assertIsNone(svc.view(self.h, state['id'])['report'])

    def test_prospective_approval_receipt_must_fit_before_save(self):
        state = self.new()
        state = self.action(state, 'mapping', mapping=MAP)
        state = self.action(state, 'header', header={})
        limit = len(json.dumps(state, allow_nan=False).encode()) + 10
        with patch.object(sf, 'MAX_RESPONSE_BYTES', limit):
            with self.assertRaisesRegex(sf.ReviewError, 'complete review'):
                self.action(state, 'approve', reviewed=True)
        out = svc.view(self.h, state['id'])
        self.assertFalse(out['approved'])
        self.assertFalse(any(o['action'] == 'approve' for o in out['operations']))

    def test_oversized_workbook_is_not_saved_even_if_builder_claims_verified(self):
        state = self.approved()
        with patch.object(wb, 'build', return_value=(b'x' * (sf.MAX_RESPONSE_BYTES + 1), {'matches_reviewed_snapshot': True})):
            with self.assertRaisesRegex(sf.ReviewError, 'No new report artifact was saved'):
                svc.generate(self.h, state['id'], dict(request_id=self.rid(), binding_hash=state['binding_hash']))
        out = svc.view(self.h, state['id'])
        self.assertIsNone(out['report'])
        self.assertFalse(any(o['action'] == 'generate' for o in out['operations']))

    def test_generated_verification_receipt_must_fit_before_save(self):
        state = self.approved()
        with patch.object(wb, 'build', return_value=(b'synthetic', {'matches_reviewed_snapshot': True, 'oversized_receipt': 'x' * sf.MAX_RESPONSE_BYTES})):
            with self.assertRaisesRegex(sf.ReviewError, 'complete review'):
                svc.generate(self.h, state['id'], dict(request_id=self.rid(), binding_hash=state['binding_hash']))
        self.assertIsNone(svc.view(self.h, state['id'])['report'])

    def test_legacy_oversized_download_fails_without_transmitting_bytes(self):
        state = self.approved()
        raw = b'x' * (sf.MAX_RESPONSE_BYTES + 1)
        sha = sf.digest(raw)
        with self.store.session() as directory:
            inbox = SpreadsheetInbox(directory)
            with inbox.connect() as db:
                saved = svc._get(db, state['id'])
                saved['report'] = dict(file_hash=sha, artifact_path='uploads/synthetic-large.xlsx', filename='test.xlsx')
                svc._put(db, state['id'], saved)
            files_for(directory).put('uploads/synthetic-large.xlsx', raw)
        self.h.path = '/api/source-review/download?id=' + state['id'] + '&sha256=' + sha
        self.h.fail = lambda code, text: (code, text)
        self.h.send = lambda *args: self.fail('unexpected', 'sent binary')
        status, text = http.handle(self.h, 'GET', '/api/source-review/download')
        self.assertEqual(status, 409)
        self.assertIn('saved bytes are unchanged', text)

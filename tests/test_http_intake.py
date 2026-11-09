"""End-to-end HTTP tests use an isolated temporary synthetic server."""
import copy
import json
import secrets
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import HTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'app'))
from server import Handler
from model import Review
from report_intake import ReportInbox


class IntakeHTTPTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.server = HTTPServer(('127.0.0.1', 0), Handler)
        self.server.reviews = {'synthetic': Review(self.temp.name)}
        self.server.inbox = ReportInbox(Path(self.temp.name) / 'intake')
        self.server.token = secrets.token_urlsafe(32)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = 'http://127.0.0.1:' + str(self.server.server_port)
        self.sample = json.loads((ROOT / 'samples/matched-report.json').read_text())

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.temp.cleanup()

    def request(self, path, body=None, token=True, headers=None):
        h = dict(headers or {})
        if token:
            h['X-Review-Token'] = self.server.token
        if body is not None:
            h['Content-Type'] = 'application/json'
            if not isinstance(body, bytes):
                body = json.dumps(body).encode()
        req = urllib.request.Request(self.base + path, data=body, headers=h)
        try:
            with urllib.request.urlopen(req, timeout=5) as response:
                return response.status, response.read(), dict(response.headers)
        except urllib.error.HTTPError as error:
            return error.code, error.read(), dict(error.headers)

    def test_empty_inbox_has_no_send_capability(self):
        code, raw, headers = self.request('/api/reports')
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(raw)['reports'], [])
        self.assertFalse(json.loads(raw)['send_enabled'])
        self.assertEqual(headers['Cache-Control'], 'no-store')

    def test_match_import_and_text_preview(self):
        code, raw, _ = self.request('/api/reports/import', self.sample)
        result = json.loads(raw)
        self.assertEqual(code, 200)
        self.assertEqual(result['item']['status'], 'matched_supplied_snapshot')
        self.assertEqual(result['item']['release_status'], 'held')
        self.assertEqual(result['state']['outbox'][0]['status'], 'preview_only')
        self.assertIsNone(result['state']['outbox'][0]['recipient'])

    def test_reimport_does_not_duplicate(self):
        self.request('/api/reports/import', self.sample)
        code, raw, _ = self.request('/api/reports/import', self.sample)
        result = json.loads(raw)
        self.assertEqual(code, 200)
        self.assertTrue(result['item']['idempotent'])
        self.assertEqual(len(result['state']['reports']), 1)
        self.assertEqual(len(result['state']['outbox']), 1)

    def test_conflict_preserves_original(self):
        self.request('/api/reports/import', self.sample)
        changed = copy.deepcopy(self.sample)
        changed['report']['totals']['inspected'] += 1
        self.assertEqual(self.request('/api/reports/import', changed)[0], 409)
        self.assertEqual(self.server.inbox.get(self.sample['report_id'], self.sample['version'])['status'], 'matched_supplied_snapshot')

    def test_import_requires_local_session_token(self):
        code, raw, headers = self.request('/api/reports/import', self.sample, token=False)
        self.assertEqual(code, 403)
        self.assertEqual(json.loads(raw), {'error': 'Local session token required'})
        self.assertEqual(int(headers['Content-Length']), len(raw))
        self.assertEqual(headers['Connection'], 'close')
        self.assertEqual(self.server.inbox.list_reports(), [])

    def test_cross_origin_import_rejected(self):
        code, raw, headers = self.request('/api/reports/import', self.sample, headers={'Origin': 'https://example.com'})
        self.assertEqual(code, 403)
        self.assertEqual(json.loads(raw), {})
        self.assertEqual(int(headers['Content-Length']), len(raw))
        self.assertEqual(headers['Connection'], 'close')
        self.assertEqual(self.server.inbox.list_reports(), [])

    def test_rejected_post_is_framed_before_the_request_body_is_sent(self):
        import socket
        # Do not send the declared body: authorization must reject immediately,
        # without parsing or waiting for potentially hostile request bytes.
        for token, origin in ((False, self.base), (True, 'https://example.invalid')):
            with self.subTest(token=token, origin=origin):
                connection = socket.create_connection(('127.0.0.1', self.server.server_port), timeout=2)
                try:
                    request = f'POST /api/reports/import HTTP/1.1\r\nHost: 127.0.0.1:{self.server.server_port}\r\nOrigin: {origin}\r\nContent-Length: 1048576\r\n'
                    if token:
                        request += f'X-Review-Token: {self.server.token}\r\n'
                    connection.sendall((request + '\r\n').encode())
                    response = connection.makefile('rb')
                    try:
                        self.assertIn(b' 403 ', response.readline())
                        headers = {}
                        while line := response.readline():
                            if line == b'\r\n':
                                break
                            key, value = line.decode().split(':', 1)
                            headers[key.lower()] = value.strip()
                        self.assertEqual(headers['connection'], 'close')
                        length = int(headers['content-length'])
                        body = response.read(length)
                        self.assertEqual(len(body), length)
                        self.assertIsInstance(json.loads(body), dict)
                    finally:
                        response.close()
                finally:
                    connection.close()
                self.assertEqual(self.server.inbox.list_reports(), [])

    def test_foreign_host_rejected(self):
        self.assertEqual(self.request('/api/reports', headers={'Host': 'untrusted.example'})[0], 403)

    def test_invalid_json_rejected_without_write(self):
        self.assertEqual(self.request('/api/reports/import', b'{broken')[0], 409)
        self.assertEqual(self.server.inbox.list_reports(), [])

    def test_duplicate_json_keys_rejected(self):
        raw = json.dumps(self.sample).encode()
        raw = raw[:-1] + b', "synthetic": true}'
        self.assertEqual(self.request('/api/reports/import', raw)[0], 409)
        self.assertEqual(self.server.inbox.list_reports(), [])

    def test_nonfinite_json_rejected(self):
        raw = json.dumps(self.sample).replace('"inspected": 10', '"inspected": NaN', 1).encode()
        self.assertEqual(self.request('/api/reports/import', raw)[0], 409)

    def test_live_data_not_accepted(self):
        self.sample['synthetic'] = False
        self.assertEqual(self.request('/api/reports/import', self.sample)[0], 409)

    def test_missing_source_is_unverifiable(self):
        self.sample['source_snapshot'] = None
        code, raw, _ = self.request('/api/reports/import', self.sample)
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(raw)['item']['status'], 'unable_to_verify')

    def test_sample_routes_and_discrepancy(self):
        code, raw, _ = self.request('/api/report-sample?name=errors')
        self.assertEqual(code, 200)
        code, raw, _ = self.request('/api/reports/import', raw)
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(raw)['item']['status'], 'mismatch')
        self.assertEqual(self.request('/api/report-sample?name=../../README.txt')[0], 404)

    def test_assets_and_no_live_send_route(self):
        for path in ('/', '/report-ui.js', '/style.css', '/ui.js'):
            code, raw, headers = self.request(path)
            self.assertEqual(code, 200)
            self.assertTrue(raw)
            self.assertIn("default-src 'self'", headers['Content-Security-Policy'])
        self.assertEqual(self.request('/api/reports/send', self.sample)[0], 409)
        self.assertEqual(self.server.inbox.list_outbox(), [])


if __name__ == '__main__':
    unittest.main()

"""Autopilot makes only the sure fixes, never approves or sends, and leaves uncertain items for a person."""
import base64, json, os, sys, threading, unittest, urllib.error, urllib.request
from datetime import date
from http.server import ThreadingHTTPServer
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(__file__))
from eos_fixture import build, ROWS


class Autopilot(unittest.TestCase):
    def setUp(self):
        from api.index import handler
        from legacy_storage_fake import PostgresWorkspace
        from test_vercel import FakeDatabase
        self.env = patch.dict(os.environ, {'CI_LOCAL_DEV': '1', 'VERCEL': '0'}); self.env.start()
        for k in ('MS_TENANT_ID', 'MS_CLIENT_ID', 'MS_CLIENT_SECRET', 'ANTHROPIC_API_KEY'):
            os.environ.pop(k, None)
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), handler)
        self.server.workspace_store = PostgresWorkspace('postgresql://test.invalid/x', FakeDatabase().connect)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f'http://127.0.0.1:{self.server.server_port}'

    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.env.stop()

    def call(self, path, body):
        req = urllib.request.Request(self.base + path, data=json.dumps(body).encode(),
                                     headers={'Content-Type': 'application/json', 'Origin': self.base, 'X-Review-Token': 'same-origin'})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_paused_by_default(self):
        os.environ.pop('PPG_AUTOPILOT', None)
        st, out = self.call('/api/autopilot', {'run': True, 'force': True})
        self.assertEqual((st, out['ran'], out['paused']), (200, False, True))
        st, out = self.call('/api/autopilot', {'on': True})
        self.assertFalse(out['on'])

    def test_upload_changes_nothing_by_default(self):
        os.environ.pop('PPG_AUTOFORMAT_ON_UPLOAD', None)
        rows = [tuple(r) for r in ROWS]
        rows[1] = (rows[1][0], ' 80139189 ') + rows[1][2:]              # a sure fix, but the person decides
        st, up = self.call('/api/spreadsheets/import', {'filename': '2230-OWENS-BASCO - EOS - 2026-10-07.xlsx',
                                                        'content_base64': base64.b64encode(build(rows)).decode()})
        self.assertEqual(st, 200, up)
        self.assertNotIn('auto_formatting', up['item'])
        self.assertFalse(up['item'].get('fixed_from'))

    def test_only_sure_fixes_and_never_sends(self):
        os.environ['PPG_AUTOPILOT'] = '1'; self.addCleanup(os.environ.pop, 'PPG_AUTOPILOT', None)   # when it is brought back
        os.environ['PPG_AUTOFORMAT_ON_UPLOAD'] = '1'; self.addCleanup(os.environ.pop, 'PPG_AUTOFORMAT_ON_UPLOAD', None)   # opt-in cleanup
        rows = [tuple(r) for r in ROWS]
        rows[1] = (rows[1][0], ' 80139189 ') + rows[1][2:]              # stray spaces: a sure fix
        st, up = self.call('/api/spreadsheets/import', {'filename': '2230-OWENS-BASCO - EOS - 2026-10-07.xlsx',
                                                        'content_base64': base64.b64encode(build(rows)).decode()})
        self.assertEqual(st, 200, up)
        h = up['item']['file_hash']
        self.assertEqual(up['item']['auto_formatting']['count'], 1)       # formatting is fixed on upload already
        self.assertEqual(up['item']['auto_formatting']['examples'][0]['kind'], 'extra spaces')
        st, props = self.call('/api/spreadsheets/fixes', {'file_hash': h})
        self.assertTrue(all('sure' in p for p in props['proposals']))
        self.assertFalse(any(p['sure'] for p in props['proposals']))
        st, off = self.call('/api/autopilot', {'run': True})
        self.assertEqual((st, off['ran']), (200, False))                  # off by default: nothing happens
        self.assertTrue(self.call('/api/autopilot', {'on': True})[1]['on'])
        # run for the report's day (the app normally uses today's date at PPG)
        import api.index as idx
        with patch.object(idx, 'datetime', wraps=idx.datetime) as fake:
            fake.now.return_value = __import__('datetime').datetime(2026, 10, 8, 9, 0)
            st, out = self.call('/api/autopilot', {'run': True, 'force': True})
        self.assertEqual(st, 200, out)
        last = out['last']
        self.assertEqual(last['cleaned'], 0, last)                         # nothing sure left after the upload cleanup
        with urllib.request.urlopen(self.base + '/api/spreadsheet?id=' + h, timeout=30) as r:
            item = json.loads(r.read())
        self.assertFalse(item.get('approval')); self.assertFalse(any(x.get('sent') for x in item.get('sent') or []))
        with patch.object(idx, 'datetime', wraps=idx.datetime) as fake:
            fake.now.return_value = __import__('datetime').datetime(2026, 10, 8, 9, 0)
            again = self.call('/api/autopilot', {'run': True, 'force': True})[1]['last']
        self.assertEqual(again['cleaned'], 0)                              # a report is cleaned once

    def test_remove_duplicate_rows(self):
        os.environ['PPG_AUTOFORMAT'] = '0'; self.addCleanup(os.environ.pop, 'PPG_AUTOFORMAT', None)   # the manual way
        rows = [tuple(r) for r in ROWS]
        rows[4] = rows[2]
        st, up = self.call('/api/spreadsheets/import', {'filename': '2230-OWENS-BASCO - EOS - 2026-10-07.xlsx',
                                                        'content_base64': base64.b64encode(build(rows)).decode()})
        h = up['item']['file_hash']
        with urllib.request.urlopen(self.base + '/api/spreadsheet?id=' + h, timeout=30) as r:
            dups = [i for i in json.loads(r.read())['review']['issues'] if i['code'] == 'duplicate_row']
        self.assertEqual(len(dups), 1)
        st, out = self.call('/api/spreadsheets/remove-rows', {'file_hash': h, 'rows': [{'sheet': dups[0]['sheet'], 'row': dups[0]['row']}]})
        self.assertEqual(st, 200, out)
        with urllib.request.urlopen(self.base + '/api/spreadsheet?id=' + out['item']['file_hash'], timeout=30) as r:
            after = json.loads(r.read())['review']['issues']
        self.assertFalse([i for i in after if i['code'] == 'duplicate_row'])      # the extra copy is gone
        self.assertEqual(self.call('/api/spreadsheets/remove-rows', {'file_hash': h, 'rows': []})[0], 409)


if __name__ == '__main__':
    unittest.main()

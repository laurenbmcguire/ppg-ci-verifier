"""Claude / ChatGPT connector (MCP over HTTP): handshake, tools, search/fetch, and the send gates. Fake mailbox."""
import base64, json, os, sys, threading, unittest, urllib.error, urllib.request
from http.server import ThreadingHTTPServer
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(__file__))
from test_api import seeded, NAME
from test_send import FakeMail, ENV
from hosting import sharepoint


class McpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ['PPG_AUTOFORMAT'] = '0'   # these tests walk through the manual fix steps
        sup = super(); getattr(sup, 'setUpClass', lambda: None)()

    @classmethod
    def tearDownClass(cls):
        os.environ.pop('PPG_AUTOFORMAT', None)
        sup = super(); getattr(sup, 'tearDownClass', lambda: None)()

    def setUp(self):
        from api.index import handler
        from legacy_storage_fake import PostgresWorkspace
        from test_vercel import FakeDatabase
        sharepoint._token.update(value=None, expires=0)
        self.env = patch.dict(os.environ, dict(ENV, CI_LOCAL_DEV='1', VERCEL='0')); self.env.start()
        os.environ.pop('ANTHROPIC_API_KEY', None)
        self.fake = FakeMail()
        self.graph = patch('hosting.sharepoint._open', lambda request, opener=None: self.fake(request)); self.graph.start()
        self.lookup = patch('hosting.sharepoint.fetch_export', side_effect=sharepoint.SharePointNotFound('none')); self.lookup.start()
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), handler)
        self.server.workspace_store = PostgresWorkspace('postgresql://test.invalid/x', FakeDatabase().connect)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f'http://127.0.0.1:{self.server.server_port}'
        self.n = 0

    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.graph.stop(); self.lookup.stop(); self.env.stop()
        sharepoint._token.update(value=None, expires=0)

    def app(self, path, body):
        req = urllib.request.Request(self.base + path, data=json.dumps(body).encode(),
                                     headers={'Content-Type': 'application/json', 'Origin': self.base, 'X-Review-Token': 'same-origin'})
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())

    def post(self, url, body):
        req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                     headers={'Content-Type': 'application/json', 'Accept': 'application/json, text/event-stream'})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                data = r.read()
                return r.status, (json.loads(data) if data else None)
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b'null')

    def rpc(self, url, method, params=None):
        self.n += 1
        status, out = self.post(url, dict(jsonrpc='2.0', id=self.n, method=method, params=params or {}))
        self.assertEqual(status, 200, out)
        return out

    def tool(self, url, name, **args):
        res = self.rpc(url, 'tools/call', dict(name=name, arguments=args))['result']
        return res['isError'], (json.loads(res['content'][0]['text']) if not res['isError'] else res['content'][0]['text'])

    def connector(self, can_send=False):
        made = self.app('/api/keys/create', {'name': 'Claude', 'can_send': can_send, 'connector': True})['key']
        self.assertTrue(made['connector_url'].startswith(self.base + '/mcp/ppgv_'))
        return made['connector_url']

    def test_handshake_and_tools(self):
        url = self.connector()
        init = self.rpc(url, 'initialize', dict(protocolVersion='2025-06-18', capabilities={}, clientInfo=dict(name='claude-ai', version='1')))
        self.assertEqual(init['result']['protocolVersion'], '2025-06-18')
        self.assertEqual(init['result']['serverInfo']['name'], 'ppg-verifier')
        self.assertIn('data@ppgquality.com', init['result']['instructions'])
        self.assertEqual(self.post(url, dict(jsonrpc='2.0', method='notifications/initialized'))[0], 202)
        names = {t['name'] for t in self.rpc(url, 'tools/list')['result']['tools']}
        self.assertTrue({'search', 'fetch', 'list_reports', 'get_report', 'apply_fixes', 'approve_report', 'send_to_client'} <= names)
        self.assertEqual(self.rpc(url, 'nope')['error']['code'], -32601)
        self.assertEqual(self.post(self.base + '/mcp/ppgv_bad', dict(jsonrpc='2.0', id=1, method='initialize'))[0], 401)
        with self.assertRaises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(url, timeout=10)
        self.assertEqual(e.exception.code, 405)

    def test_report_flow_and_send_gate(self):
        url = self.connector()
        self.app('/api/spreadsheets/import', {'filename': NAME, 'content_base64': base64.b64encode(seeded()).decode()})
        err, found = self.tool(url, 'search', query='owens')                       # ChatGPT style
        self.assertFalse(err); rid = found['results'][0]['id']
        self.assertTrue(found['results'][0]['url'].endswith('#r=' + rid))
        err, doc = self.tool(url, 'fetch', id=rid)
        self.assertIn('2230-OWENS-BASCO', doc['text']); self.assertIn('Status:', doc['text'])
        err, rep = self.tool(url, 'get_report', id=rid)                             # Claude style
        self.assertEqual(rep['id'], rid)
        err, fx = self.tool(url, 'get_fixes', id=rid); self.assertTrue(fx['fixes'])
        err, fixed = self.tool(url, 'apply_fixes', id=rid, accept_all=True)
        self.assertFalse(err, fixed); self.assertEqual(fixed['corrected_from']['id'], rid)
        err, msg = self.tool(url, 'send_to_client', id=fixed['id'], mode='draft')
        self.assertTrue(err); self.assertIn('cannot send', msg)                    # read-only connector
        self.assertEqual(self.tool(url, 'get_report', id='zz')[0], True)
        sender = self.connector(can_send=True)
        err, msg = self.tool(sender, 'send_to_client', id=fixed['id'], mode='draft')
        self.assertTrue(err); self.assertIn('Approve', msg)
        err, ok = self.tool(sender, 'approve_report', id=fixed['id'], approver='Lauren', override=True)
        self.assertFalse(err, ok)
        err, prev = self.tool(sender, 'preview_client_email', id=fixed['id'])
        self.assertEqual(prev['to'], ['buyer@client.com'])
        err, out = self.tool(sender, 'send_to_client', id=fixed['id'], mode='draft')
        self.assertFalse(err, out); self.assertFalse(out['record']['sent']); self.assertEqual(out['record']['via'], 'connector:Claude')
        self.assertFalse(any(u.endswith('/send') for _, u, _ in self.fake.calls))   # draft only, nothing sent


if __name__ == '__main__':
    unittest.main()

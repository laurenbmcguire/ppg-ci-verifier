"""Shift times: scans put on a shift by time (midnight rule), wrong-shift rows found and moved, settings and AI rules."""
import base64, io, json, os, sys, threading, unittest, urllib.error, urllib.request
from datetime import datetime
from http.server import ThreadingHTTPServer
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(__file__))
from eos_fixture import build, ROWS
from hosting import sharepoint, shifts

JOB, NAME = '2230-OWENS-BASCO', '2230-OWENS-BASCO - EOS - 10.07.26.xlsx'
LATE = '8010808537'                      # scanned at 4:00 PM Central: 2nd shift, but listed on the 1st Shift tab


def items():
    out = []
    for n, (ident, part, ins, rej, defect, comment) in enumerate(ROWS, 1):
        stamp = '2026-10-07T21:00:00Z' if ident == LATE else f'2026-10-07T{13 + n % 5:02d}:10:00Z'
        out.append({'id': str(n), 'createdDateTime': stamp, 'lastModifiedDateTime': stamp,
                    'fields': {'Identifier': ident, 'PartNumber': part, 'QuantityInspected': ins, 'QtyNOK': rej,
                               'Shift': '1', 'Date0': stamp}})
    # a 3rd-shift scan at 1:15 AM on the 8th belongs to the 7th; a 7:00 AM scan on the 8th does not
    out.append({'id': '98', 'createdDateTime': '2026-10-08T06:15:00Z', 'fields': {'Identifier': 'NIGHT1', 'Shift': '3', 'Date0': '2026-10-08T06:15:00Z', 'QuantityInspected': 1}})
    out.append({'id': '99', 'createdDateTime': '2026-10-08T12:00:00Z', 'fields': {'Identifier': 'NEXTDAY', 'Shift': '1', 'Date0': '2026-10-08T12:00:00Z', 'QuantityInspected': 1}})
    return out


COLS = [{'name': 'Identifier', 'displayName': 'Identifier'}, {'name': 'PartNumber', 'displayName': 'Part Number'},
        {'name': 'QuantityInspected', 'displayName': 'Qty Inspected'}, {'name': 'QtyNOK', 'displayName': 'Qty NOK'},
        {'name': 'Shift', 'displayName': 'Shift'}, {'name': 'Date0', 'displayName': 'Date'}]


class FakeGraph:
    def __call__(self, request, timeout=None):
        url = request.full_url
        ok = lambda d: io.BytesIO(json.dumps(d).encode())
        if 'login.microsoftonline.com' in url:
            return ok({'access_token': 't', 'expires_in': 3600})
        if '/sites/ppgquality.sharepoint.com:/sites/' in url:
            return ok({'id': 'site-' + url.rsplit('/', 1)[1]})
        if url.endswith('/lists?$select=id,displayName,webUrl&$top=500'):
            return ok({'value': [{'id': 'L1', 'displayName': f'Data #{JOB}', 'webUrl': 'https://x/lists/data'}]})
        if '/lists/L1/columns' in url:
            return ok({'value': COLS})
        if '/lists/L1/items' in url:
            return ok({'value': items()})
        raise urllib.error.HTTPError(url, 404, 'nf', {}, io.BytesIO(b''))


ENV = {'MS_TENANT_ID': 't', 'MS_CLIENT_ID': 'c', 'MS_CLIENT_SECRET': 's', 'SHAREPOINT_SITES': 'Owens'}


class ShiftUnitTests(unittest.TestCase):
    def setUp(self):
        sharepoint._token.update(value=None, expires=0); sharepoint._list_cache.clear(); sharepoint._site_ids.clear()
        self.env = patch.dict(os.environ, ENV); self.env.start()

    def tearDown(self):
        self.env.stop(); sharepoint._list_cache.clear(); sharepoint._token.update(value=None, expires=0); sharepoint._site_ids.clear()

    def test_midnight_rule_and_time_based_shift(self):
        name, raw, meta = sharepoint.list_day(JOB, '2026-10-07', opener=FakeGraph(), schedule=shifts.DEFAULT)
        text = raw.decode()
        self.assertIn('NIGHT1', text); self.assertNotIn('NEXTDAY', text)
        self.assertEqual(meta['scan_shifts'][LATE]['shift'], '2')
        self.assertEqual(meta['scan_shifts'][LATE]['picked'], '1')
        self.assertEqual(meta['scan_shifts']['NIGHT1']['shift'], '3')
        self.assertIn('Picked shift', text.splitlines()[0])
        # without shift times the old calendar-day split still applies
        _, raw2, meta2 = sharepoint.list_day(JOB, '2026-10-07', opener=FakeGraph())
        self.assertNotIn('NIGHT1', raw2.decode()); self.assertNotIn('scan_shifts', meta2)

    def test_clean_rejects_overlaps_and_bad_times(self):
        with self.assertRaises(shifts.ShiftError):
            shifts.clean([dict(name='1st', start='06:00', end='15:00'), dict(name='2nd', start='14:30', end='22:30')])
        with self.assertRaises(shifts.ShiftError):
            shifts.clean([dict(name='1st', start='6am', end='15:00')])
        self.assertEqual(shifts.clean(shifts.DEFAULT)[2]['start'], '22:30')
        s, day = shifts.assign(datetime(2026, 10, 8, 1, 15), shifts.DEFAULT)
        self.assertEqual((s['name'], day), ('3rd', '2026-10-07'))


class ShiftRouteTests(unittest.TestCase):
    def setUp(self):
        from api.index import handler
        from legacy_storage_fake import PostgresWorkspace
        from test_vercel import FakeDatabase
        sharepoint._token.update(value=None, expires=0); sharepoint._list_cache.clear(); sharepoint._site_ids.clear()
        self.env = patch.dict(os.environ, dict(ENV, CI_LOCAL_DEV='1', VERCEL='0')); self.env.start()
        os.environ.pop('ANTHROPIC_API_KEY', None)
        fake = FakeGraph()
        self.graph = patch('hosting.sharepoint._open', lambda request, opener=None: fake(request)); self.graph.start()
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), handler)
        self.server.workspace_store = PostgresWorkspace('postgresql://test.invalid/x', FakeDatabase().connect)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f'http://127.0.0.1:{self.server.server_port}'

    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.graph.stop(); self.env.stop()
        sharepoint._list_cache.clear(); sharepoint._token.update(value=None, expires=0); sharepoint._site_ids.clear()

    def post(self, path, body):
        req = urllib.request.Request(self.base + path, data=json.dumps(body).encode(),
                                     headers={'Content-Type': 'application/json', 'Origin': self.base, 'X-Review-Token': 'same-origin'})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_wrong_shift_found_and_moved(self):
        status, data = self.post('/api/spreadsheets/import', {'filename': NAME, 'content_base64': base64.b64encode(build()).decode()})
        self.assertEqual(status, 200, data); item = data['item']
        sp = item['sharepoint']
        wrong = [f for f in sp['findings'] if f['code'] == 'wrong_shift']
        self.assertEqual(len(wrong), 1, sp['findings']); self.assertIn(LATE, wrong[0]['message'])
        self.assertIn('2nd shift (2:30 PM to 10:30 PM)', wrong[0]['message'])
        self.assertFalse([f for f in sp['findings'] if f['code'] == 'missing_from_report' and 'NIGHT1' in f['message']])  # other shifts left out
        status, out = self.post('/api/spreadsheets/shift-fix', {'file_hash': item['file_hash']})
        self.assertEqual(status, 200, out); self.assertEqual(out['moved'], 1)
        fixed = out['item']
        self.assertEqual(fixed['fixed_from']['file_hash'], item['file_hash'])
        self.assertFalse([f for f in (fixed.get('sharepoint') or {}).get('findings') or [] if f['code'] == 'wrong_shift'])
        status, again = self.post('/api/spreadsheets/shift-fix', {'file_hash': fixed['file_hash']})
        self.assertEqual(status, 409)

    def test_settings_shifts_and_rules(self):
        status, s = self.post('/api/settings', {})
        self.assertEqual(status, 200); self.assertEqual(s['shifts']['default'][0]['start'], '06:30')
        self.assertTrue(any(g['title'] == 'Against SharePoint' for g in s['checks']))
        status, err = self.post('/api/settings/shifts', {'shifts': [dict(name='1st', start='06:00', end='15:00'), dict(name='2nd', start='14:00', end='22:00')]})
        self.assertEqual(status, 409); self.assertIn('overlap', err['error'])
        mine = [dict(name='1st', start='07:00', end='17:00'), dict(name='2nd', start='17:00', end='03:00')]
        status, s = self.post('/api/settings/shifts', {'job': 'hands-hmma-2219', 'shifts': mine})
        self.assertEqual(status, 200, s); self.assertEqual(s['shifts']['jobs']['HANDS-HMMA-2219'][1]['label'], '2nd shift (5:00 PM to 3:00 AM)')
        status, s = self.post('/api/settings/shifts', {'job': 'HANDS-HMMA-2219', 'remove': True})
        self.assertNotIn('HANDS-HMMA-2219', s['shifts']['jobs'])
        status, s = self.post('/api/settings/rules', {'text': 'Flag any VIN that does not start with HZA', 'job': 'HANDS-HMMA-2219'})
        self.assertEqual(status, 200); rule = s['rules'][0]; self.assertEqual(rule['job'], 'HANDS-HMMA-2219')
        self.post('/api/settings/rules', {'text': 'Comments must not mention inspector names'})
        from hosting.spreadsheet_inbox import SpreadsheetInbox
        with self.server.workspace_store.session() as d:
            self.assertEqual(len(SpreadsheetInbox(d).rules_for('HANDS-HMMA-2219')), 2)
            self.assertEqual(SpreadsheetInbox(d).rules_for('2230-OWENS-BASCO'), ['Comments must not mention inspector names'])
        status, s = self.post('/api/settings/rules', {'remove': rule['id']})
        self.assertEqual(len(s['rules']), 1)

    def test_rules_reach_the_ai_prompt(self):
        from hosting import ai_review
        seen = {}
        def fake_post(body, opener=None):
            seen['prompt'] = body['messages'][0]['content']
            return {'content': [{'type': 'tool_use', 'input': {'summary': 'ok', 'issues': []}}]}
        with patch.object(ai_review, '_post', fake_post), patch.dict(os.environ, {'ANTHROPIC_API_KEY': 'x'}):
            ai_review.review({'filename': NAME, 'findings': []}, build(), rules=['Flag any VIN that does not start with HZA'])
        self.assertIn('<reviewer_rules>', seen['prompt']); self.assertIn('does not start with HZA', seen['prompt'])


if __name__ == '__main__':
    unittest.main()

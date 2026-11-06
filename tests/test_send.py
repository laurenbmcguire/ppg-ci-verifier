"""Approve → send to client: wording, Reply All on the last report, draft vs send, and the server-side gates.
A fake Microsoft 365 answers every call; nothing is ever sent."""
import base64, io, json, os, sys, threading, unittest, urllib.error, urllib.request
from http.server import ThreadingHTTPServer
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(__file__))
from eos_fixture import build
from hosting import mailer, sharepoint

NAME = '2230-OWENS-BASCO - EOS - 10.07.26.xlsx'
PREV_BODY = ('Good Afternoon,\r\n\r\nPlease see the attached PPG Quality End of Shift Data and Billing Report for the '
             'Basco inspection completed on 1st shift 10.06.26.\r\n\r\nThank you,\r\nPPG Quality\r\n\r\n'
             '________________________________\r\nFrom: Someone <a@b.com>\r\nSent: Monday\r\nold thread text 10.01.26')


class FakeMail:
    def __init__(self, previous=True, fail_send=None):
        self.calls, self.previous, self.fail_send = [], previous, fail_send

    def __call__(self, request, timeout=None):
        url, method = request.full_url, request.get_method()
        body = json.loads(request.data) if request.data and 'login.' not in url else None
        self.calls.append((method, url, body))
        reply = lambda d: io.BytesIO(json.dumps(d).encode())
        if 'login.microsoftonline.com' in url:
            return reply({'access_token': 't', 'expires_in': 3600})
        if '/mailFolders/sentitems/messages' in url:
            value = [
                {'id': 'old', 'subject': 'RE: 2230-OWENS-BASCO| PPG Quality End-of-Shift Report - 10.01.26 1st Shift', 'sentDateTime': '2026-10-01T20:00:00Z',
                 'toRecipients': [{'emailAddress': {'address': 'old@client.com'}}], 'ccRecipients': [], 'body': {'content': 'x'}},
                {'id': 'm1', 'subject': 'RE: 2230-OWENS-BASCO| PPG Quality End-of-Shift Report - 10.06.26 1st Shift', 'sentDateTime': '2026-10-06T20:00:00Z',
                 'webLink': 'https://outlook/m1', 'toRecipients': [{'emailAddress': {'address': 'buyer@client.com'}}],
                 'ccRecipients': [{'emailAddress': {'address': 'qa@client.com'}}, {'emailAddress': {'address': 'lauren.mcguire@ppgquality.com'}}],
                 'body': {'content': PREV_BODY}},
                {'id': 'other', 'subject': '9999-OTHER| PPG Quality End-of-Shift Report - 10.07.26', 'sentDateTime': '2026-10-07T20:00:00Z'}]
            return reply({'value': value if self.previous else []})
        if url.endswith('/createReplyAll'):
            return reply({'id': 'draft1', 'webLink': 'https://outlook/draft1'})
        if method == 'POST' and url.endswith('/messages'):
            return reply({'id': 'draft2', 'webLink': 'https://outlook/draft2'})
        if url.endswith('/attachments'):
            return reply({'id': 'att'})
        if url.endswith('/send'):
            if self.fail_send:
                raise urllib.error.HTTPError(url, self.fail_send, 'no', {}, io.BytesIO(b''))
            return io.BytesIO(b'')
        if method == 'PATCH':
            return reply({'id': 'draft1'})
        raise urllib.error.HTTPError(url, 404, 'nf', {}, io.BytesIO(b''))


ENV = {'MS_TENANT_ID': 't', 'MS_CLIENT_ID': 'c', 'MS_CLIENT_SECRET': 's'}


class WordingTests(unittest.TestCase):
    def test_date_and_shift(self):
        self.assertEqual(mailer.mdy('2026-09-18'), '9.18.26')
        self.assertEqual(mailer.mdy('2026-10-07'), '10.07.26')
        self.assertEqual(mailer.shift_of({'filename': 'HANDS 2nd Shift 10.07.26.xlsx'}), '2nd')
        self.assertEqual(mailer.shift_of({'filename': 'x.xlsx', 'report_identity': {'shift': 'First shift'}}), '1st')
        self.assertIsNone(mailer.shift_of({'filename': '1st shift and 2nd shift.xlsx'}))

    def test_compose_from_previous_keeps_wording_and_swaps_date_and_shift(self):
        prev = {'subject': 'RE: RE: 2230-OWENS-BASCO| PPG Quality End-of-Shift Report - 10.06.26 1st Shift', 'body': PREV_BODY}
        subject, body = mailer.compose('2230-OWENS-BASCO', '2026-10-07', '2nd', prev)
        self.assertEqual(subject, '2230-OWENS-BASCO| PPG Quality End-of-Shift Report - 10.07.26 2nd Shift')
        self.assertIn('Basco inspection completed on 2nd shift 10.07.26.', body)
        self.assertIn('Thank you,', body)
        self.assertNotIn('old thread', body)   # quoted history is left to the reply itself

    def test_compose_without_previous_uses_the_pattern(self):
        subject, body = mailer.compose('2230-OWENS-BASCO', '2026-10-07', None, None)
        self.assertEqual(subject, '2230-OWENS-BASCO| PPG Quality End-of-Shift Report - 10.07.26')
        self.assertTrue(body.startswith('Good Afternoon,'))

    def test_addresses(self):
        self.assertEqual(mailer.clean_addresses('a@b.com; A@B.com, c@d.org', 'To'), ['a@b.com', 'c@d.org'])
        with self.assertRaises(mailer.MailError):
            mailer.clean_addresses(['not-an-email'], 'To')


class GraphTests(unittest.TestCase):
    def setUp(self):
        sharepoint._token.update(value=None, expires=0)
        self.env = patch.dict(os.environ, ENV); self.env.start()

    def tearDown(self):
        self.env.stop(); sharepoint._token.update(value=None, expires=0)

    def test_last_sent_picks_newest_for_this_job(self):
        prev = mailer.last_sent('2230-OWENS-BASCO', opener=FakeMail())
        self.assertEqual(prev['id'], 'm1')
        self.assertEqual(prev['to'], ['buyer@client.com'])
        self.assertIsNone(mailer.last_sent('2230-OWENS-BASCO', opener=FakeMail(previous=False)))

    def test_reply_all_draft_then_send(self):
        fake = FakeMail()
        out = mailer.deliver(['buyer@client.com'], ['qa@client.com'], 'S', 'Body', 'r.xlsx', b'PK123', reply_to='m1', opener=fake)
        self.assertTrue(out['sent'])
        steps = [(m, u.rsplit('/', 1)[1]) for m, u, _ in fake.calls if 'login' not in u]
        self.assertEqual(steps, [('POST', 'createReplyAll'), ('PATCH', 'draft1'), ('POST', 'attachments'), ('POST', 'send')])
        att = [b for m, u, b in fake.calls if u.endswith('/attachments')][0]
        self.assertEqual(base64.b64decode(att['contentBytes']), b'PK123')
        self.assertTrue(fake.calls[1][1].startswith('https://graph.microsoft.com/v1.0/users/data%40ppgquality.com/'))

    def test_draft_only_never_calls_send(self):
        fake = FakeMail()
        out = mailer.deliver(['buyer@client.com'], [], 'S', 'Body', 'r.xlsx', b'x', send_now=False, opener=fake)
        self.assertFalse(out['sent']); self.assertEqual(out['web_link'], 'https://outlook/draft2')
        self.assertFalse(any(u.endswith('/send') for _, u, _ in fake.calls))

    def test_permission_error_is_plain(self):
        with self.assertRaises(mailer.MailError) as e:
            mailer.deliver(['buyer@client.com'], [], 'S', 'B', 'r.xlsx', b'x', opener=FakeMail(fail_send=403))
        self.assertIn('Mail.Send', str(e.exception))

    def test_too_big_and_no_recipients(self):
        with self.assertRaises(mailer.MailError):
            mailer.deliver([], [], 'S', 'B', 'r.xlsx', b'x', opener=FakeMail())
        with self.assertRaises(mailer.MailError):
            mailer.deliver(['a@b.com'], [], 'S', 'B', 'r.xlsx', b'x' * (mailer.MAX_ATTACHMENT + 1), opener=FakeMail())


class SendRouteTests(unittest.TestCase):
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

    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.graph.stop(); self.lookup.stop(); self.env.stop()
        sharepoint._token.update(value=None, expires=0)

    def post(self, path, body):
        req = urllib.request.Request(self.base + path, data=json.dumps(body).encode(),
                                     headers={'Content-Type': 'application/json', 'Origin': self.base, 'X-Review-Token': 'same-origin'})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def upload(self):
        status, data = self.post('/api/spreadsheets/import', {'filename': NAME, 'content_base64': base64.b64encode(build()).decode()})
        self.assertEqual(status, 200, data)
        return data['item']['file_hash']

    def test_approve_then_send_records_it_and_remembers_contacts(self):
        h = self.upload()
        status, info = self.post('/api/spreadsheets/send-info', {'file_hash': h})
        self.assertEqual(status, 200, info)
        self.assertEqual(info['recipients_from'], 'previous')
        self.assertEqual(info['to'], ['buyer@client.com'])
        self.assertIn('lauren.mcguire@ppgquality.com', info['cc'])
        self.assertEqual(info['subject'], '2230-OWENS-BASCO| PPG Quality End-of-Shift Report - 10.07.26 1st Shift')
        self.assertEqual(info['attachment']['name'], NAME)
        msg = dict(file_hash=h, to=info['to'], cc=info['cc'], subject=info['subject'], body=info['body'], reply_to='m1', mode='send')
        status, data = self.post('/api/spreadsheets/send', msg)
        self.assertEqual(status, 409); self.assertIn('Approve', data['error'])
        self.assertFalse(any(u.endswith('/send') for _, u, _ in self.fake.calls))
        status, data = self.post('/api/spreadsheets/approve', {'file_hash': h, 'approver': 'Chris', 'override': True})
        self.assertEqual(status, 200, data); self.assertEqual(data['item']['approval']['by'], 'Chris')
        status, data = self.post('/api/spreadsheets/send', msg)
        self.assertEqual(status, 200, data)
        self.assertTrue(data['record']['sent']); self.assertTrue(data['record']['reply_all'])
        self.assertEqual(len(data['item']['sent']), 1)
        self.assertTrue(any(u.endswith('/send') for _, u, _ in self.fake.calls))
        status, data = self.post('/api/spreadsheets/send', msg)   # a second send needs "Send again"
        self.assertEqual(status, 409); self.assertIn('Send again', data['error'])
        status, data = self.post('/api/spreadsheets/approve', {'file_hash': h, 'withdraw': True})
        self.assertEqual(status, 409)                                # approval stays once sent
        self.fake.previous = False                                   # no earlier email: use the contacts saved by the app
        status, info = self.post('/api/spreadsheets/send-info', {'file_hash': h})
        self.assertEqual(info['recipients_from'], 'saved'); self.assertEqual(info['to'], ['buyer@client.com'])

    def test_withdraw_and_approve_again(self):
        status, data = self.post('/api/spreadsheets/import', {'filename': NAME, 'content_base64': base64.b64encode(build()).decode()})
        h = data['item']['file_hash']
        for _ in range(2):
            status, data = self.post('/api/spreadsheets/approve', {'file_hash': h, 'approver': 'Lauren', 'override': True})
            self.assertEqual(status, 200, data); self.assertTrue(data['item'].get('approval'))
            status, data = self.post('/api/spreadsheets/approve', {'file_hash': h, 'approver': 'Lauren', 'withdraw': True})
            self.assertEqual(status, 200, data); self.assertFalse(data['item'].get('approval'))
            self.assertEqual(data['item']['approval_withdrawn']['by'], 'Lauren')

    def test_must_fix_needs_approve_anyway(self):
        from eos_fixture import ROWS
        rows = [list(r) for r in ROWS]; rows[1][0] = ''
        status, data = self.post('/api/spreadsheets/import', {'filename': NAME, 'content_base64': base64.b64encode(build([tuple(r) for r in rows])).decode()})
        h = data['item']['file_hash']
        status, data = self.post('/api/spreadsheets/approve', {'file_hash': h, 'approver': 'Chris'})
        if status == 409:
            self.assertIn('Approve anyway', data['error'])
            status, data = self.post('/api/spreadsheets/approve', {'file_hash': h, 'approver': 'Chris', 'override': True})
            self.assertTrue(data['item']['approval']['overridden'])
        self.assertEqual(status, 200, data)
        status, data = self.post('/api/spreadsheets/approve', {'file_hash': h, 'approver': ''})
        self.assertEqual(status, 409)

    def test_draft_mode_and_mail_errors(self):
        h = self.upload()
        self.post('/api/spreadsheets/approve', {'file_hash': h, 'approver': 'Lauren', 'override': True})
        status, data = self.post('/api/spreadsheets/send', dict(file_hash=h, to=['buyer@client.com'], cc=[], subject='S', body='B', mode='draft'))
        self.assertEqual(status, 200, data); self.assertFalse(data['record']['sent'])
        self.assertFalse(any(u.endswith('/send') for _, u, _ in self.fake.calls))
        self.fake.fail_send = 403
        status, data = self.post('/api/spreadsheets/send', dict(file_hash=h, to=['buyer@client.com'], cc=[], subject='S', body='B', mode='send'))
        self.assertEqual(status, 409); self.assertIn('Mail.Send', data['error'])


if __name__ == '__main__':
    unittest.main()

"""Report vs SharePoint daily export: comparison rules, Graph lookup (fake endpoints), and HTTP routes."""
import base64
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(Path(__file__).parent))
from eos_fixture import build, source_csv, ROWS
from hosting import sharepoint
from hosting.source_compare import parse_export, compare, identify
from hosting.spreadsheets import report_records
from hosting.spreadsheet_inbox import SpreadsheetInbox


def run(report_rows=None, source_rows=None, overrides=None):
    rows = report_records('eos.xlsx', build(rows=report_rows or list(ROWS), overrides=overrides))
    return compare(rows, parse_export('2230-OWENS-BASCO 2026-10-07.csv', source_csv(source_rows or list(ROWS))))


class CompareTests(unittest.TestCase):
    def test_identical_report_matches(self):
        c = run()
        self.assertEqual(c['status'], 'match', c['findings'])
        self.assertEqual((c['report_rows'], c['sharepoint_rows'], c['matched_rows']), (13, 13, 13))
        self.assertEqual(c['totals']['inspected'], {'report': '438', 'sharepoint': '438'})
        self.assertEqual(c['totals']['certified'], {'report': '251', 'sharepoint': '251'})  # report formula results vs Qty OK

    def test_value_difference(self):
        report = list(ROWS); report[2] = report[2][:3] + (12,) + report[2][4:]
        c = run(report_rows=report)
        self.assertEqual(c['status'], 'differences')
        diff = [f for f in c['findings'] if f['code'] == 'differs_from_sharepoint']
        self.assertTrue(any(f['field'] == 'rejected' and f['report_value'] == '12' and f['sharepoint_value'] == '11' for f in diff), diff)
        self.assertIn('total_differs_from_sharepoint', c['finding_counts'])

    def test_missing_and_extra_rows(self):
        c = run(report_rows=list(ROWS)[:-1])
        self.assertEqual(c['finding_counts']['missing_from_report'], 1)
        extra = list(ROWS) + [('8010999999', '80139189', 34, 1, 'Holes / Glue', 'x')]
        c = run(report_rows=extra)
        self.assertEqual(c['finding_counts']['not_on_sharepoint'], 1)

    def test_identifier_typo_suggests_the_sharepoint_scan(self):
        report = list(ROWS); report[0] = ('8O10808539',) + report[0][1:]
        c = run(report_rows=report)
        typo = [f for f in c['findings'] if f['code'] == 'identifier_typo_vs_sharepoint']
        self.assertEqual(typo[0]['sharepoint_value'], '8010808539')
        self.assertNotIn('missing_from_report', c['finding_counts'])  # the typo is one finding, not also a missing row

    def test_part_and_date_differences(self):
        report = list(ROWS); report[1] = (report[1][0], '80139198') + report[1][2:]
        c = run(report_rows=report, overrides={('1st Shift', 'A16'): '2026-10-06'})
        fields = {f['field'] for f in c['findings'] if f['code'] == 'differs_from_sharepoint'}
        self.assertEqual(fields, {'part', 'day'})

    def test_duplicates_in_sharepoint_export(self):
        source = list(ROWS) + [ROWS[0]]
        c = run(source_rows=source)
        self.assertIn('sharepoint_duplicate_identifier', c['finding_counts'])

    def test_identify_job_and_date(self):
        self.assertEqual(identify('2230-OWENS-BASCO - PPG Quality - End of Shift Data and Billing Report - 10.07.26 (new format sample).xlsx', {}),
                         ('2230-OWENS-BASCO', '2026-10-07'))
        self.assertEqual(identify('x.xlsx', {'report_identity': {'project_number': 'SLAL-HYMOBAL-2221', 'report_date': '2026-11-03'}}),
                         ('SLAL-HYMOBAL-2221', '2026-11-03'))
        self.assertEqual(identify('MERGSC-GRAMMS-2227 2026-10-16.xlsx', {}), ('MERGSC-GRAMMS-2227', '2026-10-16'))


class FakeGraph:
    def __init__(self, files):
        self.files = files  # {(site, path): bytes}
        self.calls = []

    def __call__(self, request, timeout=None):
        url = request.full_url
        self.calls.append(url)
        if 'oauth2/v2.0/token' in url:
            return io.BytesIO(json.dumps({'access_token': 'tok', 'expires_in': 3600}).encode())
        assert request.headers.get('Authorization') == 'Bearer tok'
        path = urllib.request.unquote(url.split('/v1.0', 1)[1])
        if path.startswith('/sites/ppgquality.sharepoint.com:/sites/'):
            name = path.rsplit('/', 1)[1]
            return io.BytesIO(json.dumps({'id': 'id-' + name}).encode())
        for (site, fpath), data in self.files.items():
            if path == f'/sites/id-{site}/drive/root:/{fpath}':
                return io.BytesIO(json.dumps({'id': 'item-' + site, 'webUrl': 'https://x/' + fpath}).encode())
            if path == f'/sites/id-{site}/drive/items/item-{site}/content':
                return io.BytesIO(data)
        raise urllib.error.HTTPError(url, 404, 'nf', {}, None)


ENV = {'MS_TENANT_ID': 't', 'MS_CLIENT_ID': 'c', 'MS_CLIENT_SECRET': 'very-secret-value', 'SHAREPOINT_SITES': 'Lear_T1,BriggsStratton_Auburn'}


class GraphLookupTests(unittest.TestCase):
    def setUp(self):
        sharepoint._token.update(value=None, expires=0)
        sharepoint._site_ids.clear()

    def test_reads_only_the_exact_assigned_list(self):
        calls = []
        base = '/sites/site-exact/lists/list-exact'
        def graph(request, timeout=None):
            calls.append(request.full_url)
            if 'oauth2/v2.0/token' in request.full_url:
                return io.BytesIO(json.dumps({'access_token': 'tok', 'expires_in': 3600}).encode())
            path = urllib.request.unquote(request.full_url.split('/v1.0', 1)[1])
            if path == base:
                value = dict(id='list-exact', displayName='Data #2230-OWENS-BASCO',
                             webUrl='https://example.com/sites/Synthetic/Lists/Exact')
            elif path.startswith(base + '/columns?'):
                value = {'value': [dict(name=n, displayName=n) for n in ('Date', 'Identifier', 'Quantity Inspected', 'Quantity Rejected')]}
            elif path.startswith(base + '/items?'):
                value = {'value': [dict(id='1', fields={'Date': '2026-10-07', 'Identifier': 'synthetic-1',
                                                      'Quantity Inspected': 34, 'Quantity Rejected': 1})]}
            else:
                raise AssertionError('Unexpected cross-source request: ' + path)
            return io.BytesIO(json.dumps(value).encode())
        with patch.dict(os.environ, ENV):
            name, content, meta = sharepoint.fetch_export('2230-OWENS-BASCO', '2026-10-07', opener=graph,
                                                        site_id='site-exact', list_id='list-exact')
        self.assertIn('SharePoint list', name)
        self.assertIn(b'synthetic-1', content)
        self.assertEqual(meta['capture']['site_id'], 'site-exact')
        self.assertEqual(meta['capture']['list_id'], 'list-exact')
        self.assertTrue(meta['capture']['terminal'])
        self.assertTrue(all('client_secret' not in c and '/drive/' not in c for c in calls))

    def test_job_only_lookup_cannot_fall_back_to_named_export(self):
        fake = FakeGraph({})
        with patch.dict(os.environ, ENV), self.assertRaisesRegex(sharepoint.SharePointError, 'verified site_id and list_id'):
            sharepoint.fetch_export('2230-OWENS-BASCO', '2026-10-07', opener=fake)
        self.assertEqual(fake.calls, [])

    def test_not_found_and_not_configured(self):
        with patch.dict(os.environ, ENV):
            with self.assertRaises(sharepoint.SharePointNotFound):
                sharepoint.fetch_export('NOPE-JOB-1', '2026-10-07', opener=FakeGraph({}), site_id='site-exact', list_id='missing')
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(sharepoint.configured())
            with self.assertRaises(sharepoint.SharePointNotConfigured):
                sharepoint.fetch_export('A-B-1', '2026-10-07')

    def test_forbidden_site_is_explained_without_secrets(self):
        def deny(request, timeout=None):
            if 'oauth2' in request.full_url:
                return io.BytesIO(json.dumps({'access_token': 'tok', 'expires_in': 3600}).encode())
            raise urllib.error.HTTPError(request.full_url, 403, 'forbidden', {}, None)
        with patch.dict(os.environ, ENV), self.assertRaises(sharepoint.SharePointError) as caught:
            sharepoint.fetch_export('2230-OWENS-BASCO', '2026-10-07', opener=deny, site_id='site-exact', list_id='list-exact')
        self.assertIn('not allowed', str(caught.exception))
        self.assertNotIn('very-secret-value', str(caught.exception))


class InboxCompareTests(unittest.TestCase):
    def test_compare_saves_a_revision_and_keeps_originals(self):
        with tempfile.TemporaryDirectory() as root:
            inbox = SpreadsheetInbox(root)
            report = build()
            item = inbox.ingest('2230-OWENS-BASCO - EOS - 10.07.26.xlsx', report)
            result = inbox.compare_source(item['file_hash'], '2230-OWENS-BASCO 2026-10-07.csv', source_csv(), {'origin': 'uploaded'})
            self.assertEqual(result['sharepoint']['status'], 'match')
            self.assertEqual(result['source_reconciliation_status'], 'match')
            self.assertEqual(inbox.get(item['file_hash'])['sharepoint']['matched_rows'], 13)
            changed = list(ROWS); changed[0] = changed[0][:3] + (9,) + changed[0][4:]
            result = inbox.compare_source(item['file_hash'], 'x.csv', source_csv(changed), {'origin': 'uploaded'})
            self.assertEqual(result['source_reconciliation_status'], 'mismatch')
            self.assertEqual(result['internal_check_status'], 'findings')

    def test_recheck_keeps_and_reruns_the_comparison(self):
        with tempfile.TemporaryDirectory() as root:
            inbox = SpreadsheetInbox(root)
            item = inbox.ingest('r.xlsx', build())
            inbox.compare_source(item['file_hash'], '2230-OWENS-BASCO 2026-10-07.csv', source_csv(), {'origin': 'sharepoint', 'site': 'BriggsStratton_Auburn'})
            with patch('hosting.spreadsheet_inbox.CHECKER_VERSION', 'future-version'), patch('hosting.spreadsheets.CHECKER_VERSION', 'future-version'):
                again = inbox.apply_recheck(item['file_hash'], 'Chris')
            self.assertEqual(again['checker_version'], 'future-version')
            self.assertEqual(again['sharepoint']['status'], 'match')
            self.assertEqual(again['sharepoint']['site'], 'BriggsStratton_Auburn')

    def test_lookup_status_is_recorded(self):
        with tempfile.TemporaryDirectory() as root:
            inbox = SpreadsheetInbox(root)
            item = inbox.ingest('r.xlsx', build())
            result = inbox.record_lookup(item['file_hash'], 'not_configured', 'SharePoint lookup is not set up yet.', 'A-B-1', '2026-10-07')
            self.assertEqual(result['sharepoint']['status'], 'not_configured')


if __name__ == '__main__':
    unittest.main()


class RouteTests(unittest.TestCase):
    """POST /api/spreadsheets/source-upload and /api/spreadsheets/sharepoint over HTTP."""
    def setUp(self):
        from api.index import handler
        from legacy_storage_fake import PostgresWorkspace
        from test_vercel import FakeDatabase
        self.env = patch.dict(os.environ, {'CI_LOCAL_DEV': '1', 'VERCEL': '0'}); self.env.start()
        for key in ('MS_TENANT_ID', 'MS_CLIENT_ID', 'MS_CLIENT_SECRET'):
            os.environ.pop(key, None)
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), handler)
        self.server.workspace_store = PostgresWorkspace('postgresql://test.invalid/x', FakeDatabase().connect)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True); self.thread.start()
        self.base = 'http://127.0.0.1:' + str(self.server.server_port)

    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join(); self.env.stop()

    def post(self, path, body):
        req = urllib.request.Request(self.base + path, data=json.dumps(body).encode(),
                                     headers={'Content-Type': 'application/json', 'Origin': self.base, 'X-Review-Token': 'same-origin'})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_upload_export_and_lookup_not_configured(self):
        code, res = self.post('/api/spreadsheets/import', {'filename': '2230-OWENS-BASCO - EOS - 10.07.26.xlsx', 'content_base64': base64.b64encode(build()).decode()})
        self.assertEqual(code, 200, res)
        file_hash = res['item']['file_hash']
        code, res = self.post('/api/spreadsheets/sharepoint', {'file_hash': file_hash})
        self.assertEqual(code, 200, res)
        self.assertEqual(res['item']['sharepoint']['status'], 'error')
        self.assertIn('assignment', res['item']['sharepoint']['message'].lower())
        with patch.object(SpreadsheetInbox, 'assignment_for', return_value=dict(status='source', site_id='site-exact', list_id='list-exact')):
            code, res = self.post('/api/spreadsheets/sharepoint', {'file_hash': file_hash})
        self.assertEqual(code, 200, res)
        self.assertEqual(res['item']['sharepoint']['status'], 'not_configured')
        self.assertEqual((res['item']['sharepoint']['job'], res['item']['sharepoint']['day']), ('2230-OWENS-BASCO', '2026-10-07'))
        code, res = self.post('/api/spreadsheets/source-upload', {'file_hash': file_hash, 'filename': '2230-OWENS-BASCO 2026-10-07.csv',
                                                                   'content_base64': base64.b64encode(source_csv()).decode()})
        self.assertEqual(code, 200, res)
        self.assertEqual(res['item']['sharepoint']['status'], 'match')
        self.assertEqual(res['item']['sharepoint']['origin'], 'uploaded')
        code, res = self.post('/api/spreadsheets/sharepoint', {'file_hash': 'zz'})
        self.assertEqual(code, 409)

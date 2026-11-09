import io, json, os, unittest, urllib.error
from unittest.mock import patch

from hosting import sharepoint
from hosting.source_compare import parse_export

JOB = 'HANDS-HMMA-2219'


def item(i, day, vin, lf='OK', susp=0, hour=14):
    return {'id': str(i), 'createdDateTime': f'{day}T{hour:02d}:00:00Z', 'lastModifiedDateTime': f'{day}T{hour:02d}:05:00Z',
            'fields': {'Title': vin, 'VIN': vin, 'Date': ('2026-10-05' if day == '2026-10-06' and hour == 3 else day), 'Date0': f'{day}T{hour:02d}:00:00Z', 'QuantityInspected': 8, 'LFCondition': lf,
                       'RFCondition': 'OK', 'SuspectQty': susp, 'AuditorLookupId': '12', '@odata.etag': 'x', 'Modified': 'y'}}


ITEMS = [item(1, '2026-10-02', 'HZA 250001'), item(2, '2026-10-05', 'HZA 259059'), item(3, '2026-10-05', 'HZA 258989', 'SUSPECT', 1),
         item(4, '2026-10-06', 'HZA 260000', hour=3),  # 03:00Z = Oct 5 evening in Chicago
         item(5, '2026-10-01', 'HZA 249999')]
COLS = [{'name': 'Title', 'displayName': 'Title'}, {'name': 'VIN', 'displayName': 'VIN Inspected'}, {'name': 'Date', 'displayName': 'Date', 'dateTime': {'format': 'dateOnly'}},
        {'name': 'QuantityInspected', 'displayName': 'Quantity Inspected'}, {'name': 'LFCondition', 'displayName': 'LF Condition'},
        {'name': 'RFCondition', 'displayName': 'RF Condition'}, {'name': 'SuspectQty', 'displayName': 'Suspect Quantity'},
        {'name': 'Modified', 'displayName': 'Modified', 'readOnly': True}, {'name': 'Hidden1', 'displayName': 'x', 'hidden': True}]


class FakeGraph:
    def __init__(self):
        self.calls = []

    def __call__(self, request, timeout=None):
        url = request.full_url
        self.calls.append(url)
        if 'login.microsoftonline.com' in url:
            return io.BytesIO(json.dumps({'access_token': 't', 'expires_in': 3600}).encode())
        if '/sites/ppgquality.sharepoint.com:/sites/' in url:
            return io.BytesIO(json.dumps({'id': 'site-' + url.rsplit('/', 1)[1]}).encode())
        if url.endswith('/lists?$select=id,displayName,webUrl,lastModifiedDateTime&$top=500'):
            lists = [{'id': 'L1', 'displayName': f'Data #{JOB}', 'webUrl': 'https://x/lists/data'}] if 'site-Hyundai_Montgomery' in url else [{'id': 'L0', 'displayName': 'Documents'}]
            return io.BytesIO(json.dumps({'value': lists}).encode())
        if url.endswith('/sites/site-Hyundai_Montgomery/lists/L1'):
            return io.BytesIO(json.dumps({'id': 'L1', 'displayName': f'Data #{JOB}', 'webUrl': 'https://x/sites/Hyundai_Montgomery/Lists/data'}).encode())
        if '/lists/L1/columns' in url:
            return io.BytesIO(json.dumps({'value': COLS}).encode())
        if url.endswith('/lists/L1/items?$expand=fields&$top=999'):
            return io.BytesIO(json.dumps({'value': ITEMS[:3], '@odata.nextLink': 'https://graph.microsoft.com/v1.0/sites/site-Hyundai_Montgomery/lists/L1/items?$skiptoken=page2'}).encode())
        if url == 'https://graph.microsoft.com/v1.0/sites/site-Hyundai_Montgomery/lists/L1/items?$skiptoken=page2':
            return io.BytesIO(json.dumps({'value': ITEMS[3:]}).encode())
        raise urllib.error.HTTPError(url, 404, 'nf', {}, io.BytesIO(b''))


class ListFallbackTests(unittest.TestCase):
    def setUp(self):
        sharepoint._token.update(value=None, expires=0); sharepoint._site_ids.clear(); sharepoint._list_cache.clear()
        self.env = patch.dict(os.environ, {'MS_TENANT_ID': 't', 'MS_CLIENT_ID': 'c', 'MS_CLIENT_SECRET': 's',
                                           'SHAREPOINT_SITES': 'BriggsStratton_Auburn,Hyundai_Montgomery'}); self.env.start()

    def tearDown(self):
        self.env.stop(); sharepoint._list_cache.clear(); sharepoint._token.update(value=None, expires=0); sharepoint._site_ids.clear()

    def test_day_comes_from_the_data_list_when_no_export_exists(self):
        fake = FakeGraph()
        name, raw, meta = sharepoint.fetch_export(JOB, '2026-10-05', opener=fake, site_id='site-Hyundai_Montgomery', list_id='L1')
        self.assertEqual(meta['source_kind'], 'list'); self.assertEqual(meta['site'], 'Hyundai_Montgomery'); self.assertEqual(meta['rows'], 3)
        text = raw.decode()
        self.assertTrue(text.startswith('Title,VIN Inspected,Date,Quantity Inspected,LF Condition,RF Condition,Suspect Quantity'))
        self.assertNotIn('Modified', text.splitlines()[0]); self.assertNotIn('Lookup', text.splitlines()[0])
        rows = parse_export(name, raw)
        self.assertEqual(sorted(r['labels'].get('identifier') for r in rows), ['HZA 258989', 'HZA 259059', 'HZA 260000'])
        self.assertEqual(sum(r['values']['rejected'] for r in rows), 1)

    def test_history_from_the_list_and_cache(self):
        fake = FakeGraph()
        hist = sharepoint.recent_exports(JOB, '2026-10-05', limit=5, opener=fake, site_id='site-Hyundai_Montgomery', list_id='L1')
        self.assertEqual([d for d, _, _ in hist], ['2026-10-02', '2026-10-01'])
        before = len(fake.calls)
        sharepoint.list_day(JOB, '2026-10-05', opener=fake, site_id='site-Hyundai_Montgomery', list_id='L1')
        self.assertGreater(len(fake.calls), before)  # custom openers deliberately bypass shared auth-context cache

    def test_missing_identity_or_assigned_list_fails_closed(self):
        with self.assertRaisesRegex(sharepoint.SharePointError, 'verified site_id and list_id'):
            sharepoint.fetch_export('NO-SUCH-JOB', '2026-10-05', opener=FakeGraph())
        with self.assertRaisesRegex(sharepoint.SharePointNotFound, 'assigned'):
            sharepoint.fetch_export(JOB, '2026-10-05', opener=FakeGraph(), site_id='site-Hyundai_Montgomery', list_id='missing')


if __name__ == '__main__':
    unittest.main()


class ListNameMatchTests(unittest.TestCase):
    def test_job_number_first_or_last_matches(self):
        from hosting.sharepoint import _list_match
        self.assertEqual(_list_match('HANDS-HMMA-2219', 'Data #2219-HANDS-HMMA'), 2)
        self.assertEqual(_list_match('HANDS-HMMA-2219', 'Data #HANDS-HMMA-2219'), 2)
        self.assertEqual(_list_match('2230-OWENS-BASCO', 'Data #2230-OWENS-BASCO'), 2)
        self.assertEqual(_list_match('HANDS-HMMA-2219', 'Data #2219 HMMA'), 1)
        self.assertEqual(_list_match('HANDS-HMMA-2219', 'Data #2230-OWENS-BASCO'), 0)
        self.assertEqual(_list_match('HANDS-HMMA-2219', 'Data 2219HANDSHMMA'), 2)
        self.assertEqual(_list_match('HANDS-HMMA-2219', 'Something', 'https://ppgquality.sharepoint.com/sites/Hyundai_Montgomery/Lists/Data%202219HANDSHMMA'), 2)
        self.assertEqual(_list_match('HANDS-HMMA-2219', 'Documents'), 0)
        self.assertEqual(_list_match('HANDS-HMMA-2219', '2219 Site Pages'), 0)


class Discovery(unittest.TestCase):
    """Every client site on SharePoint shows up, but tests and demos never do."""
    SITES = [dict(id='s1', name='Hyundai_Montgomery', displayName='Hyundai Montgomery'),
             dict(id='s2', name='Lear_Hammond', displayName='Lear Hammond'),
             dict(id='s3', name='TestSite', displayName='Test site'),
             dict(id='s4', name='Demo_Client', displayName='Demo client'),
             dict(id='s5', name='PPGQualityHR', displayName='HR'),           # no job lists: not a client
             dict(id='s6', name='Owens_Basco', displayName='Owens (training copy)')]

    def setUp(self):
        sharepoint._token.update(value=None, expires=0); sharepoint._site_ids.clear()
        self.env = patch.dict(os.environ, {'MS_TENANT_ID': 't', 'MS_CLIENT_ID': 'c', 'MS_CLIENT_SECRET': 's', 'SHAREPOINT_SITES': ''}); self.env.start()

    def tearDown(self):
        self.env.stop(); sharepoint._token.update(value=None, expires=0)

    def fake(self, request, timeout=None):
        url = request.full_url
        if 'login.microsoftonline.com' in url:
            return io.BytesIO(json.dumps({'access_token': 't', 'expires_in': 3600}).encode())
        if '/sites?search=' in url:
            return io.BytesIO(json.dumps({'value': self.SITES}).encode())
        if '/lists?' in url:
            sid = url.split('/sites/')[1].split('/')[0]
            lists = [] if sid == 's5' else [{'id': 'L' + sid, 'displayName': f'Data #JOB-{sid}-2219'}]
            return io.BytesIO(json.dumps({'value': lists}).encode())
        raise urllib.error.HTTPError(url, 404, 'nf', {}, io.BytesIO(b''))

    def test_finds_clients_and_skips_tests_and_demos(self):
        self.assertTrue(sharepoint._is_test('TestSite')); self.assertTrue(sharepoint._is_test('Demo_Client'))
        self.assertFalse(sharepoint._is_test('Contest_Motors')); self.assertFalse(sharepoint._is_test('Hyundai_Montgomery'))
        names = [n for n, _, _ in sharepoint.client_sites(self.fake, refresh=True)]
        self.assertEqual(sorted(names), ['Hyundai_Montgomery', 'Lear_Hammond'])
        keys = sorted(c['key'] for c in sharepoint.clients(self.fake, refresh=True))
        self.assertEqual(keys, ['Hyundai', 'Lear'])

import io, json, os, unittest, urllib.error
from unittest.mock import patch

from hosting import sharepoint
from hosting.source_compare import parse_export

JOB = 'HANDS-HMMA-2219'


def item(i, day, vin, lf='OK', susp=0, hour=14):
    return {'id': str(i), 'createdDateTime': f'{day}T{hour:02d}:00:00Z', 'lastModifiedDateTime': f'{day}T{hour:02d}:05:00Z',
            'fields': {'Title': vin, 'VIN': vin, 'Date0': f'{day}T{hour:02d}:00:00Z', 'QuantityInspected': 8, 'LFCondition': lf,
                       'RFCondition': 'OK', 'SuspectQty': susp, 'AuditorLookupId': '12', '@odata.etag': 'x', 'Modified': 'y'}}


ITEMS = [item(1, '2026-10-02', 'HZA 250001'), item(2, '2026-10-05', 'HZA 259059'), item(3, '2026-10-05', 'HZA 258989', 'SUSPECT', 1),
         item(4, '2026-10-06', 'HZA 260000', hour=3),  # 03:00Z = Oct 5 evening in Chicago
         item(5, '2026-10-01', 'HZA 249999')]
COLS = [{'name': 'Title', 'displayName': 'Title'}, {'name': 'VIN', 'displayName': 'VIN Inspected'}, {'name': 'Date0', 'displayName': 'Date'},
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
        if url.endswith('/lists?$select=id,displayName,webUrl&$top=500'):
            lists = [{'id': 'L1', 'displayName': f'Data #{JOB}', 'webUrl': 'https://x/lists/data'}] if 'site-Hyundai_Montgomery' in url else [{'id': 'L0', 'displayName': 'Documents'}]
            return io.BytesIO(json.dumps({'value': lists}).encode())
        if '/lists/L1/columns' in url:
            return io.BytesIO(json.dumps({'value': COLS}).encode())
        if url.endswith('/lists/L1/items?$expand=fields&$top=999'):
            return io.BytesIO(json.dumps({'value': ITEMS[:3], '@odata.nextLink': 'https://graph.microsoft.com/v1.0/page2'}).encode())
        if url.endswith('/page2'):
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
        name, raw, meta = sharepoint.fetch_export(JOB, '2026-10-05', opener=fake)
        self.assertEqual(meta['source_kind'], 'list'); self.assertEqual(meta['site'], 'Hyundai_Montgomery'); self.assertEqual(meta['rows'], 3)
        text = raw.decode()
        self.assertTrue(text.startswith('Title,VIN Inspected,Date,Quantity Inspected,LF Condition,RF Condition,Suspect Quantity'))
        self.assertNotIn('Modified', text.splitlines()[0]); self.assertNotIn('Lookup', text.splitlines()[0])
        rows = parse_export(name, raw)
        self.assertEqual(sorted(r['labels'].get('identifier') for r in rows), ['HZA 258989', 'HZA 259059', 'HZA 260000'])
        self.assertEqual(sum(r['values']['rejected'] for r in rows), 1)

    def test_history_from_the_list_and_cache(self):
        fake = FakeGraph()
        hist = sharepoint.recent_exports(JOB, '2026-10-05', limit=5, opener=fake)
        self.assertEqual([d for d, _, _ in hist], ['2026-10-02', '2026-10-01'])
        before = len(fake.calls)
        sharepoint.list_day(JOB, '2026-10-05', opener=fake)
        self.assertEqual(len(fake.calls), before)  # items reused from the short cache

    def test_not_found_message_names_the_list(self):
        with self.assertRaises(sharepoint.SharePointNotFound) as ctx:
            sharepoint.fetch_export('NO-SUCH-JOB', '2026-10-05', opener=FakeGraph())
        self.assertIn('"Data #NO-SUCH-JOB" list', str(ctx.exception))


if __name__ == '__main__':
    unittest.main()

import copy
import unittest
from hosting import reconcile, sheet_roles
from test_r8_count_contract import fixture, compare

class SourceURLIdentity(unittest.TestCase):
    def url_records(self):
        records,snap=fixture()
        snap['list']['url']='https://tenant.sharepoint.com/sites/Plant/Lists/Data'
        for r in records:r['source']=dict(site='Plant',list='Data',item=r['source']['item'],url=f'https://tenant.sharepoint.com/sites/Plant/Lists/Data/{r["source"]["item"]}_.000')
        return records,snap
    def test_matching_complete_links_verify_and_origin_is_preserved(self):
        records,snap=self.url_records();self.assertTrue(compare(records,snap)['verified'])
        parsed=sheet_roles.source_url_identity(records[0]['source']['url'])
        self.assertEqual(parsed['origin'],'https://tenant.sharepoint.com');self.assertEqual(parsed['list_path'],'/sites/plant/lists/data')
        self.assertEqual(sheet_roles.parse_source_url(records[0]['source']['url']),('Plant','Data','1'))
    def test_foreign_host_missing_list_and_conflicting_ids_hold(self):
        for mutate in (
            lambda r,s:r[0]['source'].update(url=r[0]['source']['url'].replace('tenant.','other.')),
            lambda r,s:s['list'].update(url='https://tenant.sharepoint.com/sites/Plant'),
            lambda r,s:r[0]['source'].update(site_id='different',list_id='lid'),
            lambda r,s:r[0]['source'].update(item='2'),
            lambda r,s:r[0]['source'].pop('url')):
            records,snap=self.url_records();mutate(records,snap);self.assertFalse(compare(records,snap)['verified'])
    def test_embedded_urls_fragments_credentials_and_encoded_paths_rejected(self):
        base='https://tenant.sharepoint.com/sites/Plant/Lists/Data/1_.000'
        for url in ('text '+base, 'https://evil.test/?url='+base,base+'#fragment',base.replace('https://','https://user@'),
                    base.replace('Plant','Plant%2fExtra'),base.replace('/Data/','/Data/%2e%2e/'),base.replace('Data','%2544ata')):
            with self.subTest(url=url):self.assertIsNone(sheet_roles.parse_source_url(url))
    def test_dispform_item_id_must_agree(self):
        records,snap=self.url_records();records[0]['source']['url']='https://tenant.sharepoint.com/sites/Plant/Lists/Data/DispForm.aspx?ID=1'
        self.assertTrue(compare(records,snap)['verified'])
        records[0]['source']['url']+='&ID=2';self.assertFalse(compare(records,snap)['verified'])
    def test_unbound_export_cannot_become_verified_from_names_only(self):
        records,snap=self.url_records();export=copy.deepcopy(records[0]);export['source'].pop('url')
        result=reconcile.compare('2026-11-07',records,[export],snap)
        self.assertFalse(result['verified']);self.assertIn('export_source_unconfirmed',[r['status'] for r in result['rows']])

    def test_unconfirmed_identity_never_offers_authoritative_corrections(self):
        records,snap=self.url_records();records[0]['values']['inspected']=99;records[0]['source'].pop('url')
        result=compare(records,snap)
        self.assertFalse(result['verified']);self.assertTrue(any(r['status']=='value_differs' for r in result['rows']))
        self.assertFalse(result['proposed_corrections'])
    def test_embedded_export_fill_requires_exact_origin_and_unique_item(self):
        from unittest.mock import patch
        from hosting.fixes import proposals
        from hosting.spreadsheets import inspect_spreadsheet
        from tests.sl_daily_fixture import sl_daily
        from hosting.spreadsheets import report_and_export_records
        import io
        from openpyxl import load_workbook
        workbook=load_workbook(io.BytesIO(sl_daily()));workbook['Inspection Detail']['J5']=None
        buffer=io.BytesIO();workbook.save(buffer);raw=buffer.getvalue();result=inspect_spreadsheet('r.xlsx',raw)
        records,exports=report_and_export_records('r.xlsx',raw)
        self.assertTrue(any(p.get('snapshot_id','').startswith('export:') for p in proposals(result,'r.xlsx',raw)))
        bad=copy.deepcopy(exports)
        for e in bad:e['source']['url']=e['source']['url'].replace('ppg.sharepoint.com','other.sharepoint.com')
        with patch('hosting.spreadsheets.report_and_export_records',return_value=(records,bad)):
            self.assertFalse(any(p.get('snapshot_id','').startswith('export:') for p in proposals(result,'r.xlsx',raw)))
        duplicated=exports+copy.deepcopy(exports)
        with patch('hosting.spreadsheets.report_and_export_records',return_value=(records,duplicated)):
            self.assertFalse(any(p.get('snapshot_id','').startswith('export:') for p in proposals(result,'r.xlsx',raw)))

import importlib.util
import os
from pathlib import Path
import unittest
from unittest.mock import patch

spec=importlib.util.spec_from_file_location('sharepoint', Path(__file__).parents[1]/'hosting/sharepoint.py')
sp=importlib.util.module_from_spec(spec);spec.loader.exec_module(sp)

class SourceIntegrity(unittest.TestCase):
    def setUp(self): sp._list_cache.clear()
    def test_ambiguous_job_does_not_select_latest(self):
        sites=[('SL','sl',[dict(id='a',displayName='Data #2207',lastModifiedDateTime='2020')]),('HMMA','hmma',[dict(id='b',displayName='Data #2207',lastModifiedDateTime='2026')])]
        with patch.object(sp,'client_sites',return_value=sites):
            with self.assertRaisesRegex(sp.SharePointError,'Ambiguous'): sp._find_list('2207')
    def test_exact_binding_never_searches_other_sites(self):
        with patch.object(sp,'_get',return_value={'id':'b'}),patch.object(sp,'client_sites',side_effect=AssertionError('fallback')):
            self.assertEqual(sp._find_list('2207',site_id='hmma',list_id='b')[1:],('hmma',{'id':'b'}))
    def test_missing_exact_source_does_not_fallback(self):
        with patch.object(sp,'_get',return_value=None):
            with self.assertRaises(sp.SharePointNotFound):sp._find_list('2207',site_id='sl',list_id='a')
    def test_partial_identity_rejected(self):
        with self.assertRaises(sp.SharePointError):sp._find_list('2207',site_id='sl')
    def test_columns_and_items_paginated_and_capture_evidence(self):
        pages=[{'value':[{'name':'Date','dateTime':{'format':'dateOnly'}}],'@odata.nextLink':'/sites/sl/lists/a/columns?$skiptoken=2'}, {'value':[{'name':'Shift'}]}, {'value':[{'id':'1'}],'@odata.nextLink':'/sites/sl/lists/a/items?$skiptoken=2'}, {'value':[{'id':'2'}]}]
        with patch.object(sp,'_find_list',return_value=('SL','sl',{'id':'a'})),patch.object(sp,'_get',side_effect=pages):
            val=sp._list_items('2207')
        self.assertEqual(set(val[2]),{'Date','Shift'});self.assertEqual(len(val[3]),2)
        self.assertTrue(val[1]['_capture']['terminal']);self.assertEqual(val[1]['_capture']['site_id'],'sl')
        self.assertIn('dateTime',val[1]['_capture']['columns'][0])
    def test_cache_separates_sites_and_not_job_text(self):
        with patch.object(sp,'_find_list',side_effect=[('SL','sl',{'id':'a'}),('HMMA','hmma',{'id':'b'})]),patch.object(sp,'_get',side_effect=[{'value':[]},{'value':[{'id':'1'}]},{'value':[]},{'value':[{'id':'2'}]}]):
            self.assertEqual(sp._list_items('2207')[3][0]['id'],'1')
            self.assertEqual(sp._list_items('2207')[3][0]['id'],'2')
        self.assertEqual(len(sp._list_cache),2)
    def test_cap_does_not_return_partial(self):
        with patch.object(sp,'_get',return_value={'value':[1,2],'@odata.nextLink':'/next'}):
            with self.assertRaisesRegex(sp.SharePointError,'safety limit'):sp._collection('/start',cap=2)
    def test_exact_cap_terminal_is_complete(self):
        with patch.object(sp,'_get',return_value={'value':[1,2]}):self.assertEqual(sp._collection('/start',cap=2),[1,2])
    def test_failed_page_repeated_cursor_and_foreign_url_rejected(self):
        for page in [None,{}, {'value':[],'@odata.nextLink':'/start'}, {'value':[],'@odata.nextLink':'https://evil.example/a'}, {'value':[],'@odata.nextLink':''}]:
            with self.subTest(page=page),patch.object(sp,'_get',return_value=page):
                with self.assertRaises(sp.SharePointError):sp._collection('/start')
    def test_duplicate_item_ids_rejected_not_silently_removed(self):
        with patch.object(sp,'_find_list',return_value=('SL','sl',{'id':'a'})),patch.object(sp,'_get',side_effect=[{'value':[]},{'value':[{'id':'1'},{'id':'1'}]}]):
            with self.assertRaisesRegex(sp.SharePointError,'repeated'):sp._list_items('2207')
        self.assertFalse(sp._list_cache)
    def test_bound_export_routes_only_to_exact_list(self):
        with patch.object(sp,'configured',return_value=True),patch.object(sp,'list_day',return_value=None) as get,patch.object(sp,'_sites',side_effect=AssertionError('unbound export')):
            with self.assertRaises(sp.SharePointNotFound):sp.fetch_export('2207','2026-11-07',site_id='sl',list_id='a')
        self.assertEqual(get.call_args.kwargs['site_id'],'sl')

if __name__=='__main__':unittest.main()

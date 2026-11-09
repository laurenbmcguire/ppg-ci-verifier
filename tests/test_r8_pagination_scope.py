"""Never stamp unrelated Graph pages with the requested source identity."""
import unittest
from unittest.mock import patch
from hosting import sharepoint, reconcile

PATH='/sites/EXPECTED/lists/RIGHT/items'
class PaginationScope(unittest.TestCase):
    def test_same_collection_query_continuation_accepts_all_records(self):
        urls=[PATH+'?$skiptoken=two','https://graph.microsoft.com/v1.0'+PATH+'?$skiptoken=two','https://graph.microsoft.com/v1.0/sites/%45XPECTED/lists/RIGHT/items?$skiptoken=two']
        for url in urls:
            with self.subTest(url=url):
                pages=[{'value':[{'id':'1'}],'@odata.nextLink':url},{'value':[{'id':'2'}]}]
                with patch.object(sharepoint,'_get',side_effect=pages) as get:
                    self.assertEqual(len(sharepoint._collection(PATH)),2);self.assertEqual(get.call_count,2)
                cov=dict(failed_pages=0,notes=[],pages=0)
                with patch.object(reconcile,'_fetch',side_effect=[(200,p) for p in pages]):
                    self.assertEqual(len(reconcile._pages(PATH,cov)),2)
                self.assertTrue(cov['items_terminal']);self.assertEqual(cov['failed_pages'],0)
    def test_wrong_or_ambiguous_collection_never_fetched(self):
        bad=[PATH.replace('RIGHT','WRONG'),PATH.replace('EXPECTED','OTHER'),PATH.replace('/items','/columns'),
             'https://graph.microsoft.com/v1.0/sites/OTHER/lists/WRONG/items',
             'https://user@graph.microsoft.com/v1.0'+PATH,
             'https://graph.microsoft.com/v1.0'+PATH+'#fragment',
             '/sites/EXPECTED%2flists/RIGHT/items','/sites/EXPECTED/lists/RIGHT/%2e%2e/items',
             '/sites/EXPECTED/lists/RIGHT/%2569tems','/sites/EXPECTED/lists/RIGHT%5citems',
             '//graph.microsoft.com/v1.0'+PATH,'https://graph.microsoft.com.evil.test/v1.0'+PATH]
        for url in bad:
            with self.subTest(url=url):
                first={'value':[{'id':'1'}],'@odata.nextLink':url}
                with patch.object(sharepoint,'_get',return_value=first) as get:
                    with self.assertRaises(sharepoint.SharePointError):sharepoint._collection(PATH)
                    self.assertEqual(get.call_count,1)
                cov=dict(failed_pages=0,notes=[],pages=0)
                with patch.object(reconcile,'_fetch',return_value=(200,first)) as fetch:
                    rows=reconcile._pages(PATH,cov);self.assertEqual(fetch.call_count,1)
                self.assertEqual(rows,[{'id':'1'}]);self.assertEqual(cov['failed_pages'],1);self.assertFalse(cov['items_terminal'])
    def test_discovery_lists_also_pin_collection(self):
        with patch.object(sharepoint,'_get',return_value={'value':[],'@odata.nextLink':'https://graph.microsoft.com/v1.0/sites/OTHER/lists'}) as get:
            with self.assertRaises(sharepoint.SharePointError):sharepoint._site_lists('EXPECTED')
            self.assertEqual(get.call_count,1)

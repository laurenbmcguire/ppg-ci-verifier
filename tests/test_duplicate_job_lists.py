import unittest
from unittest import mock

from hosting import sharepoint
from hosting.access import client_of


class DuplicateJobLists(unittest.TestCase):
    """One job list on two client sites (SL Alabama's Alexander City site and the Hyundai Montgomery site):
    only an explicitly selected site/list identity authorizes the source."""

    def test_newest_list_is_not_source_authority(self):
        sites = [('Hyundai_Montgomery', 's1', [dict(id='a', displayName='Data #2207-SLALAL-CJAL', webUrl='x', lastModifiedDateTime='2026-09-01T00:00:00Z')]),
                 ('SLAlabama_AlexanderCity', 's2', [dict(id='b', displayName='Data #2207-SLALAL-CJAL', webUrl='y', lastModifiedDateTime='2026-11-06T22:00:00Z')])]
        with mock.patch.object(sharepoint, 'client_sites', return_value=sites):
            with self.assertRaisesRegex(sharepoint.SharePointError, 'Ambiguous'):
                sharepoint._find_list('2207-SLALAL-CJAL')
        selected = dict(sites[1][2][0], webUrl='https://x/sites/SLAlabama_AlexanderCity/Lists/b')
        with mock.patch.object(sharepoint, '_get', return_value=selected) as get:
            name, site_id, lst = sharepoint._find_list('2207-SLALAL-CJAL', site_id='s2', list_id='b')
        self.assertEqual((name, site_id, lst['id']), ('SLAlabama_AlexanderCity', 's2', 'b'))
        get.assert_called_once_with('/sites/s2/lists/b', None)

    def test_client_follows_the_active_list(self):
        clients = [dict(key='Hyundai', jobs=[dict(job='2207-SLALAL-CJAL', numbers=['2207'], modified='2026-09-01T00:00:00Z')]),
                   dict(key='SLAlabama', jobs=[dict(job='2207-SLALAL-CJAL', numbers=['2207'], modified='2026-11-06T22:00:00Z')])]
        self.assertEqual(client_of('2207', clients), 'SLAlabama')
        self.assertEqual(client_of('2207-SLALAL-CJAL', clients), 'SLAlabama')
        self.assertEqual(client_of('9999', clients), None)

import unittest
from unittest import mock

from hosting import sharepoint
from hosting.access import client_of


class DuplicateJobLists(unittest.TestCase):
    """One job list on two client sites (SL Alabama's Alexander City site and the Hyundai Montgomery site):
    the list the tablets are writing to is the one used."""

    def test_most_recently_changed_list_wins(self):
        sites = [('Hyundai_Montgomery', 's1', [dict(id='a', displayName='Data #2207-SLALAL-CJAL', webUrl='x', lastModifiedDateTime='2026-09-01T00:00:00Z')]),
                 ('SLAlabama_AlexanderCity', 's2', [dict(id='b', displayName='Data #2207-SLALAL-CJAL', webUrl='y', lastModifiedDateTime='2026-11-06T22:00:00Z')])]
        with mock.patch.object(sharepoint, 'client_sites', return_value=sites):
            name, site_id, lst = sharepoint._find_list('2207-SLALAL-CJAL')
        self.assertEqual((name, lst['id']), ('SLAlabama_AlexanderCity', 'b'))

    def test_client_follows_the_active_list(self):
        clients = [dict(key='Hyundai', jobs=[dict(job='2207-SLALAL-CJAL', numbers=['2207'], modified='2026-09-01T00:00:00Z')]),
                   dict(key='SLAlabama', jobs=[dict(job='2207-SLALAL-CJAL', numbers=['2207'], modified='2026-11-06T22:00:00Z')])]
        self.assertEqual(client_of('2207', clients), 'SLAlabama')
        self.assertEqual(client_of('2207-SLALAL-CJAL', clients), 'SLAlabama')
        self.assertEqual(client_of('9999', clients), None)

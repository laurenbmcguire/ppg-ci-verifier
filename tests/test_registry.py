"""Client/job assignment: a job label on two client sites never decides the client by itself."""
import tempfile
import unittest

from hosting import registry
from hosting.spreadsheet_inbox import SpreadsheetInbox
from tests.sl_daily_fixture import sl_daily

SL_LIST = '32a42ee0-4592-4d73-b893-1b98970b3d4b'
HY_LIST = '47f89ceb-d8b9-45c3-932a-66d46e3f4272'
# Hyundai sorts first and its 2207 list was modified most recently: the two traps the old job-number lookup fell into.
SNAPSHOT = [
    dict(client='Hyundai', key='Hyundai', sites=[dict(site='Hyundai_Montgomery', location='Montgomery')],
         jobs=[dict(job='2207', numbers=['2207'], site='Hyundai_Montgomery', list_id=HY_LIST, modified='2026-11-07T10:00:00Z'),
               dict(job='2300', numbers=['2300'], site='Hyundai_Montgomery', list_id='h-2300')]),
    dict(client='SL Alabama', key='SLAlabama', sites=[dict(site='SLAlabama_AlexanderCity', location='Alexander City')],
         jobs=[dict(job='2207', numbers=['2207'], site='SLAlabama_AlexanderCity', list_id=SL_LIST, modified='2026-11-01T10:00:00Z'),
               dict(job='2214', numbers=['2214'], site='SLAlabama_AlexanderCity', list_id='d1a655b8-dc19-4d0f-bad2-35ed9999ccc2'),   # the list now pinned in the registry
               dict(job='2300', numbers=['2300'], site='SLAlabama_AlexanderCity', list_id='s-2300')]),
]
REG = registry.entries([])


def resolve(filename, job, result=None, manual=None):
    return registry.resolve(result or {}, filename, job, '2026-11-06', REG, SNAPSHOT, manual)


class DuplicateJobNumbers(unittest.TestCase):
    def test_sl_2207_goes_to_the_alexander_city_list(self):
        a = resolve('SL Alabama - 2207 - 2026-11-06.xlsx', '2207')
        self.assertEqual((a['status'], a['client_key'], a['site'], a['list_id']), ('verified', 'SLAlabama', 'SLAlabama_AlexanderCity', SL_LIST))

    def test_hyundai_list_never_overrides_2207_without_evidence(self):
        a = resolve('PPG_2207_Nov06_Provisional.xlsx', '2207')
        self.assertEqual((a['client_key'], a['list_id']), ('SLAlabama', SL_LIST))
        self.assertNotEqual(a['list_id'], HY_LIST)

    def test_a_filename_alone_cannot_move_2207_to_the_hyundai_list(self):
        a = resolve('Hyundai HMMA - 2207 - 2026-11-06.xlsx', '2207')
        self.assertEqual(a['status'], 'ambiguous')   # a person decides; the report stays on hold
        self.assertIsNone(a.get('client_key'))

    def test_hyundai_source_urls_do_prove_the_hyundai_list(self):
        result = {'dataset_selection': {'source_lists': [{'site': 'Hyundai_Montgomery', 'list': 'Data 2207'}]}}
        a = resolve('Hyundai HMMA - 2207 - 2026-11-06.xlsx', '2207', result)
        self.assertEqual((a['client_key'], a['list_id']), ('Hyundai', HY_LIST))

    def test_unpinned_job_on_two_sites_needs_a_person(self):
        a = resolve('PPG_2300_Nov06.xlsx', '2300')
        self.assertEqual(a['status'], 'ambiguous')
        self.assertIsNone(a.get('client_key'))
        self.assertEqual(len(a['candidates']), 2)

    def test_filename_hint_picks_between_two_sites(self):
        self.assertEqual(resolve('SL Alabama - 2300 - 2026-11-06.xlsx', '2300')['client_key'], 'SLAlabama')

    def test_source_urls_in_the_workbook_decide_the_site(self):
        from hosting.spreadsheets import inspect_spreadsheet
        r = inspect_spreadsheet('PPG_2214.xlsx', sl_daily())
        a = registry.resolve(r, 'PPG_2214.xlsx', '2214', '2026-11-06', REG, SNAPSHOT)
        self.assertEqual((a['status'], a['client_key'], a['site']), ('verified', 'SLAlabama', 'SLAlabama_AlexanderCity'))   # 2214 is a pinned source since 2026-11-20

    def test_filename_that_contradicts_the_source_urls_is_a_conflict(self):
        from hosting.spreadsheets import inspect_spreadsheet
        r = inspect_spreadsheet('Hyundai HMMA - 2207 - 2026-11-06.xlsx', sl_daily(job='2207'))
        r['report_identity'] = {}   # only the filename names a client
        a = registry.resolve(r, 'Hyundai HMMA - 2207 - 2026-11-06.xlsx', '2207', '2026-11-06', REG, SNAPSHOT)
        self.assertEqual(a['status'], 'conflict')

    def test_unknown_job_is_unmatched(self):
        self.assertEqual(resolve('x.xlsx', '9999')['status'], 'unmatched')

    def test_label_reads_client_job_date(self):
        a = resolve('SL Alabama - 2207 - 2026-11-06.xlsx', '2207')
        self.assertEqual(registry.label(a), 'SL Alabama → 2207 → 2026-11-06')


class PrattSources(unittest.TestCase):
    def test_each_source_stays_separate_with_no_invented_job_number(self):
        a = resolve('Pratt - DCA Main Audit - 2026-11-06.xlsx', None)
        b = resolve('Pratt - DCA Multi-Pack Audit - 2026-11-06.xlsx', None)
        c = resolve('Audits_KN 2026-11-06.xlsx', None)
        self.assertEqual([x['job'] for x in (a, b, c)], ['DCA Main Audit', 'DCA Multi-Pack Audit', 'Audits_KN'])
        self.assertTrue(all(x['client'] == 'Pratt & Whitney' and x['job_number'] is None for x in (a, b, c)))


class ManualAssignment(unittest.TestCase):
    def test_choice_is_saved_audited_and_read_back_after_reload(self):
        with tempfile.TemporaryDirectory() as root:
            inbox = SpreadsheetInbox(root)
            inbox.set_setting(registry.SNAPSHOT_KEY, SNAPSHOT)
            from tests.test_report_titles import provisional
            item = inbox.ingest('PPG_2300_Nov06.xlsx', provisional(job='2300', client='PPG'))
            listed = next(x for x in inbox.list()['spreadsheets'] if x['file_hash'] == item['file_hash'])
            self.assertTrue(listed['assignment']['needs_person'])
            registry.assign(inbox, item['file_hash'], dict(client_key='SLAlabama', client='SL Alabama', job='2300', site='SLAlabama_AlexanderCity'),
                            {'email': 'reviewer@example.com'}, 'Tablet entries came from Alexander City.', listed['assignment'])
            again = SpreadsheetInbox(root)   # a fresh load, as after a page reload
            row = next(x for x in again.list()['spreadsheets'] if x['file_hash'] == item['file_hash'])
            self.assertEqual((row['assignment']['status'], row['assignment']['client_key']), ('manual', 'SLAlabama'))
            self.assertEqual(row['assignment']['label'], 'SL Alabama → 2300 → 2026-11-06')
            log = registry.history(again, item['file_hash'])
            self.assertEqual(len(log), 1)
            self.assertEqual(log[0]['by'], 'reviewer@example.com')
            self.assertEqual(log[0]['previous']['status'], 'ambiguous')
            self.assertEqual(again.get(item['file_hash'])['release_status'], 'held')   # choosing a client approves nothing

    def test_a_client_and_job_are_required(self):
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaises(ValueError):
                registry.assign(SpreadsheetInbox(root), 'a' * 64, dict(client_key='SLAlabama'), None, '')


class ActiveJobSources(unittest.TestCase):
    """The active job lists pinned on 2026-11-20: a build needs exactly one verified list for the job."""
    PINNED = {'SLALAL-SLAL-2169': ('SLAlabama_AlexanderCity', '38718598-a97e-4388-bcc5-709ede868b21'),
              'SLALAL-SLALANNX-2200': ('SLAlabama_AlexanderCity', 'e402d426-4595-47bc-a305-a0704df18258'),
              'SLAL-SLAL-2214': ('SLAlabama_AlexanderCity', 'd1a655b8-dc19-4d0f-bad2-35ed9999ccc2'),
              'SLAL-HMMA-2236': ('Hyundai_Montgomery', '9876a097-558d-4407-9b90-216d46a81217'),
              'LSAMX-HMMA-2250': ('Hyundai_Montgomery', '4a6b1fb9-ede1-4d5c-80dd-9ad1d889df91'),
              'BROQU-LEART1-2206': ('Lear_T1', '4cd2ca3c-c0c6-4092-8b8d-8b52ada1fb3d'),
              'TENFM-BSAL-2228': ('BriggsStratton_Auburn', 'fe964017-6594-4f24-8e8a-deb82119418c')}

    def verified(self, job):
        return [(e['site'], e['list_id']) for e in registry.entries([]) if e.get('verified') and e.get('list_id') and registry._same_job(job, e.get('job'))]

    def test_each_active_job_has_exactly_one_verified_list(self):
        for job, source in self.PINNED.items():
            self.assertEqual(self.verified(job), [source], job)

    def test_jobs_that_were_not_pinned_still_have_none_and_2207_still_needs_its_site_named(self):
        for job in ('SCTMFG-BSAL-2229', 'OWENS-BASCO-2230', 'SLAL-HYMOBAL-2221', 'SLTN-HMMA-2231'):
            self.assertEqual(self.verified(job), [], job)
        self.assertEqual(len(self.verified('SLALAL-CJAL-2207')), 2)        # the SL Alabama list and the separate Hyundai copy

    def test_no_list_is_pinned_twice_and_no_pratt_list_was_added(self):
        ids = [e['list_id'] for e in registry.SEED if e.get('list_id')]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual([e for e in registry.SEED if e['client_key'] == 'PrattWhitney' and e.get('list_id')], [])


class OneListIsOneCandidate(unittest.TestCase):
    """SharePoint reports each list with its site ID; a pinned entry names the site only by name. They are the same list."""
    SID = 'ppg.sharepoint.com,11111111-1111-1111-1111-111111111111,22222222-2222-2222-2222-222222222222'
    LIVE = [dict(client='SL Alabama', key='SLAlabama', sites=[dict(site='SLAlabama_AlexanderCity', location='Alexander City')],
                 jobs=[dict(job='2207', numbers=['2207'], site='SLAlabama_AlexanderCity', site_id=SID, list_id=SL_LIST, url='https://ppg.sharepoint.com/sites/SLAlabama_AlexanderCity/Lists/Data%202207')])]

    def test_a_pinned_list_seen_on_sharepoint_with_its_site_id_is_one_candidate(self):
        sl = [c for c in registry.candidates_for('2207', REG, self.LIVE) if c['client_key'] == 'SLAlabama']
        self.assertEqual(len(sl), 1)
        self.assertEqual((sl[0]['verified'], sl[0]['list_id'], sl[0]['site_id'], bool(sl[0]['url'])), (True, SL_LIST, self.SID, True))

    def test_a_named_report_for_a_pinned_job_is_not_called_ambiguous(self):
        a = registry.resolve({}, 'SL Alabama - 2207 - 2026-11-06.xlsx', '2207', '2026-11-06', REG, self.LIVE)
        self.assertEqual((a['status'], a['client_key'], a['list_id']), ('verified', 'SLAlabama', SL_LIST))

    def test_a_different_site_id_is_a_different_list_and_stays_separate(self):
        other = [dict(self.LIVE[0], jobs=[dict(self.LIVE[0]['jobs'][0], site_id=self.SID.replace('2222', '3333'))])]
        saved = [dict(client_key='SLAlabama', client='SL Alabama', job='2207', site='SLAlabama_AlexanderCity', site_id=self.SID, list_id=SL_LIST, verified=True)]
        sl = [c for c in registry.candidates_for('2207', registry.entries(saved), other) if c['client_key'] == 'SLAlabama']
        self.assertEqual(len(sl), 2)                                             # two site IDs disagree: nothing is merged

    def test_a_saved_entry_with_the_site_id_replaces_the_pinned_one_for_the_same_list(self):
        saved = [dict(client_key='SLAlabama', client='SL Alabama', job='2207', site='SLAlabama_AlexanderCity', site_id=self.SID, list_id=SL_LIST, verified=True)]
        sl = [e for e in registry.entries(saved) if e.get('list_id') == SL_LIST]
        self.assertEqual([(e.get('site_id'), e['verified']) for e in sl], [(self.SID, True)])
        self.assertEqual(len([e for e in registry.entries([]) if e.get('list_id') == SL_LIST]), 1)   # with nothing saved the pinned entry stands


class ExactSiteAndListBeforeVerification(unittest.TestCase):
    """A list is its exact site ID and list ID. A pin known only by site name is matched after everything is collected."""
    A = 'ppg.sharepoint.com,aaaaaaaa-0000-0000-0000-000000000000,aaaaaaaa-1111-1111-1111-111111111111'
    B = 'ppg.sharepoint.com,bbbbbbbb-0000-0000-0000-000000000000,bbbbbbbb-1111-1111-1111-111111111111'

    def live(self, *site_ids):
        return [dict(client='SL Alabama', key='SLAlabama', sites=[dict(site='SLAlabama_AlexanderCity', location='Alexander City')],
                     jobs=[dict(job='2207', numbers=['2207'], site='SLAlabama_AlexanderCity', site_id=sid, list_id=SL_LIST, url=f'https://x/{sid[19:27]}') for sid in site_ids])]

    def sl(self, cands):
        return sorted(((c.get('site_id'), c['verified']) for c in cands if c['client_key'] == 'SLAlabama'), key=str)

    def test_two_site_ids_for_one_pinned_name_stay_ambiguous_in_either_discovery_order(self):
        for order in ((self.A, self.B), (self.B, self.A)):
            cands = registry.candidates_for('2207', REG, self.live(*order))
            self.assertEqual(self.sl(cands), [(self.A, False), (self.B, False)], order)       # the pin verified neither one
            self.assertTrue(all(c.get('name_only_entry_unresolved') for c in cands if c['client_key'] == 'SLAlabama'))
            a = registry.resolve({}, 'SL Alabama - 2207 - 2026-11-06.xlsx', '2207', '2026-11-06', REG, self.live(*order))
            self.assertEqual(a['status'], 'ambiguous', order)
            self.assertIsNone(a.get('list_id'))

    def test_the_same_holds_when_the_two_site_ids_are_saved_entries_in_either_order(self):
        saved = [dict(client_key='SLAlabama', client='SL Alabama', job='2207', site='SLAlabama_AlexanderCity', site_id=sid, list_id=SL_LIST, verified=False) for sid in (self.A, self.B)]
        for order in (saved, saved[::-1]):
            entries = registry.entries(order)
            self.assertEqual(len([e for e in entries if e.get('list_id') == SL_LIST]), 3)     # the pin and both saved entries: nothing resolved
            self.assertEqual(self.sl(registry.candidates_for('2207', entries, [])), [(self.A, False), (self.B, False)])
            self.assertEqual(registry.resolve({}, 'SL Alabama - 2207.xlsx', '2207', '2026-11-06', entries, [])['status'], 'ambiguous')

    def test_one_exact_match_is_resolved_and_verified_wherever_it_appears(self):
        other_list = dict(job='2207', numbers=['2207'], site='SLAlabama_AlexanderCity', site_id=self.B, list_id='another-list')
        for jobs in ([self.live(self.A)[0]['jobs'][0], other_list], [other_list, self.live(self.A)[0]['jobs'][0]]):
            snap = [dict(self.live(self.A)[0], jobs=jobs)]
            got = {c['list_id']: (c.get('site_id'), c['verified']) for c in registry.candidates_for('2207', REG, snap) if c['client_key'] == 'SLAlabama'}
            self.assertEqual(got, {SL_LIST: (self.A, True), 'another-list': (self.B, False)})

    def test_a_saved_mapping_for_another_job_neither_hides_nor_inherits_the_pin(self):
        other_job = dict(client_key='SLAlabama', client='SL Alabama', job='2300', site='SLAlabama_AlexanderCity', site_id=self.A, list_id=SL_LIST, verified=False)
        entries = registry.entries([other_job])
        pin = [e for e in entries if e.get('list_id') == SL_LIST and str(e.get('job')) == '2207']
        self.assertEqual([(e.get('site_id'), e['verified']) for e in pin], [(None, True)])           # the 2207 pin is still there, untouched
        saved = [e for e in entries if str(e.get('job')) == '2300']
        self.assertEqual([(e['site_id'], e['verified']) for e in saved], [(self.A, False)])            # and 2300 did not become verified
        self.assertEqual([(c['job'], c['verified']) for c in registry.candidates_for('2300', entries, [])], [('2300', False)])
        self.assertEqual(self.sl(registry.candidates_for('2207', entries, [])), [(None, True)])
        # the reverse: a saved, verified 2207 entry with the site ID does not verify a pin for another job on the same list
        both = registry.entries([dict(other_job, job='2207', verified=True), other_job])
        self.assertEqual(sorted((str(e['job']), e['verified']) for e in both if e.get('list_id') == SL_LIST), [('2207', True), ('2300', False)])


if __name__ == '__main__':
    unittest.main()


class SharePointListChoice(unittest.TestCase):
    def test_global_pin_does_not_replace_exact_source_identity(self):
        from unittest.mock import patch
        from hosting import sharepoint
        sites = [('Hyundai_Montgomery', 'h', [dict(id=HY_LIST, displayName='Data #2207', lastModifiedDateTime='2026-11-07T10:00:00Z')]),
                 ('SLAlabama_AlexanderCity', 's', [dict(id=SL_LIST, displayName='Data #2207', lastModifiedDateTime='2026-11-01T10:00:00Z')])]
        with patch('hosting.sharepoint.client_sites', return_value=sites):
            with self.assertRaisesRegex(sharepoint.SharePointError, 'Ambiguous'):
                sharepoint._find_list('2207')
        selected = dict(sites[1][2][0], webUrl='https://x/sites/SLAlabama_AlexanderCity/Lists/data')
        with patch('hosting.sharepoint._get', return_value=selected):
            name, sid, lst = sharepoint._find_list('2207', site_id='s', list_id=SL_LIST)
        self.assertEqual((name, sid, lst['id']), ('SLAlabama_AlexanderCity', 's', SL_LIST))


class DiscoveryCoverage(unittest.TestCase):
    def test_an_unreadable_site_is_reported_not_treated_as_empty_and_lists_are_paged(self):
        from unittest.mock import patch
        from hosting import sharepoint
        def get(url, opener=None):
            if url.startswith('/sites?search'):
                return {'value': [{'id': 'a', 'name': 'SLAlabama_AlexanderCity'}, {'id': 'b', 'name': 'Hyundai_Montgomery'}]}
            if url.startswith('/sites/b/lists'):
                raise sharepoint.SharePointError('Access denied (403).')
            if url.startswith('/sites/a/lists'):
                return {'value': [{'id': 'l1', 'displayName': 'Data #2207'}], '@odata.nextLink': 'https://graph.microsoft.com/v1.0/sites/a/lists?$skiptoken=page2'}
            if url == 'https://graph.microsoft.com/v1.0/sites/a/lists?$skiptoken=page2':
                return {'value': [{'id': 'l2', 'displayName': '2214-SLAL-SLAL'}]}
            return None
        with patch('hosting.sharepoint._get', get), patch.dict('os.environ', {'SHAREPOINT_SITES': ''}):
            sites = sharepoint.client_sites(opener=lambda *a, **k: None, refresh=True)
        self.assertEqual([len(l) for _, _, l in sites], [2])
        self.assertEqual(sharepoint.last_discovery['unreadable_sites'][0]['site'], 'Hyundai_Montgomery')

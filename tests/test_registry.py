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
               dict(job='2214', numbers=['2214'], site='SLAlabama_AlexanderCity', list_id='s-2214'),
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

    def test_a_real_hyundai_report_stays_hyundai(self):
        a = resolve('Hyundai HMMA - 2207 - 2026-11-06.xlsx', '2207')
        self.assertEqual(a['client_key'], 'Hyundai')

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
        self.assertEqual((a['status'], a['client_key'], a['site']), ('matched', 'SLAlabama', 'SLAlabama_AlexanderCity'))

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


if __name__ == '__main__':
    unittest.main()

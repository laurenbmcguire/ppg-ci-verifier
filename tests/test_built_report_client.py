"""A report built from a client's SharePoint site and list always knows that client. Made-up rows only; no network."""
import tempfile
import unittest
from unittest.mock import patch

from hosting import builder, public_api, registry, sharepoint, shifts
from hosting.spreadsheet_inbox import SpreadsheetInbox

SL_LIST, HYUNDAI_LIST = '32a42ee0-4592-4d73-b893-1b98970b3d4b', '47f89ceb-d8b9-45c3-932a-66d46e3f4272'
SL = dict(client_key='SLAlabama', client='SL Alabama', job='2207', job_number='2207', site='SLAlabama_AlexanderCity', site_id='sl-site-id', list_id=SL_LIST)
REG = registry.entries([])


def resolve(job=None, manual=None, built=None, filename='2207 - EOS - 11.21.26.xlsx'):
    return registry.resolve({}, filename, job, '2026-11-21', REG, [], manual, built=built)


def capture():
    return ('SL', {'id': 'lid', 'displayName': 'Data', '_capture': {'site_id': 'sl-site-id', 'list_id': SL_LIST, 'columns': []}}, {'Shift': 'Shift'}, [
        {'id': '1', 'fields': {'Date': '2026-11-21', 'Date0': '2026-11-21T15:00:00Z', 'Shift': '1', 'Identifier': 'A1', 'Qty': 48, 'NOK': 0}},
        {'id': '2', 'fields': {'Date': '2026-11-21', 'Date0': '2026-11-21T16:00:00Z', 'Shift': '1', 'Identifier': 'A2', 'Qty': 48, 'NOK': 0}}])


def workbook():
    with patch.object(sharepoint, 'configured', return_value=True), patch.object(sharepoint, '_list_items', return_value=capture()), \
            patch.object(builder, 'column_roles', return_value={'Identifier': 'identifier', 'Qty': 'inspected', 'NOK': 'rejected'}):
        rows, _ = builder.day_rows('2207', '2026-11-21', shifts.DEFAULT, site_id='sl-site-id', list_id=SL_LIST)
    return builder.build_xlsx('2207', '2026-11-21', rows, shifts.DEFAULT)


class TheBuildRecordsTheClient(unittest.TestCase):
    def selection(self, job, source):
        class Inbox:
            def setting(self, *a): return []
        with patch.object(sharepoint, 'assignment_identity', side_effect=lambda e, *a: {'site_id': 'id-of-' + e['site'], 'list_id': e['list_id']}):
            return public_api._build_selection(Inbox(), job, source)

    def test_the_selected_source_names_its_client_however_the_job_is_typed(self):
        for job in ('2207', 'SLALAL-CJAL-2207', 'slalal-cjal-2207'):
            binding, entry = self.selection(job, {'site': 'SLAlabama_AlexanderCity', 'list_id': SL_LIST})
            record = registry.built_from(entry, binding, job)
            self.assertEqual((record['client_key'], record['client'], record['site'], record['list_id']), ('SLAlabama', 'SL Alabama', 'SLAlabama_AlexanderCity', SL_LIST))
            self.assertEqual((record['site_id'], record['job'], record['job_number']), ('id-of-SLAlabama_AlexanderCity', job, '2207'))

    def test_the_hyundai_copy_of_the_same_job_number_records_hyundai(self):
        binding, entry = self.selection('2207', {'site': 'Hyundai_Montgomery', 'list_id': HYUNDAI_LIST})
        self.assertEqual(registry.built_from(entry, binding, '2207')['client_key'], 'Hyundai')

    def test_the_binding_alone_is_unchanged_for_other_callers(self):
        class Inbox:
            def setting(self, *a): return []
        with patch.object(sharepoint, 'assignment_identity', return_value={'site_id': 'sid', 'list_id': SL_LIST}):
            self.assertEqual(public_api._build_binding(Inbox(), '2207', {'site': 'SLAlabama_AlexanderCity', 'list_id': SL_LIST}), {'site_id': 'sid', 'list_id': SL_LIST})


class TheReportKnowsItsClient(unittest.TestCase):
    def test_a_built_report_is_assigned_without_a_job_in_its_name(self):
        a = resolve(built=SL)
        self.assertEqual((a['status'], a['client_key'], a['client'], a['job'], a['site'], a['list_id']), ('verified', 'SLAlabama', 'SL Alabama', '2207', 'SLAlabama_AlexanderCity', SL_LIST))
        self.assertIn('Built from the SL Alabama SharePoint list', a['evidence'][0])
        self.assertNotIn(a['status'], registry.NEEDS_PERSON)

    def test_a_job_number_two_clients_share_goes_to_the_client_whose_list_was_read(self):
        self.assertEqual(resolve('2207', built=SL)['client_key'], 'SLAlabama')
        self.assertEqual(resolve('2207', built=dict(SL, client_key='Hyundai', client='Hyundai', site='Hyundai_Montgomery', list_id=HYUNDAI_LIST))['client_key'], 'Hyundai')

    def test_a_persons_choice_still_comes_first(self):
        a = resolve(manual=dict(client_key='Hyundai', client='Hyundai', job='2207', by='reviewer@example.com', at='2026-11-23T00:00:00+00:00'), built=SL)
        self.assertEqual((a['status'], a['client_key']), ('manual', 'Hyundai'))

    def test_an_incomplete_record_assigns_nothing(self):
        for missing in ('client_key', 'site_id', 'list_id'):
            a = resolve(built={k: v for k, v in SL.items() if k != missing})
            self.assertEqual(a['status'], 'unmatched', missing)
        self.assertEqual(resolve(built=None)['status'], 'unmatched')
        self.assertEqual(resolve(built={})['status'], 'unmatched')

    def test_in_the_report_list_the_built_report_is_under_its_client_and_needs_nobody(self):
        with tempfile.TemporaryDirectory() as root:
            inbox = SpreadsheetInbox(root)
            item = inbox.ingest(builder.file_name('2207', '2026-11-21'), workbook())
            row = next(x for x in inbox.list()['spreadsheets'] if x['file_hash'] == item['file_hash'])
            before = row['assignment']
            inbox.set_setting(registry.BUILT_FROM_PREFIX + item['file_hash'], SL)
            again = SpreadsheetInbox(root)
            after = next(x for x in again.list()['spreadsheets'] if x['file_hash'] == item['file_hash'])['assignment']
            self.assertEqual((after['status'], after['client_key'], after['client'], after['needs_person']), ('verified', 'SLAlabama', 'SL Alabama', False))
            self.assertTrue(after['label'].startswith('SL Alabama → '))
            # without the record, this file's name gives no job, so it waited for a person (what the app showed before)
            self.assertEqual((before['status'], before['needs_person']), ('unmatched', True))
            self.assertEqual(again.get(item['file_hash'])['release_status'], 'held')        # knowing the client approves nothing

    def test_another_report_is_not_given_this_reports_client(self):
        with tempfile.TemporaryDirectory() as root:
            inbox = SpreadsheetInbox(root)
            built = inbox.ingest(builder.file_name('2207', '2026-11-21'), workbook())
            inbox.set_setting(registry.BUILT_FROM_PREFIX + built['file_hash'], SL)
            ctx = inbox.assignment_context()
            other = inbox.assignment_for({'file_hash': 'f' * 64, 'filename': 'Uploaded by hand.xlsx'}, {'job': None, 'day': '2026-11-21'}, ctx)
            self.assertEqual(other['status'], 'unmatched')


class TheReportCardShowsTheJob(unittest.TestCase):
    def test_the_card_falls_back_to_the_assigned_job_instead_of_job_not_found(self):
        from pathlib import Path
        source = (Path(__file__).resolve().parents[1] / 'web' / 'app.js').read_text()
        self.assertIn("SETTLED.includes(r.assignment?.status) && r.assignment?.job ? 'Job ' + r.assignment.job : 'Job not found'", source)


if __name__ == '__main__':
    unittest.main()

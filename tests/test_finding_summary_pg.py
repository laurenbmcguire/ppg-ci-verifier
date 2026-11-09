"""Real disposable-Postgres route/lock parity. Never connect to a live database.

Use the existing CI_PG_TEST_URL and disposable marker gate from test_native_pg.
"""
import os
import unittest
from unittest.mock import patch
from tests import test_native_pg as native
from hosting import finding_summary
from hosting.spreadsheet_inbox import SpreadsheetInbox
from hosting.review_summary import check_run


@unittest.skipUnless(native.URL, 'set CI_PG_TEST_URL to the existing disposable Postgres')
class FindingSummaryPostgres(unittest.TestCase):
    setUp = native.NativeTables.setUp
    tearDown = native.NativeTables.tearDown
    call = native.NativeTables.call

    def prepare_report(self):
        with self.server.workspace_store.session() as directory:
            box = SpreadsheetInbox(directory)
            item = box.ingest('SYNTHETIC.csv', b'Part,Inspected,Rejected\nSYN-1,10,\n')
        return dict(file_hash=item['file_hash'], expected_check_hash=item['check_hash'], run=check_run(item),
                    expected_rev=0, scope='available_findings')

    def state(self, fh):
        import psycopg
        with self.server.workspace_store.session() as directory:
            box = SpreadsheetInbox(directory)
            value = (box.get(fh), box.original_bytes(fh)[1])
        with psycopg.connect(native.URL) as db:
            counts = tuple(db.execute('SELECT count(*) FROM ' + name).fetchone()[0] for name in
                           ('spreadsheets', 'spreadsheet_checks', 'settings', 'ci_verifier_files'))
        return value, counts

    def test_actual_postgres_snapshot_route_is_readonly(self):
        p = self.prepare_report()
        before = self.state(p['file_hash'])
        with patch.object(finding_summary, 'ask', return_value='Review the available findings.') as provider:
            status, result = self.call('/api/spreadsheets/review-summary', p)
        self.assertEqual(status, 200, result)
        self.assertTrue(result['read_only'])
        self.assertEqual(result['narrative_status'], 'done')
        self.assertEqual(provider.call_count, 1)
        self.assertEqual(before, self.state(p['file_hash']))

    def test_actual_postgres_assignment_settings_race_discards_summary(self):
        p = self.prepare_report()
        def change(body):
            # Another committed settings write while the provider is outside snapshot locks.
            with self.server.workspace_store.session() as directory:
                SpreadsheetInbox(directory).set_setting('assignment|' + p['file_hash'], {'job': 'SYNTHETIC-NEW'})
            return 'This response must be discarded.'
        with patch.object(finding_summary, 'ask', side_effect=change):
            status, result = self.call('/api/spreadsheets/review-summary', p)
        self.assertEqual(status, 409, result)
        self.assertNotIn('reply', result)
        self.assertIn('changed', result.get('error', '').lower())

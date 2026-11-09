"""Actual SQLite PDF persistence: approval preservation and render/save races."""
import copy
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from unittest.mock import patch
from hosting import change_summary, review_summary
from hosting.spreadsheet_inbox import SpreadsheetInbox, files_for
from test_r8_count_contract import fixture, compare
from eos_fixture import build

class PDFTransactions(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.inbox=SpreadsheetInbox(self.tmp.name);self.item=self.inbox.ingest('2214 - EOS - 2026-11-07.xlsx',build())
        self.h=self.item['file_hash'];self.assignment=dict(status='source',site='Plant',site_id='sid',list_id='lid',job='2214',needs_person=False)
        root=self.tmp.name
        class Handler:
            def store(self):
                class Store:
                    @contextmanager
                    def session(self):yield root
                return Store()
        self.handler=Handler()
    def seed_approval(self):
        def save(result):
            result['approval']=dict(by='Synthetic reviewer',at='2026-11-08',run=review_summary.check_run(result),decisions_basis=review_summary.decisions_basis(result))
            result['approval']['approved_state_hash']=review_summary.approved_state_fingerprint(result)
        return self.inbox._update(self.h,save)
    def test_pdf_leaves_approved_state_and_original_bytes_unchanged(self):
        self.seed_approval();before=self.inbox.get(self.h);raw=self.inbox.original_bytes(self.h)[1]
        self.assertTrue(review_summary.approval_current(before))
        with patch.object(SpreadsheetInbox,'assignment_for',return_value=self.assignment):
            record=change_summary.make(self.handler,self.h)
        after=self.inbox.get(self.h);self.assertEqual(after,before)
        self.assertTrue(review_summary.approval_current(after));self.assertEqual(raw,self.inbox.original_bytes(self.h)[1])
        self.assertEqual(len(self.inbox.setting(change_summary.RECORDS)),1)
        self.assertTrue(files_for(self.inbox.root).get(record['path']).startswith(b'%PDF'))
    def test_changes_during_render_prevent_persistence(self):
        for field,value in [('checker_version','obsolete'),('source_reconciliation',{'snapshot_id':'new'}),('finding_decisions',{'x':{'action':'defer'}})]:
            with self.subTest(field=field):
                self.inbox._update(self.h,lambda r:r.pop(field,None) if field!='checker_version' else None)
                def render(data,by):
                    self.inbox._update(self.h,lambda r:r.update({field:value}));return b'%PDF synthetic'
                with patch.object(SpreadsheetInbox,'assignment_for',return_value=self.assignment),patch.object(change_summary,'render',side_effect=render):
                    with self.assertRaisesRegex(ValueError,'changed while'):change_summary.make(self.handler,self.h)
                self.assertFalse(self.inbox.setting(change_summary.RECORDS))
    def test_assignment_change_during_render_prevents_persistence(self):
        assignment=dict(self.assignment)
        def render(data,by):assignment['site_id']='different';return b'%PDF synthetic'
        with patch.object(SpreadsheetInbox,'assignment_for',side_effect=lambda *a:dict(assignment)),patch.object(change_summary,'render',side_effect=render):
            with self.assertRaisesRegex(ValueError,'changed while'):change_summary.make(self.handler,self.h)
        self.assertFalse(self.inbox.setting(change_summary.RECORDS))
    def test_competing_pdf_writers_do_not_lose_records(self):
        with patch.object(SpreadsheetInbox,'assignment_for',return_value=self.assignment),patch.object(change_summary,'render',return_value=b'%PDF synthetic'):
            with ThreadPoolExecutor(max_workers=2) as pool:
                records=list(pool.map(lambda _:change_summary.make(self.handler,self.h),range(2)))
        self.assertEqual({r['id'] for r in records},{r['id'] for r in self.inbox.setting(change_summary.RECORDS)})
    def test_old_checker_or_forged_count_label_cannot_verify_receipt(self):
        from test_change_summary_binding import TestBinding
        item=TestBinding().item();a=TestBinding.assignment
        self.assertEqual(change_summary.source_receipt(item,'2026-11-06',a)['status'],'verified')
        item['checker_version']='old';item['source_reconciliation']['bound_to']['checker_version']='old'
        self.assertEqual(change_summary.source_receipt(item,'2026-11-06',a)['status'],'held')
        item=TestBinding().item();item['source_reconciliation']['count_reconciliation']={'status':'verified'}
        self.assertEqual(change_summary.source_receipt(item,'2026-11-06',a)['status'],'held')

import os
@unittest.skipUnless(os.environ.get('CI_PG_TEST_URL'),'set CI_PG_TEST_URL for disposable PostgreSQL')
class PDFPostgresTransactions(unittest.TestCase):
    def setUp(self):
        from pg_safety import assert_disposable
        from hosting.postgres_storage import PostgresWorkspace
        import psycopg
        self.url=os.environ['CI_PG_TEST_URL'];assert_disposable(self.url)
        with psycopg.connect(self.url) as db:
            db.execute('TRUNCATE ci_verifier.ci_verifier_state, ci_verifier.ci_verifier_files, ci_verifier.ci_verifier_upload_parts, ci_verifier.spreadsheets, ci_verifier.spreadsheet_checks, ci_verifier.spreadsheet_sources, ci_verifier.client_contacts, ci_verifier.settings, ci_verifier.vin_index, ci_verifier.vin_indexed, ci_verifier.dashboards, ci_verifier.api_keys RESTART IDENTITY')
        self.store=PostgresWorkspace(self.url)
        store=self.store
        class Handler:
            def store(self):return store
        self.handler=Handler()
        with self.store.session() as root:
            inbox=SpreadsheetInbox(root);self.h=inbox.ingest('synthetic.xlsx',build())['file_hash']
            def approve(r):
                r['approval']=dict(by='Synthetic reviewer',run=review_summary.check_run(r),decisions_basis=review_summary.decisions_basis(r))
                r['approval']['approved_state_hash']=review_summary.approved_state_fingerprint(r)
            inbox._update(self.h,approve);self.before=inbox.get(self.h);self.raw=inbox.original_bytes(self.h)[1]
    def test_parallel_pdf_writes_preserve_approval_and_all_records(self):
        with patch.object(change_summary,'render',return_value=b'%PDF synthetic'):
            with ThreadPoolExecutor(max_workers=2) as pool:
                records=list(pool.map(lambda _:change_summary.make(self.handler,self.h),range(2)))
        with self.store.session() as root:
            inbox=SpreadsheetInbox(root)
            self.assertEqual(inbox.get(self.h),self.before);self.assertTrue(review_summary.approval_current(inbox.get(self.h)))
            self.assertEqual(inbox.original_bytes(self.h)[1],self.raw)
            self.assertEqual({r['id'] for r in records},{r['id'] for r in inbox.setting(change_summary.RECORDS)})
            for rec in records:self.assertEqual(files_for(root).get(rec['path']),b'%PDF synthetic')
    def test_change_during_render_has_no_pdf_record(self):
        def render(*a):
            with self.store.session() as root:SpreadsheetInbox(root)._update(self.h,lambda r:r.update(note='changed'))
            return b'%PDF synthetic'
        with patch.object(change_summary,'render',side_effect=render):
            with self.assertRaisesRegex(ValueError,'changed while'):change_summary.make(self.handler,self.h)
        with self.store.session() as root:self.assertFalse(SpreadsheetInbox(root).setting(change_summary.RECORDS))

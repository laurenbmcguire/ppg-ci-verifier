import os
"""SharePoint job check and the PDF change summary kept on record."""
import base64, io, json, os, sys, unittest, urllib.request

sys.path.insert(0, os.path.dirname(__file__))
import test_autopilot
from eos_fixture import build, ROWS
from hosting import sharepoint_jobs
from unittest.mock import patch
from decimal import Decimal


def job_list():
    from openpyxl import Workbook
    wb = Workbook(); ws = wb.active; ws.title = 'All Jobs'
    ws.append(['PPG SharePoint — Clients & Jobs']); ws.append(['Pulled live']); ws.append(['As-of date', '2026-11-06'])
    ws.append([]); ws.append(['Client', 'Location', 'SharePoint Site', 'Job # (list name)', 'Type', 'Status', 'Entries', 'First Entry', 'Last Entry', 'Days', 'Open', 'Open', 'Notes'])
    ws.append(['Owens', 'Basco', 'Owens_Basco', '2230-OWENS-BASCO', 'Job', 'Active', 50, None, '2026-11-05', 1, '', '', ''])
    ws.append(['Grammer', 'Shannon, MS', 'Grammer_Shannon', '2222-SUNWAY-GRAMMS', 'Job', 'Active', 55, None, '2026-10-28', 9, '', '', 'List web address says 2232 (internal name only)'])
    ws.append(['AVIIXA', '—', 'AVIIXA', 'HyundaiTemplate', 'Template / Test', 'Template / Test', 0, None, None, None, '', '', ''])
    b = io.BytesIO(); wb.save(b); return b.getvalue()

from test_send import displayed_binding


class Lookup(unittest.TestCase):
    def test_lookup(self):
        cat = sharepoint_jobs.parse(job_list())
        self.assertEqual((cat['as_of'], len(cat['jobs'])), ('2026-11-06', 3))
        self.assertEqual(sharepoint_jobs.lookup('2230-OWENS-BASCO', cat)['status'], 'found')
        self.assertEqual(sharepoint_jobs.lookup('OWENS-BASCO-2230', cat)['status'], 'differs')
        self.assertEqual(sharepoint_jobs.lookup('2232-SUNWAY-GRAMMS', cat)['match']['job'], '2222-SUNWAY-GRAMMS')
        self.assertEqual(sharepoint_jobs.lookup('9999-NOPE', cat)['status'], 'not_found')
        self.assertEqual(sharepoint_jobs.lookup('HyundaiTemplate', cat)['status'], 'not_found')   # templates are not jobs
        self.assertEqual(sharepoint_jobs.lookup('2230-OWENS-BASCO', None)['status'], 'no_list')


class Summaries(test_autopilot.Autopilot):
    test_only_sure_fixes_and_never_sends = test_remove_duplicate_rows = test_paused_by_default = None

    def setUp(self):
        super().setUp()
        self.assignment = dict(status='source', site_id='site-synthetic', list_id='list-synthetic',
                               site='Synthetic', client_key='Synthetic', job='2230-OWENS-BASCO')
        # These PDF route tests assume a separately verified assignment. Registry
        # resolution and ambiguous assignments retain their own regression suites.
        self.enterContext(patch('hosting.spreadsheet_inbox.SpreadsheetInbox.assignment_for', return_value=self.assignment))

    def bind_verified_source(self, file_hash, day='2026-10-07'):
        from hosting import reconcile
        from hosting.spreadsheet_inbox import SpreadsheetInbox
        from hosting.spreadsheets import CHECKER_VERSION
        records, items = [], []
        for n, row in enumerate(ROWS, 1):
            values = dict(inspected=Decimal(row[2]), rejected=Decimal(row[3]), certified=Decimal(row[2])-Decimal(row[3]))
            records.append(dict(sheet='1st Shift', row=15+n, columns={'inspected': 'F', 'rejected': 'H', 'certified': 'G'},
                                source=dict(site_id='site-synthetic', list_id='list-synthetic', item=str(n)), values=values))
            items.append(dict(item=str(n), day=day, day_basis='business_date', raw={
                'QuantityInspected': row[2], 'QuantityRejected': row[3], 'QuantityOK': row[2]-row[3]}))
        snapshot = dict(site_id='site-synthetic', list_id='list-synthetic', site='Synthetic',
                        list=dict(id='list-synthetic', url='https://example.com/sites/Synthetic/Lists/Exact'),
                        captured_at='2026-11-08T00:00:00Z', coverage=dict(complete=True, pages=1, items=len(items)), items=items,
                        quantity_candidates={'inspected': ['QuantityInspected'], 'rejected': ['QuantityRejected'], 'certified': ['QuantityOK']},
                        columns={n: dict(name=n, displayName=n) for n in ('QuantityInspected', 'QuantityRejected', 'QuantityOK')})
        receipt = reconcile.compare(day, records, [], snapshot, report=dict(file_hash=file_hash, checker_version=CHECKER_VERSION))
        self.assertTrue(receipt['verified'], receipt)
        self.assertTrue(reconcile.counts_verified(receipt))
        with self.server.workspace_store.session() as directory:
            inbox = SpreadsheetInbox(directory)
            item = inbox.get(file_hash)
            item['source_reconciliation'] = receipt
            item['source_reconciliation_status'] = 'verified'
            with inbox.connect() as db:
                inbox.save_check(db, item)

    def upload(self, rows, day='2026-10-07'):
        st, up = self.call('/api/spreadsheets/import', {'filename': f'2230-OWENS-BASCO - EOS - {day}.xlsx', 'content_base64': base64.b64encode(build(rows, day=day)).decode()})
        self.assertEqual(st, 200, up); return up['item']['file_hash']

    def get(self, path):
        with urllib.request.urlopen(self.base + path, timeout=30) as r:
            return r.read()

    def test_changed_report_gets_a_pdf_on_record(self):
        os.environ['PPG_AUTOFORMAT_ON_UPLOAD'] = '1'; self.addCleanup(os.environ.pop, 'PPG_AUTOFORMAT_ON_UPLOAD', None)   # opt-in cleanup
        st, info = self.call('/api/settings/sharepoint-jobs', {'content_base64': base64.b64encode(job_list()).decode(), 'filename': 'jobs.xlsx'})
        self.assertEqual((st, info['real'], info['active']), (200, 2, 2))
        rows = [tuple(r) for r in ROWS]
        rows[1] = (rows[1][0], ' 80139189 ') + rows[1][2:]                     # formatting: cleaned automatically
        h = self.upload(rows)
        status, proposals = self.call('/api/spreadsheets/fixes', {'file_hash': h})
        self.assertEqual(status, 200, proposals)
        chosen = [f for f in proposals['proposals'] if f.get('sure')]
        self.assertEqual(len(chosen), 1)
        status, corrected = self.call('/api/spreadsheets/apply-fixes', {'file_hash': h,
            'changes': chosen, **displayed_binding(self.base, h)})
        self.assertEqual(status, 200, corrected)
        h = corrected['item']['file_hash']
        item = json.loads(self.get('/api/spreadsheet?id=' + h))
        self.assertEqual(item['sp_job']['status'], 'found')
        self.bind_verified_source(h)
        st, out = self.call('/api/spreadsheets/approve', {'file_hash': h, 'approver': 'Lauren', 'override': True, 'note': 'Synthetic fixture reviewed', **displayed_binding(self.base, h)})
        self.assertEqual(st, 200, out)
        rec = out['change_summary']; self.assertTrue(rec and rec['differed']); self.assertEqual(rec['changes'], 1)
        pdf = self.get('/api/change-summary?id=' + rec['id'])
        self.assertTrue(pdf.startswith(b'%PDF'))
        from pypdf import PdfReader
        text = ' '.join(p.extract_text() for p in PdfReader(io.BytesIO(pdf)).pages)
        for want in ('Report change summary', '2230-OWENS-BASCO', 'Current SharePoint check', 'Status: verified', '80139189', 'Recorded approval by Lauren'):
            self.assertIn(want, text)
        st, lst = self.call('/api/change-summaries', {})
        self.assertEqual([r['id'] for r in lst['records']], [rec['id']])
        st, mine = self.call('/api/change-summaries', {'file_hash': h})
        self.assertEqual(len(mine['records']), 1)
        st, again = self.call('/api/change-summary', {'file_hash': h})        # on request too
        self.assertEqual(st, 200, again); self.assertEqual(len(self.call('/api/change-summaries', {})[1]['records']), 2)

    def test_matching_report_needs_no_pdf(self):
        self.call('/api/settings/sharepoint-jobs', {'content_base64': base64.b64encode(job_list()).decode()})
        h = self.upload([tuple(r) for r in ROWS], day='2026-10-08')
        self.bind_verified_source(h, day='2026-10-08')
        st, out = self.call('/api/spreadsheets/approve', {'file_hash': h, 'approver': 'Lauren', 'override': True, 'note': 'Synthetic fixture reviewed', **displayed_binding(self.base, h)})
        self.assertEqual(st, 200, out)
        self.assertIsNone(out['change_summary'])  # complete exact-version match needs no difference record

    def test_clean_report_without_current_source_receipt_keeps_hold_in_pdf(self):
        h = self.upload([tuple(r) for r in ROWS], day='2026-10-08')
        st, out = self.call('/api/spreadsheets/approve', {'file_hash': h, 'approver': 'Lauren', 'override': True,
            'note': 'Synthetic internal review only', **displayed_binding(self.base, h)})
        self.assertEqual(st, 200, out)
        rec = out['change_summary']
        self.assertEqual(rec['source_receipt']['status'], 'held')
        self.assertTrue(rec['differed'])
        from pypdf import PdfReader
        text = ' '.join(p.extract_text() for p in PdfReader(io.BytesIO(self.get('/api/change-summary?id=' + rec['id']))).pages)
        self.assertIn('HOLD: current exact-version source verification', text)
        self.assertIn('Recorded approval by Lauren', text)
        self.assertNotIn('Matched SharePoint and needed no changes.', text)


if __name__ == '__main__':
    unittest.main()

"""SharePoint job check and the PDF change summary kept on record."""
import base64, io, json, os, sys, unittest, urllib.request

sys.path.insert(0, os.path.dirname(__file__))
import test_autopilot
from eos_fixture import build, ROWS
from hosting import sharepoint_jobs


def job_list():
    from openpyxl import Workbook
    wb = Workbook(); ws = wb.active; ws.title = 'All Jobs'
    ws.append(['PPG SharePoint — Clients & Jobs']); ws.append(['Pulled live']); ws.append(['As-of date', '2026-11-06'])
    ws.append([]); ws.append(['Client', 'Location', 'SharePoint Site', 'Job # (list name)', 'Type', 'Status', 'Entries', 'First Entry', 'Last Entry', 'Days', 'Open', 'Open', 'Notes'])
    ws.append(['Owens', 'Basco', 'Owens_Basco', '2230-OWENS-BASCO', 'Job', 'Active', 50, None, '2026-11-05', 1, '', '', ''])
    ws.append(['Grammer', 'Shannon, MS', 'Grammer_Shannon', '2222-SUNWAY-GRAMMS', 'Job', 'Active', 55, None, '2026-10-28', 9, '', '', 'List web address says 2232 (internal name only)'])
    ws.append(['AVIIXA', '—', 'AVIIXA', 'HyundaiTemplate', 'Template / Test', 'Template / Test', 0, None, None, None, '', '', ''])
    b = io.BytesIO(); wb.save(b); return b.getvalue()


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
    test_only_sure_fixes_and_never_sends = test_remove_duplicate_rows = None

    def upload(self, rows, day='2026-10-07'):
        st, up = self.call('/api/spreadsheets/import', {'filename': f'2230-OWENS-BASCO - EOS - {day}.xlsx', 'content_base64': base64.b64encode(build(rows, day=day)).decode()})
        self.assertEqual(st, 200, up); return up['item']['file_hash']

    def get(self, path):
        with urllib.request.urlopen(self.base + path, timeout=30) as r:
            return r.read()

    def test_changed_report_gets_a_pdf_on_record(self):
        st, info = self.call('/api/settings/sharepoint-jobs', {'content_base64': base64.b64encode(job_list()).decode(), 'filename': 'jobs.xlsx'})
        self.assertEqual((st, info['real'], info['active']), (200, 2, 2))
        rows = [tuple(r) for r in ROWS]
        rows[1] = (rows[1][0], ' 80139189 ') + rows[1][2:]                     # formatting: cleaned automatically
        h = self.upload(rows)
        item = json.loads(self.get('/api/spreadsheet?id=' + h))
        self.assertEqual(item['sp_job']['status'], 'found')
        st, out = self.call('/api/spreadsheets/approve', {'file_hash': h, 'approver': 'Lauren', 'override': True})
        self.assertEqual(st, 200, out)
        rec = out['change_summary']; self.assertTrue(rec and rec['differed']); self.assertEqual(rec['changes'], 1)
        pdf = self.get('/api/change-summary?id=' + rec['id'])
        self.assertTrue(pdf.startswith(b'%PDF'))
        from pypdf import PdfReader
        text = ' '.join(p.extract_text() for p in PdfReader(io.BytesIO(pdf)).pages)
        for want in ('Report change summary', '2230-OWENS-BASCO', 'is in SharePoint', 'Automatic cleanup', 'Approved as final by Lauren'):
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
        st, out = self.call('/api/spreadsheets/approve', {'file_hash': h, 'approver': 'Lauren', 'override': True})
        self.assertEqual(st, 200, out)
        if out['change_summary'] is not None:                                      # only when something differed or was open
            self.assertTrue(out['change_summary']['open'] or out['change_summary']['differences'])


if __name__ == '__main__':
    unittest.main()

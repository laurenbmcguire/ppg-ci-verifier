"""Downloads come in the selected PPG Daily End of Shift template. Made-up rows only; no network, no real report."""
import io
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from openpyxl import load_workbook

from hosting import builder, registry, sharepoint, shifts, template_export as te
from hosting.spreadsheet_inbox import SpreadsheetInbox

JOB, DAY, NAME = 'SLALAL-CJAL-2207', '2026-11-21', 'SLALAL-CJAL-2207 - EOS - 11.21.26.xlsx'
ROLES = {'Identifier': 'identifier', 'Qty': 'inspected', 'NOK': 'rejected', 'Part': 'part', 'Area': 'area', 'Cond': 'condition', 'For': 'inspected_for'}
HEAD = dict(paying_client='SL Alabama', worksite='Alexander City', prepared_by='PPG Quality')


def source_row(n, shift='1', **fields):
    return {'id': str(n), 'fields': dict({'Date': DAY, 'Date0': f'{DAY}T15:{n % 60:02d}:00Z', 'Shift': shift, 'Identifier': f'SYN{n:04d}', 'Part': '00000001-V',
                                          'Area': 'Main', 'Qty': 48, 'NOK': None, 'Cond': 'OK', 'For': 'Wire'}, **fields)}


def stored_report(items):
    data = ('Site', {'id': 'l', 'displayName': 'Data', '_capture': {'site_id': 's', 'list_id': 'l', 'columns': []}}, {'Shift': 'Shift'}, items)
    with patch.object(sharepoint, 'configured', return_value=True), patch.object(sharepoint, '_list_items', return_value=data), \
            patch.object(builder, 'column_roles', return_value=ROLES):
        rows, _ = builder.day_rows(JOB, DAY, shifts.DEFAULT, site_id='s', list_id='l')
    return builder.build_xlsx(JOB, DAY, rows, shifts.DEFAULT, {'customer': 'SL Alabama'})


SMALL = [source_row(1), source_row(2, NOK=3, Cond='NOK'), source_row(3, shift='2', Part='00000002-V')]
LARGE = ([source_row(n, Part=f'0000000{1 + n % 3}-V', NOK=(n % 5 if n % 4 == 0 else None)) for n in range(1, 41)]
         + [source_row(n, shift='2') for n in range(41, 53)] + [source_row(n, shift='3', NOK=1, Cond='NOK') for n in range(53, 60)])


def visible_text(book):
    return [(ws.title, c.coordinate, c.value) for ws in book if ws.sheet_state == 'visible' for row in ws.iter_rows() for c in row if isinstance(c.value, str)]


def detail(book, sheet):
    out = []
    for row in book[sheet].iter_rows(min_row=16, max_col=12):
        if isinstance(row[4].value, str) and row[4].value.startswith('SYN'):
            out.append([c.value for c in row])
    return out


class InTheTemplate(unittest.TestCase):
    def test_a_report_is_written_into_the_selected_template_with_client_ready_wording(self):
        raw = stored_report(SMALL)
        out, notes = te.in_template(NAME, raw, JOB, DAY, HEAD)
        book = load_workbook(io.BytesIO(out))
        self.assertEqual([ws.title for ws in book if ws.sheet_state == 'visible'], ['Summary', '1st Shift', '2nd Shift', '3rd Shift', 'Rejects & Photos'])
        s = book['Summary']
        self.assertEqual((s['B5'].value, s['D13'].value, s['M13'].value, s['M15'].value, s['M16'].value), ('End of Shift Report', JOB, 'SL Alabama', 'Alexander City', 'PPG Quality'))
        self.assertEqual(s['B6'].value, 'SL Alabama   |   Job SLALAL-CJAL-2207   |   Nov 21, 2026')
        self.assertEqual(s['B10'].value, '3 inspection records   |   2 part numbers   |   1 record with rejects')
        self.assertEqual(book['1st Shift']['A4'].value, 'SL Alabama   |   Job SLALAL-CJAL-2207   |   Nov 21, 2026   |   1st Shift')
        self.assertEqual([s[a].value for a in ('D21', 'D22', 'D23')], ['Recorded', 'Recorded', 'Not used'])
        self.assertEqual(book['1st Shift']['A15'].value, 'Date')                              # the template's own record header row
        words = [t for t in visible_text(book) if any(w.lower() in t[2].lower() for w in te.INTERNAL)]
        self.assertEqual(words, [])                                                           # nothing marks it as a draft or an internal file
        with zipfile.ZipFile(io.BytesIO(out)) as z:
            media = [n for n in z.namelist() if n.startswith('xl/media/')]
        self.assertGreaterEqual(len(media), 2)                                                # the template's branding images are still there

    def test_rows_and_quantities_follow_the_template_rule(self):
        out, notes = te.in_template(NAME, stored_report(SMALL), JOB, DAY, HEAD)
        book = load_workbook(io.BytesIO(out))
        first, second = detail(book, '1st Shift'), detail(book, '2nd Shift')
        self.assertEqual([(r[3], r[4], r[6], r[7], r[9], r[10]) for r in first], [('00000001-V', 'SYN0001', 48, 'OK', 0, 48), ('00000001-V', 'SYN0002', 48, 'NOK', 3, 45)])
        self.assertEqual([(r[3], r[4], r[6], r[9], r[10]) for r in second], [('00000002-V', 'SYN0003', 48, 0, 48)])
        self.assertEqual(notes, dict(reject_blank=0, certified_derived=3, inspected_blank=0))     # built reports already carry 0 where the crew left rejects empty
        said = ' '.join(t[2] for t in visible_text(book) if t[0] == 'Summary')
        self.assertNotIn('No reject quantity was recorded', said)
        self.assertIn('Quantity Certified is Quantity Inspected minus Reject Quantity on 3 records where it was not recorded.', said)
        self.assertIn('3 inspection records: 144 parts inspected, 141 certified, 3 rejected.', said)

    def test_a_stored_report_with_blank_reject_cells_counts_them_as_zero_and_says_so(self):
        with patch.dict(ROLES, clear=True, values={k: v for k, v in ROLES.items() if v != 'rejected'}):     # a report whose reject cells are empty
            raw = stored_report(SMALL)
        out, notes = te.in_template(NAME, raw, JOB, DAY, HEAD)
        book = load_workbook(io.BytesIO(out))
        self.assertEqual(notes['reject_blank'], 3)
        self.assertEqual([r[9] for r in detail(book, '1st Shift')], [0, 0])
        said = ' '.join(x[2] for x in visible_text(book) if x[0] == 'Summary')
        self.assertIn('No reject quantity was recorded on 3 records; as on the template, a blank reject quantity counts as zero.', said)

    def test_more_rows_and_part_numbers_than_the_blank_template_holds(self):
        out, _ = te.in_template(NAME, stored_report(LARGE), JOB, DAY, HEAD)
        book = load_workbook(io.BytesIO(out))
        got = {sheet: [r[4] for r in detail(book, sheet)] for sheet in ('1st Shift', '2nd Shift', '3rd Shift')}
        self.assertEqual(got['1st Shift'], [f'SYN{n:04d}' for n in range(1, 41)])             # 40 rows where the blank template has 13
        self.assertEqual(got['2nd Shift'], [f'SYN{n:04d}' for n in range(41, 53)])
        self.assertEqual(got['3rd Shift'], [f'SYN{n:04d}' for n in range(53, 60)])
        parts = [c.value for row in book['Summary'].iter_rows(min_col=2, max_col=2) for c in row if isinstance(c.value, str) and c.value.endswith('-V')]
        self.assertEqual(sorted(parts), ['00000001-V', '00000002-V', '00000003-V'])            # one line per part number

    def test_the_stored_report_is_not_changed(self):
        raw = stored_report(SMALL)
        before = bytes(raw)
        te.in_template(NAME, raw, JOB, DAY, HEAD)
        self.assertEqual(raw, before)

    def test_what_cannot_be_laid_out_says_why(self):
        from openpyxl import Workbook
        empty = io.BytesIO(); Workbook().save(empty)
        for args, why in (((NAME, empty.getvalue(), JOB, DAY), 'No inspection records'), (('report.csv', b'a,b', JOB, DAY), 'Only Excel'),
                          ((NAME, stored_report(SMALL), None, DAY), 'no job number or report date'), ((NAME, stored_report(SMALL), JOB, 'Nov 21'), 'not a calendar date')):
            with self.assertRaisesRegex(te.TemplateUnavailable, why):
                te.in_template(*args)


class TheDownload(unittest.TestCase):
    def inbox(self, root, name=NAME, items=SMALL):
        inbox = SpreadsheetInbox(root)
        item = inbox.ingest(name, stored_report(items))
        inbox.set_setting(registry.BUILT_FROM_PREFIX + item['file_hash'], dict(client_key='SLAlabama', client='SL Alabama', job=JOB, job_number='2207',
                                                                              site='SLAlabama_AlexanderCity', site_id='sid', list_id='lid'))
        inbox.set_setting('report_header|sid|lid', dict(customer='SL Alabama', worksite='Alexander City', program='Wire harness', prepared_by='PPG Quality Data Team'))
        return SpreadsheetInbox(root), item

    def test_an_ordinary_download_is_the_template_with_the_reports_client_and_header(self):
        with tempfile.TemporaryDirectory() as root:
            inbox, item = self.inbox(root)
            stored_name, stored = inbox.download(item['file_hash'])
            name, out, templated = te.for_download(inbox, inbox.get(item['file_hash']), stored_name, stored)
            self.assertTrue(templated); self.assertEqual(name, NAME); self.assertNotEqual(out, stored)
            s = load_workbook(io.BytesIO(out))['Summary']
            self.assertEqual((s['D13'].value, s['M13'].value, s['M15'].value, s['M16'].value, s['D17'].value), (JOB, 'SL Alabama', 'Alexander City', 'PPG Quality Data Team', 'Wire harness'))
            self.assertEqual(inbox.download(item['file_hash'])[1], stored)                    # the stored version is untouched

    def test_pratt_and_whitney_reports_are_never_converted(self):
        with tempfile.TemporaryDirectory() as root:
            inbox, item = self.inbox(root)
            found, (stored_name, stored) = inbox.get(item['file_hash']), inbox.download(item['file_hash'])
            self.assertEqual(te.for_download(inbox, found, 'Pratt & Whitney GA 2026-11-21.xlsx', stored), ('Pratt & Whitney GA 2026-11-21.xlsx', stored, False))
            with patch.object(SpreadsheetInbox, 'assignment_for', return_value=dict(status='source', client_key='PrattWhitney', client='Pratt & Whitney', source='GA')):
                self.assertEqual(te.for_download(inbox, found, stored_name, stored), (stored_name, stored, False))

    def test_a_report_that_cannot_be_laid_out_downloads_as_stored_and_its_name_says_so(self):
        with tempfile.TemporaryDirectory() as root:
            inbox, item = self.inbox(root)
            found, (stored_name, stored) = inbox.get(item['file_hash']), inbox.download(item['file_hash'])
            with patch.object(te, 'in_template', side_effect=te.TemplateUnavailable('no place in the template')):
                name, out, templated = te.for_download(inbox, found, stored_name, stored)
            self.assertEqual((name, out, templated), ('SLALAL-CJAL-2207 - EOS - 11.21.26 (as stored - not in PPG template).xlsx', stored, False))

    def test_the_route_keeps_the_exact_stored_and_checked_copies_available(self):
        root = Path(__file__).resolve().parents[1]
        api, page = (root / 'api' / 'index.py').read_text(), (root / 'web' / 'app.js').read_text()
        self.assertIn("if query.get('checked') != ['1'] and query.get('as') != ['stored'] and filename.lower().endswith('.xlsx'):", api)
        self.assertIn("template_export.for_download(inbox, inbox.get(file_hash), filename, raw)", api)
        self.assertIn("'/api/spreadsheets/download?id=' + x.file_hash + '&as=stored', download: x.filename || ''}, 'Download original bytes'", page)


class TheWriterIsOtherwiseUnchanged(unittest.TestCase):
    def test_rewording_never_touches_numbers_or_formulas_and_is_off_by_default(self):
        source = (Path(__file__).resolve().parents[1] / 'hosting' / 'source_first_workbook.py').read_text()
        self.assertIn('def build(rows,head,cap,bind,blobs,wording=None):', source)
        self.assertIn('if wording is not None and isinstance(value,str):value=wording(sheet,address,value)', source)
        say = te.wording(JOB, DAY, HEAD, te.rows_from([], DAY)[0] if False else [], dict(reject_blank=0, certified_derived=0, inspected_blank=0))
        self.assertEqual(say('1st Shift', 'D16', '00000001-V'), '00000001-V')                 # a report value passes through as it is
        self.assertEqual(say('Summary', 'D13', JOB), JOB)


if __name__ == '__main__':
    unittest.main()

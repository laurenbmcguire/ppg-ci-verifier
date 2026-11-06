"""Proposed fixes → approve → corrected copy; original untouched; formulas and formatting kept."""
import base64, io, json, os, sys, tempfile, threading, unittest, urllib.request, urllib.error, zipfile
from collections import Counter
from http.server import ThreadingHTTPServer
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(__file__))
from eos_fixture import build, ROWS
from hosting.fixes import proposals, apply_fixes, FixError, fixed_filename
from hosting.spreadsheets import inspect_spreadsheet
from hosting.spreadsheet_inbox import SpreadsheetInbox

NAME = '2230-OWENS-BASCO - EOS - 10.07.26.xlsx'


def seeded():
    rows = [list(r) for r in ROWS]
    rows[1][0] = '8O10808540'          # letter O for zero
    rows[2][4] = 'Holes /  Glue '       # double + trailing space
    rows[3][4] = 'holes / glue'         # spelling variant
    raw = build([tuple(r) for r in rows])
    return apply_fixes(NAME, raw, [dict(sheet='1st Shift', cell='G20', after='34', kind='text')])  # number typed as text


class FixTests(unittest.TestCase):
    def test_proposals_cover_the_seeded_errors(self):
        raw = seeded()
        result = inspect_spreadsheet(NAME, raw)
        props = proposals(result, NAME, raw)
        by = {(p['source'], p['before']): p for p in props}
        self.assertEqual(by[('Typo', '8O10808540')]['after'], '8010808540')
        self.assertEqual(by[('Clean-up', 'Holes /  Glue ')]['after'], 'Holes / Glue')
        self.assertEqual(by[('Spelling', 'holes / glue')]['after'], 'Holes / Glue')
        self.assertEqual(by[('Number as text', '34')]['kind'], 'number')
        self.assertFalse(any(p['cell'].startswith('K') for p in props))  # Certified is a formula: never proposed

    def test_apply_only_approved_and_recheck_is_clean(self):
        raw = seeded()
        result = inspect_spreadsheet(NAME, raw)
        props = proposals(result, NAME, raw)
        fixed = apply_fixes(NAME, raw, props)
        after = inspect_spreadsheet(NAME, fixed)
        before_codes, after_codes = Counter(f['code'] for f in result['findings']), Counter(f['code'] for f in after['findings'])
        for code in ('possible_identifier_typo', 'identifier_format_differs', 'extra_spaces', 'inconsistent_spelling', 'number_stored_as_text'):
            self.assertEqual(after_codes.get(code, 0), 0, (code, after_codes))
        self.assertGreater(sum(before_codes.values()), sum(after_codes.values()))
        with zipfile.ZipFile(io.BytesIO(raw)) as a, zipfile.ZipFile(io.BytesIO(fixed)) as b:
            self.assertEqual(sorted(a.namelist()), sorted(b.namelist()))
            self.assertEqual(a.read('xl/styles.xml'), b.read('xl/styles.xml'))  # formatting untouched
            sheet = [n for n in b.namelist() if n.startswith('xl/worksheets/sheet')]
            xml_b = ''.join(b.read(n).decode() for n in sheet)
            self.assertEqual(xml_b.count('<f>'), ''.join(a.read(n).decode() for n in sheet).count('<f>'))  # formulas kept
            self.assertIn('fullCalcOnLoad="1"', b.read('xl/workbook.xml').decode())
        # Only the approved change is made.
        one = [p for p in props if p['source'] == 'Typo']
        partial = inspect_spreadsheet(NAME, apply_fixes(NAME, raw, one))
        self.assertIn('extra_spaces', Counter(f['code'] for f in partial['findings']))

    def test_refuses_formulas_and_stale_values(self):
        raw = seeded()
        with self.assertRaises(FixError):
            apply_fixes(NAME, raw, [dict(sheet='1st Shift', cell='K16', after='5', kind='number')])
        with self.assertRaises(FixError):
            apply_fixes(NAME, raw, [dict(sheet='1st Shift', cell='E17', before='something else', after='x')])
        with self.assertRaises(FixError):
            apply_fixes(NAME, raw, [])

    def test_text_cell_written_inline_and_blank_cell_inserted(self):
        raw = build()
        fixed = apply_fixes(NAME, raw, [dict(sheet='1st Shift', cell='L16', after='checked <ok> & done'),
                                        dict(sheet='1st Shift', cell='Z16', after='new note')])
        from hosting.spreadsheets import _xlsx_rows
        rows = dict((t, list(r)) for t, r in _xlsx_rows(fixed, []))['1st Shift']
        self.assertEqual(rows[15][11], 'checked <ok> & done')
        self.assertEqual(rows[15][25], 'new note')

    def test_csv_fix(self):
        raw = b'Date,Identifier,Qty Inspected,Qty OK,Qty NOK\r\n2026-10-07,8O10808540,34,30,4\r\n'
        fixed = apply_fixes('r.csv', raw, [dict(sheet='CSV', cell='B2', before='8O10808540', after='8010808540')])
        self.assertEqual(fixed, b'Date,Identifier,Qty Inspected,Qty OK,Qty NOK\r\n2026-10-07,8010808540,34,30,4\r\n')

    def test_inbox_keeps_original_and_links_versions(self):
        with tempfile.TemporaryDirectory() as root:
            inbox = SpreadsheetInbox(root)
            raw = seeded(); item = inbox.ingest(NAME, raw)
            props = inbox.fix_proposals(item['file_hash'])['proposals']
            fixed = inbox.apply_fixes(item['file_hash'], props[:2])
            self.assertEqual(fixed['filename'], fixed_filename(NAME))
            self.assertEqual(len(fixed['fixed_from']['changes']), 2)
            self.assertEqual(inbox.download(item['file_hash'])[1], raw)          # original unchanged
            self.assertNotEqual(inbox.download(fixed['file_hash'])[1], raw)
            self.assertEqual(len(inbox.get(item['file_hash'])['fixes_applied']), 1)


class FixRouteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ['PPG_AUTOFORMAT'] = '0'   # these tests walk through the manual fix steps
        sup = super(); getattr(sup, 'setUpClass', lambda: None)()

    @classmethod
    def tearDownClass(cls):
        os.environ.pop('PPG_AUTOFORMAT', None)
        sup = super(); getattr(sup, 'tearDownClass', lambda: None)()

    def setUp(self):
        from api.index import handler
        from legacy_storage_fake import PostgresWorkspace
        from test_vercel import FakeDatabase
        self.env = patch.dict(os.environ, {'CI_LOCAL_DEV': '1', 'VERCEL': '0'}); self.env.start()
        for k in ('MS_TENANT_ID', 'MS_CLIENT_ID', 'MS_CLIENT_SECRET', 'ANTHROPIC_API_KEY'):
            os.environ.pop(k, None)
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), handler)
        self.server.workspace_store = PostgresWorkspace('postgresql://test.invalid/x', FakeDatabase().connect)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f'http://127.0.0.1:{self.server.server_port}'

    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.env.stop()

    def post(self, path, body):
        req = urllib.request.Request(self.base + path, data=json.dumps(body).encode(),
                                     headers={'Content-Type': 'application/json', 'Origin': self.base, 'X-Review-Token': 'same-origin'})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_propose_apply_download(self):
        raw = seeded()
        status, data = self.post('/api/spreadsheets/import', {'filename': NAME, 'content_base64': base64.b64encode(raw).decode()})
        self.assertEqual(status, 200); h = data['item']['file_hash']
        status, data = self.post('/api/spreadsheets/fixes', {'file_hash': h})
        self.assertEqual(status, 200); props = data['proposals']; self.assertGreaterEqual(len(props), 4)
        status, data = self.post('/api/spreadsheets/apply-fixes', {'file_hash': h, 'changes': props})
        self.assertEqual(status, 200, data); fixed = data['item']
        self.assertTrue(fixed['filename'].endswith('(fixed).xlsx'))
        with urllib.request.urlopen(self.base + '/api/spreadsheets/download?id=' + fixed['file_hash'], timeout=30) as r:
            self.assertIn('attachment; filename="2230-OWENS-BASCO - EOS - 10.07.26 (fixed).xlsx"', r.headers['Content-Disposition'])
            body = r.read()
        self.assertEqual(body, apply_fixes(NAME, raw, props))
        status, data = self.post('/api/spreadsheets/apply-fixes', {'file_hash': h, 'changes': [dict(sheet='1st Shift', cell='K16', after='1')]})
        self.assertEqual(status, 409); self.assertIn('formula', data['error'])


if __name__ == '__main__':
    unittest.main()


class ManualEditTests(unittest.TestCase):
    def test_row_cells_then_hand_edit_lands_in_fixed_copy(self):
        from hosting.fixes import row_cells, _sources, _cell_value
        raw = build(ROWS)
        data = row_cells(NAME, raw, '1st Shift', 17)
        self.assertEqual(data['row'], 17)
        self.assertTrue(data['header_row'] and data['header_row'] < 17)
        cells = {c['cell']: c for c in data['cells']}
        self.assertIn('A17', cells)
        self.assertTrue(any(c['header'] for c in data['cells']))
        a17 = cells['A17']
        fixed = apply_fixes(NAME, raw, [dict(sheet='1st Shift', cell='A17', before=a17['before'], after='9999999999', kind='text')])
        sources, _ = _sources(NAME, fixed)
        self.assertEqual(_cell_value(sources, '1st Shift', 'A17'), '9999999999')

    def test_row_cells_rejects_bad_input(self):
        from hosting.fixes import row_cells
        with self.assertRaises(FixError):
            row_cells(NAME, build(ROWS), '1st Shift', 'abc')
        with self.assertRaises(FixError):
            row_cells(NAME, build(ROWS), 'No such sheet', 3)


class SheetGridTests(unittest.TestCase):
    def test_grid_has_values_formulas_and_sheet_names(self):
        from hosting.fixes import sheet_grid
        g = sheet_grid(NAME, build(ROWS), '1st Shift')
        self.assertIn('Summary', g['sheets'])
        self.assertEqual(g['sheet'], '1st Shift')
        self.assertEqual(g['rows'][14][0], 'Date')
        self.assertTrue(g['formulas'])
        self.assertEqual(len(g['rows']), g['height'])

    def test_unknown_sheet_falls_back_to_first(self):
        from hosting.fixes import sheet_grid
        g = sheet_grid(NAME, build(ROWS), 'nope')
        self.assertEqual(g['sheet'], g['sheets'][0])

    def test_layout_reads_merges_and_widths(self):
        from hosting.fixes import _layout
        import io as _io, zipfile as _zip
        raw = build(ROWS)
        out = _io.BytesIO()
        with _zip.ZipFile(_io.BytesIO(raw)) as src, _zip.ZipFile(out, 'w') as dst:
            from hosting.spreadsheet_demo import sheet_paths
            path = sheet_paths(raw)['1st Shift']
            for info in src.infolist():
                data = src.read(info.filename)
                if info.filename == path:
                    x = data.decode()
                    x = x.replace('<sheetData>', '<cols><col min="1" max="2" width="20" customWidth="1"/></cols><sheetData>', 1)
                    x = x.replace('</sheetData>', '</sheetData><mergeCells count="1"><mergeCell ref="A1:D1"/></mergeCells>', 1)
                    data = x.encode()
                dst.writestr(info, data)
        merges, widths, hidden = _layout(out.getvalue(), '1st Shift')
        self.assertEqual(merges, ['A1:D1'])
        self.assertEqual(widths[0], 20.0)

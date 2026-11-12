import csv
import hashlib
import io
import json
import unittest
import zipfile
from unittest.mock import patch

from hosting.spreadsheets import inspect_spreadsheet, SpreadsheetError


def xlsx(sheet=None, extra=None):
    # Small explicit OOXML fixtures, not workbook authoring or formula execution.
    members = {
        '[Content_Types].xml': '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/></Types>',
        '_rels/.rels': '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>',
        'xl/workbook.xml': '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="Data" sheetId="1" r:id="rId1"/></sheets></workbook>',
        'xl/_rels/workbook.xml.rels': '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/></Relationships>',
        'xl/worksheets/sheet1.xml': sheet or '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData><row r="1"><c r="A1" t="inlineStr"><is><t>quantity</t></is></c></row><row r="2"><c r="A2"><v>2</v></c></row></sheetData></worksheet>',
    }
    members.update(extra or {})
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED) as archive:
        for name, body in members.items():
            archive.writestr(name, body)
    return output.getvalue()


class SpreadsheetTests(unittest.TestCase):
    def test_csv_valid_and_scope(self):
        raw = b'label,inspected,accepted,rejected\none,10,8,2\n'
        result = inspect_spreadsheet('test.csv', raw)
        self.assertEqual(result['finding_counts'], {})
        self.assertEqual(result['file_sha256'], hashlib.sha256(raw).hexdigest())
        self.assertEqual(result['spreadsheet_id'], inspect_spreadsheet('other.csv', raw)['spreadsheet_id'])
        self.assertEqual(result['verification_scope'], 'internal_spreadsheet_checks_only')
        self.assertEqual(result['source_authenticity'], 'not_supplied')
        self.assertEqual(result['release_status'], 'held')
        self.assertFalse(result['formulas_evaluated'])
        self.assertNotIn('synthetic', result)
        json.dumps(result, allow_nan=False)

    def test_xlsx_valid(self):
        result = inspect_spreadsheet('test.xlsx', xlsx())
        self.assertEqual(result['row_count'], 2)
        self.assertEqual(result['finding_counts'], {})

    def test_structural_and_numeric_findings(self):
        result = inspect_spreadsheet('test.csv', b'label,inspected,accepted,rejected\none,10,7,2\ntwo,-2,1.5,\nTotal,100,8.5,2\n')
        self.assertTrue({'inspected_count_mismatch', 'negative_count_or_quantity', 'fractional_count', 'missing_cell', 'invalid_numeric_value', 'total_mismatch'} <= result['finding_counts'].keys())
        result = inspect_spreadsheet('test.csv', b' a ,a,\n1\n')
        self.assertTrue({'duplicate_header', 'blank_header', 'header_whitespace', 'inconsistent_row_width'} <= result['finding_counts'].keys())

    def test_csv_injection_flagged_not_executed(self):
        result = inspect_spreadsheet('test.csv', b'label,quantity\n=WEBSERVICE("http://test.invalid"),2\n@SUM(A1),3\n')
        self.assertEqual(result['finding_counts']['csv_formula_injection_risk'], 2)

    def test_formula_not_evaluated(self):
        sheet = '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData><row r="1"><c r="A1"><f>1+1</f><v>2</v></c></row></sheetData></worksheet>'
        result = inspect_spreadsheet('test.xlsx', xlsx(sheet))
        self.assertEqual(result['finding_counts']['formula_not_evaluated'], 1)
        web = inspect_spreadsheet('test.xlsx', xlsx(sheet.replace('1+1', 'WEBSERVICE("https://example.invalid")')))
        self.assertEqual(web['finding_counts']['web_function_formula'], 1)  # flagged, never executed
        self.assertEqual(web['external_content'], ['web_function_formula'])
        table = inspect_spreadsheet('test.xlsx', xlsx(sheet.replace('1+1', 'SUM(Table1[Qty])+Table1[@Accepted]')))
        self.assertNotIn('external_content', table)  # structured table references are ordinary formulas

    def test_unsafe_archives(self):
        for extra in ({'../bad.xml': '<x/>'}, {'xl/vbaProject.bin': 'bad'},
                      {'xl/embeddings/oleObject1.bin': 'bad'}, {'xl/activeX/activeX1.xml': '<x/>'},
                      {'bad.xml': '<!DOCTYPE a [<!ENTITY x "hello">]><a>&x;</a>'},
                      {'bad.rels': '<Relationships><Relationship TargetMode="External" Type="http://x/oleObject" Target="file:///c:/x.doc"/></Relationships>'},
                      {'xl/worksheets/sheet2.xml': '<worksheet><oleObjects><oleObject progId="Package"/></oleObjects></worksheet>'}):
            with self.subTest(extra=extra), self.assertRaises(SpreadsheetError):
                inspect_spreadsheet('test.xlsx', xlsx(extra=extra))
        # External links and hyperlinks are recorded as findings, not refused; printer settings are normal.
        flagged = {
            'external_workbook_link': {'xl/externalLinks/externalLink1.xml': '<x/>'},
            'external_hyperlink': {'xl/worksheets/_rels/sheet1.xml.rels': '<Relationships><Relationship TargetMode="External" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink" Target="https://example.invalid"/></Relationships>'},
            'external_data_connection': {'xl/connections.xml': '<connections/>'},
            'dde_link': {'xl/externalLinks/externalLink2.xml': '<externalLink><ddeLink ddeService="cmd"/></externalLink>'},
        }
        for code, extra in flagged.items():
            with self.subTest(code=code):
                self.assertIn(code, inspect_spreadsheet('test.xlsx', xlsx(extra=extra))['finding_counts'])
        self.assertNotIn('external_content', inspect_spreadsheet('test.xlsx', xlsx(extra={'xl/printerSettings/printerSettings1.bin': 'binary'})))
        with self.assertRaises(SpreadsheetError):
            inspect_spreadsheet('bad.xlsx', b'PKbad')
        with self.assertRaises(SpreadsheetError):
            inspect_spreadsheet('bomb.xlsx', xlsx(extra={'bomb.txt': '0' * 1000000}))
        with patch('hosting.spreadsheets.MAX_UNCOMPRESSED', 10), self.assertRaises(SpreadsheetError):
            inspect_spreadsheet('test.xlsx', xlsx())
        with patch('hosting.spreadsheets.MAX_MEMBERS', 1), self.assertRaises(SpreadsheetError):
            inspect_spreadsheet('test.xlsx', xlsx())

    def test_bounded(self):
        # Row/column/cell budgets are enforced (patched small so the test stays fast); there is no byte-size limit.
        with patch('hosting.spreadsheets.MAX_ROWS', 10000), patch('hosting.spreadsheets.MAX_COLS', 100):
            for raw in (b'a\n' * 10001, (b'a,' * 100 + b'a\n')):
                with self.subTest(size=len(raw)), self.assertRaises(SpreadsheetError):
                    inspect_spreadsheet('bad.csv', raw)
            sheet = '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData><row r="10001"><c r="A10001"><v>2</v></c></row></sheetData></worksheet>'
            with self.assertRaises(SpreadsheetError):
                inspect_spreadsheet('bad.xlsx', xlsx(sheet))
        with self.assertRaises(SpreadsheetError):
            inspect_spreadsheet('bad.csv', b'a\n' + b'x' * 32768)  # single field over the CSV field limit
        with patch('hosting.spreadsheets.MAX_CELLS', 2), self.assertRaises(SpreadsheetError):
            inspect_spreadsheet('bad.csv', b'a,b\n1,2\n')
        big = b'x' * (3 * 1024 * 1024 + 1) + b'\n'
        self.assertNotIn('size', str(inspect_spreadsheet.__doc__))
        with self.assertRaises(SpreadsheetError) as caught:
            inspect_spreadsheet('big.csv', big)
        self.assertIn('field exceeds', str(caught.exception))  # rejected for the field limit, not for byte size
        wide_ok = (b'a,' * 150 + b'a\n') * 3
        self.assertEqual(inspect_spreadsheet('wide.csv', wide_ok)['sheets'][0]['header_column_count'], 151)

    def test_result_bounded(self):
        from hosting.limits import MAX_FINDINGS
        result = inspect_spreadsheet('test.csv', b'a,b\n1,2\n' + b',\n' * 500)
        self.assertEqual(len(result['findings']), sum(result['finding_counts'].values()))   # every finding is stored, not the first 200
        self.assertEqual(result['finding_counts']['missing_cell'], 1000)
        self.assertFalse(result['findings_truncated'])
        with patch('hosting.spreadsheets.MAX_FINDINGS', 300):       # the safety bound still holds and says so
            result = inspect_spreadsheet('test.csv', b'a,b\n1,2\n' + b',\n' * 500)
        self.assertEqual(len(result['findings']), 300)
        self.assertTrue(result['findings_truncated'])
        self.assertEqual(result['finding_counts']['missing_cell'], 1000)
        self.assertGreaterEqual(MAX_FINDINGS, 20000)

    def test_a_report_past_the_limit_is_held_from_approval(self):
        """Reaching the bound never hides findings silently: the report is held until a person looks."""
        import tempfile
        from hosting.spreadsheet_inbox import SpreadsheetInbox
        box = SpreadsheetInbox(tempfile.mkdtemp())
        with patch('hosting.spreadsheets.MAX_FINDINGS', 300), patch('hosting.limits.MAX_FINDINGS', 300):
            item = box.ingest('big.csv', b'a,b\n1,2\n' + b',\n' * 500)
            self.assertTrue(item['findings_truncated'])
            holds = {h['code']: h for h in box.holds(item)}
        self.assertTrue(holds['findings_incomplete']['hard'])
        self.assertIn('701 cannot be reviewed', holds['findings_incomplete']['text'])
        self.assertIn('safety limit', holds['findings_incomplete']['text'])
        with self.assertRaises(ValueError):
            box.approve(item['file_hash'], 'Reviewer')

    def test_empty_encoding_and_malformed(self):
        for name, raw in [('empty.csv', b''), ('bad.csv', b'\xff'), ('bad.csv', b'\x00'), ('bad.csv', b'"unterminated'), ('bad.xlsm', b'hi')]:
            with self.subTest(name=name, raw=raw), self.assertRaises(SpreadsheetError):
                inspect_spreadsheet(name, raw)
        result = inspect_spreadsheet('test.csv', b'quantity\nNaN\nInfinity\n1e999999\n')
        self.assertEqual(result['finding_counts']['invalid_numeric_value'], 3)


    def test_exact_totals_and_ambiguous_headers(self):
        result = inspect_spreadsheet('exact.csv', b'label,quantity\na,9999999999999999999999999999.1\nb,0.2\nTotal,9999999999999999999999999999.3\n')
        self.assertNotIn('total_mismatch', result['finding_counts'])
        result = inspect_spreadsheet('wrong.csv', b'label,quantity\na,1.1\nb,2.2\nTotal,3.4\n')
        self.assertEqual(result['finding_counts']['total_mismatch'], 1)
        for raw in (b'Quantity,Quantity\n1,2\n', b'qty,accepted count,rejected count\nabc,abc,abc\n'):
            result = inspect_spreadsheet('ambiguous.csv', raw)
            # Ambiguous or non-numeric columns never yield checked numbers.
            self.assertEqual(result.get('numeric_cells_checked', 0), 0)
            self.assertEqual(result['internal_check_status'], 'unverified')
        result = inspect_spreadsheet('missing.csv', b'label,quantity\na,\nTotal,0\n')
        self.assertNotIn('total_mismatch', result['finding_counts'])
        self.assertIn('invalid_numeric_value', result['finding_counts'])
        result = inspect_spreadsheet('after.csv', b'label,quantity\na,1\nTotal,1\nb,2\n')
        self.assertIn('rows_after_total_not_reconciled', result['finding_counts'])

    def test_csv_global_field_limit_unchanged(self):
        original = csv.field_size_limit()
        inspect_spreadsheet('test.csv', b'field\n' + b'a' * 32767)
        self.assertEqual(csv.field_size_limit(), original)
        with self.assertRaises(SpreadsheetError):
            inspect_spreadsheet('test.csv', b'field\n' + b'a' * 32768)
        self.assertEqual(csv.field_size_limit(), original)
        # Lower process settings fail safely; parser must not raise a global limit.
        try:
            csv.field_size_limit(10)
            with self.assertRaises(SpreadsheetError):
                inspect_spreadsheet('test.csv', b'field\n' + b'a' * 11)
            self.assertEqual(csv.field_size_limit(), 10)
        finally:
            csv.field_size_limit(original)

    def test_archive_directories_duplicates_and_encryption(self):
        self.assertEqual(inspect_spreadsheet('test.xlsx', xlsx(extra={'xl/': ''}))['row_count'], 2)
        for name in ('/bad.xml', 'C:/bad.xml', 'xl/../../bad.xml', 'xl\\bad.xml'):
            with self.subTest(name=name), self.assertRaises(SpreadsheetError):
                inspect_spreadsheet('test.xlsx', xlsx(extra={name: '<x/>'}))
        duplicate = io.BytesIO(xlsx())
        import warnings
        with warnings.catch_warnings():
            warnings.simplefilter('ignore', UserWarning)
            with zipfile.ZipFile(duplicate, 'a') as archive:
                archive.writestr('xl/workbook.xml', '<x/>')
        with self.assertRaises(SpreadsheetError):
            inspect_spreadsheet('test.xlsx', duplicate.getvalue())
        encrypted = bytearray(xlsx())
        # General-purpose flag in the central directory, sufficient for preflight rejection.
        central = encrypted.index(b'PK\x01\x02')
        encrypted[central + 8] |= 1
        with self.assertRaises(SpreadsheetError):
            inspect_spreadsheet('test.xlsx', bytes(encrypted))

    def test_xml_dtd_rejected_and_active_formulas_flagged(self):
        for body in ('<!DOCTYPE x><x/>', '<!DOCTYPE x SYSTEM "https://example.invalid/a"><x/>'):
            with self.subTest(body=body), self.assertRaises(SpreadsheetError):
                inspect_spreadsheet('test.xlsx', xlsx(extra={'extra.xml': body}))
        expected = {'[other.xlsx]Sheet1!A1': 'external_reference_formula', 'SUM([1]Sheet1!A1:A9)': 'external_reference_formula',
                    'cmd|data!A1': 'dde_or_macro_call_formula', 'EXECUTE(A1)': 'dde_or_macro_call_formula',
                    'REGISTER.ID(A1)': 'dde_or_macro_call_formula', 'RUN(A1)': 'dde_or_macro_call_formula',
                    'IMAGE(A1)': 'web_function_formula', 'HYPERLINK("https://example.invalid")': 'hyperlink_formula'}
        for formula, code in expected.items():
            xml = '<root><definedName>' + formula.replace('&', '&amp;').replace('"', '&quot;') + '</definedName></root>'
            with self.subTest(formula=formula):
                result = inspect_spreadsheet('test.xlsx', xlsx(extra={'extra.xml': xml}))
                self.assertIn(code, result['finding_counts'])
                self.assertIn(code, result['external_content'])
        for ordinary in ('HYPERLINK("#\'1st Shift\'!A1","1st shift detail  >")', 'SUM(Table1[Qty])', 'tbl[[#This Row],[Qty]]', 'TEXTJOIN("|",TRUE,A1:A3)', 'IF(A1="https://x",1,0)', '[0]!LocalName'):
            xml = '<root><definedName>' + ordinary.replace('"', '&quot;') + '</definedName></root>'
            with self.subTest(ordinary=ordinary):
                self.assertNotIn('external_content', inspect_spreadsheet('test.xlsx', xlsx(extra={'extra.xml': xml})))

    def test_empty_sheets_and_untrusted_dimension(self):
        empty = '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData/></worksheet>'
        self.assertEqual(inspect_spreadsheet('empty.xlsx', xlsx(empty))['finding_counts'], {'empty_sheet': 1})
        self.assertIn('no_data_rows', inspect_spreadsheet('empty.csv', b'header\n')['finding_counts'])
        sheet = '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><dimension ref="A1:XFD1048576"/><sheetData><row r="1"><c r="A1"><v>1</v></c></row></sheetData></worksheet>'
        self.assertEqual(inspect_spreadsheet('dim.xlsx', xlsx(sheet))['row_count'], 1)
        with patch('hosting.spreadsheets.MAX_ROWS', 10000), patch('hosting.spreadsheets.MAX_COLS', 100):
            for coord in ('CW2', 'A10001', 'A0'):
                with self.subTest(coord=coord), self.assertRaises(SpreadsheetError):
                    inspect_spreadsheet('bad.xlsx', xlsx(sheet.replace('r="A1"', 'r="' + coord + '"')))
        for coord in ('XFD2', 'A1048576'):
            with self.subTest(coord=coord):
                self.assertEqual(inspect_spreadsheet('edge.xlsx', xlsx(sheet.replace('r="A1"', 'r="' + coord + '"')))['row_count'], 1)
        with self.assertRaises(SpreadsheetError):
            inspect_spreadsheet('over.xlsx', xlsx(sheet.replace('r="A1"', 'r="XFE1"')))


if __name__ == '__main__':
    unittest.main()


class UnusedColumnTests(unittest.TestCase):
    def test_empty_column_is_not_missing_but_gaps_in_a_used_column_are(self):
        result = inspect_spreadsheet('t.csv', b'part,qty,notes\nA,1,\nB,,\nC,3,\n')
        missing = [(f['row'], f['column']) for f in result['findings'] if f['code'] == 'missing_cell']
        self.assertEqual(missing, [(3, 2)])   # qty blank on row 3 is flagged; the all-blank notes column is not

    def test_part_pattern_used_on_several_rows_is_not_flagged_but_a_one_off_is(self):
        head = 'Date,Shift,Area,Auditor,Identifier,Part Number,Qty Inspected,Qty OK,Qty Reworked,Qty NOK,Defect,Comments,Condition,Picture File\n'
        parts = ['Santa Fe (MX5A)'] * 12 + ['Tucson (NX4A)'] * 3 + ['Tucson-NX4']
        rows = [f'2026-10-07,1,,A,ID{n:03},{part},1,1,,0,,,OK,' for n, part in enumerate(parts)]
        result = inspect_spreadsheet('x.csv', ('\ufeff' + head + '\n'.join(rows) + '\n').encode())
        flagged = [f['row'] for f in result['findings'] if f['code'] == 'part_format_differs']
        self.assertEqual(flagged, [17])   # only the single odd value, not the model used on three rows

    def test_blank_reject_cell_is_missing_only_when_parts_are_unaccounted_for(self):
        head = 'Date,Shift,Area,Auditor,Identifier,Part Number,Qty Inspected,Qty OK,Qty Reworked,Qty NOK,Defect,Comments,Condition,Picture File\n'
        rows = ['2026-10-07,1,,A,ID1,P1,34,34,,,,,OK,',        # every part is OK: the blank NOK cell has nothing to hold
                '2026-10-07,1,,A,ID2,P1,34,30,,4,Holes,x,NOK,',
                '2026-10-07,1,,A,ID3,P1,34,30,,,,,OK,',        # 4 parts are unaccounted for: the blank NOK cell is missing
                '2026-10-07,1,,A,ID4,P1,20,20,,,,,OK,']
        result = inspect_spreadsheet('x.csv', ('\ufeff' + head + '\n'.join(rows) + '\n').encode())
        self.assertEqual(result['mapping_mode'], 'adaptive')
        self.assertEqual([(f['row'], f['column']) for f in result['findings'] if f['code'] == 'missing_cell'], [(4, 10)])


def test_fixes_are_written_into_workbooks_saved_with_prefixed_tags(tmp_path):
    """Workbooks built from the SharePoint template use <ns0:c> tags. A fix must change the cell there too, never
    return the untouched file as a saved version."""
    import io, re, zipfile
    import xml.etree.ElementTree as ET
    import openpyxl
    from hosting.spreadsheet_inbox import SpreadsheetInbox
    from sl_daily_fixture import sl_daily, records
    recs = records(n=4, same_values=True)
    for i, r in enumerate(recs):
        r['ident'] = f'LPC2TD26092302{i:04}26I140AAR692181020        000000000000030'
    out = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(sl_daily(recs))) as src, zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED) as dst:
        for info in src.infolist():
            data = src.read(info.filename)
            if re.fullmatch(r'xl/worksheets/[^/]+\.xml', info.filename) or info.filename == 'xl/workbook.xml':
                saved = dict(ET._namespace_map)
                try:
                    for url in [u for u, prefix in ET._namespace_map.items() if 'spreadsheetml/2006/main' in u or 'officeDocument/2006/relationships' in u]:
                        del ET._namespace_map[url]
                    data = ET.tostring(ET.fromstring(data), encoding='utf-8', xml_declaration=True)
                finally:
                    ET._namespace_map.clear(); ET._namespace_map.update(saved)
                assert b'<ns0:' in data
            dst.writestr(info, data)
    box = SpreadsheetInbox(tmp_path)
    item = box.ingest('SL Alabama - 2214 - 2026-11-06.xlsx', out.getvalue())
    proposals = box.fix_proposals(item['file_hash'])['proposals']
    assert proposals and all('        ' in p['before'] for p in proposals)
    fixed = box.apply_fixes(item['file_hash'], proposals, expected_check_hash=box.get(item['file_hash'])['check_hash'])
    assert fixed['file_hash'] != item['file_hash'] and not fixed.get('already_applied')
    assert box.fix_proposals(fixed['file_hash'])['proposals'] == []
    _, raw = box.original_bytes(fixed['file_hash'])
    book = openpyxl.load_workbook(io.BytesIO(raw))
    first = proposals[0]
    assert book[first['sheet']][first['cell']].value == first['after']


def test_restyle_saves_a_copy_with_one_cream_fill_and_no_value_change(tmp_path):
    """A workbook with the template's cream and yellow patchwork can be saved again with only its look tidied."""
    import pytest
    import io
    import openpyxl
    from openpyxl.styles import PatternFill, Alignment
    from hosting.fixes import needs_restyle, FixError
    from hosting.spreadsheet_inbox import SpreadsheetInbox
    from sl_daily_fixture import sl_daily
    book = openpyxl.load_workbook(io.BytesIO(sl_daily()))
    sheet = book[book.sheetnames[0]]
    sheet['Z1'].fill = PatternFill('solid', fgColor='FFFFF6C7'); sheet['Z2'].fill = PatternFill('solid', fgColor='FFFFFCEE')
    out = io.BytesIO(); book.save(out)
    box = SpreadsheetInbox(tmp_path)
    item = box.ingest('SL Alabama - 2214 - 2026-11-06.xlsx', out.getvalue())
    assert box.fix_proposals(item['file_hash'])['restyle'] is True
    with pytest.raises(ValueError):
        box.apply_fixes(item['file_hash'], [], expected_check_hash=box.get(item['file_hash'])['check_hash'])
    fixed = box.apply_fixes(item['file_hash'], [], expected_check_hash=box.get(item['file_hash'])['check_hash'], restyle=True)
    assert fixed['file_hash'] != item['file_hash'] and fixed['fixed_from']['changes'] == []
    _, raw = box.original_bytes(fixed['file_hash'])
    assert not needs_restyle(fixed['filename'], raw)
    after = openpyxl.load_workbook(io.BytesIO(raw))[book.sheetnames[0]]
    assert after['Z1'].fill.fgColor.rgb == 'FFFFFCEE' and after['Z2'].fill.fgColor.rgb == 'FFFFFCEE'
    before_values = [[c.value for c in row] for row in sheet.iter_rows()]
    assert [[c.value for c in row] for row in after.iter_rows()] == before_values
    with pytest.raises((ValueError, FixError)):   # nothing left to tidy: no empty version is ever made
        box.apply_fixes(fixed['file_hash'], [], expected_check_hash=box.get(fixed['file_hash'])['check_hash'], restyle=True)


def test_version_graph_needs_only_links_from_earlier_checks(tmp_path):
    """The hosted database reads complete records only for each version's newest check and just the version links
    for earlier ones. The graph built that way must equal the graph built from everything."""
    from hosting.spreadsheet_inbox import SpreadsheetInbox
    from hosting.working_copy import WorkingCopies
    from sl_daily_fixture import sl_daily, records
    recs = records(n=4, same_values=True)
    for i, r in enumerate(recs):
        r['ident'] = f'LPC2TD26092302{i:04}26I140AAR692181020        000000000000030'
    box = SpreadsheetInbox(tmp_path)
    item = box.ingest('SL Alabama - 2214 - 2026-11-06.xlsx', sl_daily(recs))
    box.ingest('SL Alabama - 2214 - 2026-11-07.xlsx', sl_daily(records(n=3)))
    fixed = box.apply_fixes(item['file_hash'], box.fix_proposals(item['file_hash'])['proposals'], expected_check_hash=box.get(item['file_hash'])['check_hash'])
    with box.connect() as db:
        bases, histories = box._history_inventory(db)
    slim = {key: [dict(file_hash=key, check_hash=r.get('check_hash'), fixed_from=r.get('fixed_from'), fixes_applied=r.get('fixes_applied')) for r in rows[:-1]] + rows[-1:]
            for key, rows in histories.items()}
    full_graph, slim_graph = WorkingCopies(bases, histories), WorkingCopies(bases, slim)
    assert any(len(rows) > 1 for rows in histories.values())
    for name in ('parents', 'children', 'invalid', 'items', 'recovered'):
        assert getattr(full_graph, name) == getattr(slim_graph, name), name
    for key in bases:
        assert full_graph.resolve(key) == slim_graph.resolve(key)
    assert slim_graph.resolve(item['file_hash'])['working_hash'] == fixed['file_hash']

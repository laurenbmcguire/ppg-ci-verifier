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
        result = inspect_spreadsheet('test.csv', b'a,b\n' + b',\n' * 500)
        self.assertEqual(len(result['findings']), 200)
        self.assertTrue(result['findings_truncated'])
        self.assertEqual(result['finding_counts']['missing_cell'], 1000)

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

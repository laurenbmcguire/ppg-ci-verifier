import io, json, unittest, zipfile

from hosting.report_workflow import parse, WorkflowError, SPREADSHEET_HINT


class ManifestParseTests(unittest.TestCase):
    body = {'schema_version': 2, 'report_id': 'R1'}

    def test_plain_json(self):
        self.assertEqual(parse(json.dumps(self.body)), self.body)

    def test_bom_and_utf16(self):
        self.assertEqual(parse(b'\xef\xbb\xbf' + json.dumps(self.body).encode()), self.body)
        self.assertEqual(parse(json.dumps(self.body).encode('utf-16')), self.body)

    def test_markdown_fence_and_surrounding_text(self):
        self.assertEqual(parse('```json\n' + json.dumps(self.body) + '\n```'), self.body)
        self.assertEqual(parse('Here is the manifest:\n' + json.dumps(self.body) + '\nDone.'), self.body)

    def test_xlsx_bytes_point_to_spreadsheet_upload(self):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, 'w') as z:
            z.writestr('[Content_Types].xml', '<Types/>')
        with self.assertRaises(WorkflowError) as ctx:
            parse(buffer.getvalue())
        self.assertEqual(str(ctx.exception), SPREADSHEET_HINT)

    def test_csv_points_to_spreadsheet_upload(self):
        with self.assertRaises(WorkflowError) as ctx:
            parse(b'Identifier,Part,Qty Inspected,Qty OK\nS1,P1,5,5\n')
        self.assertEqual(str(ctx.exception), SPREADSHEET_HINT)

    def test_broken_json_says_where(self):
        with self.assertRaises(WorkflowError) as ctx:
            parse('{"schema_version": 2,, }')
        self.assertIn('line 1, column', str(ctx.exception))

    def test_duplicate_keys_still_rejected(self):
        with self.assertRaises(WorkflowError) as ctx:
            parse('{"a": 1, "a": 2}')
        self.assertIn('Duplicate', str(ctx.exception))


if __name__ == '__main__':
    unittest.main()

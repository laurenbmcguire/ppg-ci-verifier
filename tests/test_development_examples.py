"""Exercise distributable synthetic examples against real inspect/reconcile paths."""
import copy
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from openpyxl import load_workbook
from hosting.build_development_examples import build_examples, rehash
from hosting.spreadsheets import inspect_spreadsheet
from hosting.reconcile_spreadsheet import reconcile_result
from hosting.report_workflow import WorkflowError


class DevelopmentExamplesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.directory = Path(cls.temp.name)
        cls.index = build_examples(cls.directory)
        cls.examples = {entry['id']: entry for entry in cls.index['examples']}

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def evaluate(self, slug):
        entry = self.examples[slug]
        raw = (self.directory / entry['artifact']['path']).read_bytes()
        report = inspect_spreadsheet(entry['artifact']['path'], raw)
        if entry['source']:
            source_raw = (self.directory / entry['source']['path']).read_bytes()
            source = json.loads(source_raw)
            before_report, before_source = copy.deepcopy(report), copy.deepcopy(source)
            result = reconcile_result(report, source)
            self.assertEqual(report, before_report)
            self.assertEqual(source, before_source)
            self.assertEqual(source_raw, (self.directory / entry['source']['path']).read_bytes())
        else:
            result = report
        self.assertEqual(raw, (self.directory / entry['artifact']['path']).read_bytes())
        return result

    def test_manifest_hashes_and_expected_actual_outcomes(self):
        for entry in self.index['examples']:
            with self.subTest(example=entry['id']):
                for artifact in (entry['artifact'], entry['source']):
                    if artifact:
                        raw = (self.directory / artifact['path']).read_bytes()
                        self.assertEqual(hashlib.sha256(raw).hexdigest(), artifact['sha256'])
                result = self.evaluate(entry['id'])
                self.assertEqual({key: result.get(key) for key in entry['expected']}, entry['expected'])
                self.assertEqual(result['release_status'], 'held')
                self.assertFalse(result.get('approval_effective', False))
                self.assertFalse(result.get('source_write_enabled', False))
                self.assertFalse(result.get('send_enabled', False))
        for artifact in self.index['supporting_source_snapshots']:
            self.assertEqual(hashlib.sha256((self.directory / artifact['path']).read_bytes()).hexdigest(), artifact['sha256'])

    def test_ppg_nonempty_quantities_and_unknown_rejects(self):
        result = self.evaluate('synthetic_ppg_active_tabs')
        self.assertEqual(result['template_id'], 'ppg-daily-active-tabs-report/4')
        self.assertEqual(result['record_count'], 2)
        self.assertEqual(result['numeric_values_compared'], 4)
        self.assertEqual(result['totals']['inspected']['total'], 34)
        self.assertIsNone(result['totals']['rejected']['total'])
        self.assertEqual(result['totals']['rejected']['missing_count'], 2)
        self.assertEqual(result['source_difference_count'], 0)
        self.assertEqual(result['absent_shift_ids'], ['2', '3'])
        source = json.loads((self.directory / self.examples['synthetic_ppg_active_tabs']['source']['path']).read_text())
        self.assertTrue(all(record['values']['rejected'] is None and record['field_presence']['rejected'] == 'absent' for record in source['records']))

    def test_ppg_injected_single_source_difference(self):
        result = self.evaluate('synthetic_ppg_source_mismatch')
        self.assertEqual(result['numeric_values_compared'], 4)
        self.assertEqual(result['source_difference_count'], 1)
        self.assertEqual(result['source_reconciliation_status'], 'mismatch')
        self.assertEqual(result['source_differences'][0]['field'], 'certified')
        self.assertEqual(result['source_differences'][0]['before'], 24)
        self.assertEqual(result['source_differences'][0]['source_value'], 23)

    def test_pratt_nine_sheets_and_distinct_grains(self):
        entry = self.examples['synthetic_pratt_template_union']
        raw = (self.directory / entry['artifact']['path']).read_bytes()
        self.assertEqual(len(load_workbook(io.BytesIO(raw)).sheetnames), 9)
        result = self.evaluate(entry['id'])
        self.assertEqual(result['template_id'], 'pratt-kn-template-union/2')
        self.assertEqual(result['numeric_values_compared'], 195)
        self.assertEqual(result['source_totals']['main']['completed'], 3)
        self.assertEqual(result['source_totals']['multi']['completed'], 2)
        self.assertEqual(result['matched_completed_record_count'], 5)
        self.assertGreater(result['unreported_template_field_count'], 100)
        source = json.loads((self.directory / entry['source']['path']).read_text())
        source['lists']['main']['list_key'] = source['lists']['multi']['list_key']
        with self.assertRaises(WorkflowError):
            reconcile_result(inspect_spreadsheet(entry['artifact']['path'], raw), rehash(source))

    def test_pratt_same_total_child_swap_detected(self):
        entry = self.examples['synthetic_pratt_template_union']
        workbook = load_workbook(self.directory / entry['artifact']['path'])
        workbook['1st Shift']['T7'] = 'Multi-pack child'
        raw = io.BytesIO()
        workbook.save(raw)
        source = json.loads((self.directory / entry['source']['path']).read_text())
        # Rebind this synthetic mutation only so group comparison, not hash gate, is exercised.
        source['expected_workbook_sha256'] = hashlib.sha256(raw.getvalue()).hexdigest()
        result = reconcile_result(inspect_spreadsheet('synthetic_mutated.xlsx', raw.getvalue()), rehash(source))
        self.assertIn('completed_record_group_difference', result['finding_counts'])
        self.assertEqual(result['source_reconciliation_status'], 'mismatch')

    def test_plain_csv_detects_numeric_issues(self):
        result = self.evaluate('synthetic_plain_numeric_issues')
        self.assertTrue({'inspected_count_mismatch', 'negative_count_or_quantity', 'fractional_count', 'invalid_numeric_value', 'missing_cell'} <= result['finding_counts'].keys())

    def test_builder_has_no_test_runtime_imports(self):
        import ast
        import hosting.build_development_examples as builder
        tree = ast.parse(Path(builder.__file__).read_text())
        imports = [node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)]
        self.assertFalse(any(name and (name.startswith('test') or '.test' in name) for name in imports))


if __name__ == '__main__':
    unittest.main()

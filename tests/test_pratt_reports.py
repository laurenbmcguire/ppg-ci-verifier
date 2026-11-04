"""Synthetic Pratt fixtures only. No client snapshots belong in this package."""
import copy
import hashlib
import io
import json
import unittest

from openpyxl import Workbook
from hosting.pratt_reports import (
    BRAND, COUNT_FIELDS, DEFECTS, DETAIL_HEADERS, FAILURE_HEADERS, LIST_KEYS,
    LOCATIONS, MAIN_GRAIN, MAIN_HEADERS, MULTI_GRAIN, MULTI_HEADERS,
    SUMMARY_BINDING, TEMPLATE_ID, build_pratt_source, formula_map, inspect_pratt,
    reconcile_pratt, fingerprint,
)
from hosting.report_workflow import WorkflowError


DATE = '2026-11-03'
MAIN_REF = 'synthetic://main'
MULTI_REF = 'synthetic://multi'


def row(identifier, *, role='main', status='Complete', result='Pass', shift='1',
        location='Commercial Bulk', export='No', defects=None):
    r = dict(ID=identifier, Id=identifier, Date=DATE + 'T06:00:00Z', Status=status,
             AuditResult=result, DeliveryNote='0000' + str(identifier), PartNumber='aB-00' + str(identifier),
             OrderQty_x002e_=7, ActualQty_x002e_=7, Export=export, Location=location)
    r['Shift' if role == 'main' else 'Shift_x002e_'] = shift
    r['Defects' if role == 'main' else 'Defect'] = None if not defects else {'results': defects}
    if role == 'main':
        r['Resolution'] = {'results': ['Inspect at shipping']} if result == 'Fail' else None
    return r


def snapshots():
    main = [row(11), row(12, result='Fail', export='Yes', defects=list(DEFECTS[:2])),
            row(13, status='In Progress', result=None, shift='2'),
            row(14, shift='3', location='446', export='Yes', defects=['Other recorded nonfailure category'])]
    # Same ID in different lists is allowed. In-progress Pass must not count.
    multi = [row(11, role='multi'), row(12, role='multi', status='In Progress'), row(13, role='multi', shift='2')]
    return ({'status': 200, 'next': None, 'data': main}, {'status': 200, 'next': None, 'data': multi})


def workbook(main=None, multi=None):
    default_main, default_multi = snapshots()
    main = default_main['data'] if main is None else main
    multi = default_multi['data'] if multi is None else multi
    w = Workbook()
    summary = w.active
    summary.title = 'Daily Summary'
    detail = w.create_sheet('Shift Detail')
    failure = w.create_sheet('Failure Summary')
    summary['A2'] = BRAND
    summary['A3'] = 'Daily audit report | November 3, 2026'
    summary['A33'] = 'Multi-pack children are shown separately. Do not add these records to the Main audit total.'
    for rownum, header in ((20, MAIN_HEADERS), (27, MULTI_HEADERS), (36, ('Recorded defect category', 'Occurrences'))):
        for col, value in enumerate(header, 1):
            summary.cell(rownum, col, value)
    for source_rows, start, field, columns in ((main, 21, 'Shift', (2, 3, 4, 6)), (multi, 28, 'Shift_x002e_', (2, 3, 4, 5))):
        for shift in ('1', '2', '3'):
            rows = [r for r in source_rows if r[field] == shift]
            complete = [r for r in rows if r['Status'] == 'Complete']
            values = [len(complete), sum(r['AuditResult'] == 'Pass' for r in complete), sum(r['AuditResult'] == 'Fail' for r in complete), sum(r['Status'] == 'In Progress' for r in rows)]
            rownum = start + int(shift) - 1
            summary.cell(rownum, 1, 'Shift ' + shift)
            for col, value in zip(columns, values):
                summary.cell(rownum, col, value)
        summary.cell(start + 3, 1, 'Daily total')
    failed = [r for r in main if r['Status'] == 'Complete' and r['AuditResult'] == 'Fail']
    for rownum, category in enumerate(DEFECTS, 37):
        summary.cell(rownum, 1, category)
        summary.cell(rownum, 2, sum(category in r['Defects']['results'] for r in failed))
    summary['A40'] = 'Total occurrences'
    detail['A2'] = 'Completed Main audits by shift'
    for col, value in enumerate(DETAIL_HEADERS, 1):
        detail.cell(6, col, value)
    rownum = 7
    for shift in ('1', '2', '3'):
        for location in LOCATIONS:
            for export in ('No', 'Yes'):
                rows = [r for r in main if r['Status'] == 'Complete' and r['Shift'] == shift and r['Location'] == location and r['Export'] == export]
                failures = [r for r in rows if r['AuditResult'] == 'Fail']
                values = [int(shift), location, 'Domestic' if export == 'No' else 'Export', len(rows), sum(r['AuditResult'] == 'Pass' for r in rows), len(failures)]
                values.extend(sum(category in r['Defects']['results'] for r in failures) for category in DEFECTS)
                for col, value in enumerate(values, 1):
                    detail.cell(rownum, col, value)
                rownum += 1
    detail['A31'] = 'Daily total'
    failure['A2'] = 'Failure summary'
    failure['A3'] = str(len(failed)) + ' completed Main audits with recorded failures'
    for col, value in enumerate(FAILURE_HEADERS, 1):
        failure.cell(6, col, value)
    for rownum, r in enumerate(failed, 7):
        values = [r['DeliveryNote'], r['PartNumber'], r['OrderQty_x002e_'], r['ActualQty_x002e_'], '; '.join(r['Defects']['results']), r['Export'], int(r['Shift']), r['Location'], '; '.join(r['Resolution']['results'])]
        for col, value in enumerate(values, 1):
            failure.cell(rownum, col, value)
    for (sheet, address), value in formula_map().items():
        w[sheet][address] = value
    return w


def report(w=None):
    w = w or workbook()
    buffer = io.BytesIO()
    w.save(buffer)
    raw = buffer.getvalue()
    base = dict(file_sha256=hashlib.sha256(raw).hexdigest(), checker_version='synthetic-test', filename='Synthetic.xlsx')
    return inspect_pratt([(sheet.title, list(sheet.values)) for sheet in w], base)


def source(r=None, main=None, multi=None):
    r = r or report()
    default_main, default_multi = snapshots()
    main_raw = json.dumps(default_main if main is None else main).encode()
    multi_raw = json.dumps(default_multi if multi is None else multi).encode()
    binding = dict(library_file_id='synthetic-file', file_sha256=r['file_sha256'], report_date=DATE,
                   main_grain=MAIN_GRAIN, multi_grain=MULTI_GRAIN, client_summary_binding=SUMMARY_BINDING,
                   source_sha256={MAIN_REF: hashlib.sha256(main_raw).hexdigest(), MULTI_REF: hashlib.sha256(multi_raw).hexdigest()})
    return build_pratt_source(json.dumps(binding).encode(), main_raw, multi_raw,
                              captured_at='2026-11-04T03:20:00Z',
                              main_source_reference=MAIN_REF, multi_source_reference=MULTI_REF)


def rehash(s):
    s.pop('source_sha256', None)
    s['source_sha256'] = fingerprint(s)
    return s


class PrattTests(unittest.TestCase):
    def test_explicit_layout_maps_distinct_grains(self):
        r = report()
        self.assertEqual(r['template_id'], TEMPLATE_ID)
        self.assertEqual(r['template_mapping_status'], 'matched')
        self.assertEqual(r['main_totals'], dict(completed=3, **{'pass': 2}, fail=1, in_progress=1))
        self.assertEqual(r['multi_totals']['completed'], 2)
        self.assertEqual(r['multi_totals']['in_progress'], 1)
        self.assertEqual(r['numeric_cells_checked'], 173)
        self.assertEqual(r['formula_count'], 19)
        self.assertEqual(r['finding_counts'], {'source_not_supplied': 1})

    def test_unrelated_template_does_not_claim_pratt(self):
        w = workbook()
        w['Daily Summary']['A2'] = 'Unrelated'
        self.assertIsNone(report(w))

    def test_complete_source_comparison_remains_held(self):
        r = report()
        checked = reconcile_pratt(r, source(r))
        self.assertEqual(checked['source_difference_count'], 0)
        self.assertEqual(checked['numeric_values_compared'], 173)
        self.assertEqual(checked['typed_source_identifiers_checked'], 14)
        self.assertEqual(checked['source_totals']['main']['completed'], 3)
        self.assertEqual(checked['source_totals']['multi']['completed'], 2)
        self.assertEqual(checked['source_defect_occurrences'], dict(zip(DEFECTS[:2], (1, 1))))
        self.assertEqual(checked['source_nonfailure_defect_records'], 1)
        self.assertEqual(checked['source_reconciliation_status'], 'observed_values_compared_with_limits')
        self.assertEqual(checked['release_status'], 'held')
        self.assertFalse(checked['approval_effective'])
        self.assertFalse(checked['source_write_enabled'])
        self.assertFalse(checked['send_enabled'])
        self.assertIn('source_list_identity_unverified', checked['finding_counts'])

    def test_unknown_sheet_not_silently_ignored(self):
        w = workbook()
        w.create_sheet('Raw rows')
        self.assertEqual(report(w)['template_mapping_status'], 'incomplete')

    def test_shifted_or_wrong_headers_not_guessed(self):
        w = workbook()
        w['Daily Summary']['B20'] = 'Quantity'
        r = report(w)
        self.assertEqual(r['template_mapping_status'], 'incomplete')
        self.assertEqual(r['main_shift_counts'], [])

    def test_extra_columns_block_mapping(self):
        w = workbook()
        w['Failure Summary']['J7'] = 999
        self.assertEqual(report(w)['template_mapping_status'], 'incomplete')

    def test_main_formula_cannot_include_child_counts(self):
        w = workbook()
        w['Daily Summary']['B24'] = '=SUM(B21:B23)+B31'
        self.assertIn('summary_formula_mapping_mismatch', report(w)['finding_counts'])

    def test_formula_in_detail_is_never_executed(self):
        w = workbook()
        w['Shift Detail']['D7'] = '=1'
        r = report(w)
        self.assertIn('quantity_invalid', r['finding_counts'])
        self.assertIn('unmapped_formula', r['finding_counts'])
        self.assertFalse(r['formulas_evaluated'])

    def test_invalid_numeric_literals_not_coerced(self):
        for value in (True, '2', -1, 1.5):
            with self.subTest(value=value):
                w = workbook()
                w['Daily Summary']['B21'] = value
                self.assertIn('quantity_invalid', report(w)['finding_counts'])

    def test_typed_failure_identifiers_preserved(self):
        r = report()
        self.assertEqual(r['records'][0]['delivery_note'], '000012')
        self.assertEqual(r['records'][0]['part_number'], 'aB-0012')
        w = workbook()
        w['Failure Summary']['A7'] = 12
        self.assertIn('typed_identifier_invalid', report(w)['finding_counts'])

    def test_same_total_wrong_group_detected(self):
        r = report()
        s = source(r)
        s['lists']['main']['records'][0]['location'] = 'Commercial Small'
        checked = reconcile_pratt(r, rehash(s))
        self.assertGreater(checked['source_difference_count'], 0)
        self.assertEqual(checked['source_reconciliation_status'], 'mismatch')

    def test_failure_quantity_difference_detected(self):
        r = report()
        s = source(r)
        s['lists']['main']['records'][1]['actual_quantity'] = 8
        checked = reconcile_pratt(r, rehash(s))
        self.assertEqual(checked['source_difference_count'], 1)
        self.assertEqual(checked['source_differences'][0]['field'], 'actual_quantity')

    def test_failure_identifier_case_change_detected(self):
        r = report()
        s = source(r)
        s['lists']['main']['records'][1]['part_number'] = 'AB-0012'
        checked = reconcile_pratt(r, rehash(s))
        self.assertIn('source_failure_group_difference', checked['finding_counts'])

    def test_source_integer_id_type_and_alias_preserved(self):
        for field, value in (('id', '11'), ('id', True), ('id_alias', 12)):
            with self.subTest(field=field, value=value):
                r = report()
                s = source(r)
                s['lists']['main']['records'][0][field] = value
                with self.assertRaises(WorkflowError):
                    reconcile_pratt(r, rehash(s))

    def test_duplicate_within_list_rejected(self):
        r = report()
        s = source(r)
        s['lists']['main']['records'].append(copy.deepcopy(s['lists']['main']['records'][0]))
        with self.assertRaises(WorkflowError):
            reconcile_pratt(r, rehash(s))

    def test_source_hash_tampering_rejected(self):
        r = report()
        s = source(r)
        s['lists']['main']['records'][0]['actual_quantity'] = 1
        with self.assertRaises(WorkflowError):
            reconcile_pratt(r, s)

    def test_unknown_status_blocks_all_source_comparisons(self):
        r = report()
        s = source(r)
        s['lists']['main']['records'][0]['status'] = 'Unknown'
        checked = reconcile_pratt(r, rehash(s))
        self.assertEqual(checked['numeric_values_compared'], 0)
        self.assertIn('source_status_or_shift_unmapped', checked['finding_counts'])
        self.assertEqual(checked['source_reconciliation_status'], 'unverified')

    def test_wrong_date_blocks_comparisons(self):
        r = report()
        s = source(r)
        s['lists']['main']['records'][0]['source_date'] = '2026-11-02T06:00:00Z'
        checked = reconcile_pratt(r, rehash(s))
        self.assertEqual(checked['numeric_values_compared'], 0)
        self.assertIn('source_date_scope_mismatch', checked['finding_counts'])

    def test_empty_source_cannot_pass_as_zero(self):
        r = report()
        s = source(r)
        s['lists']['main']['records'] = []
        checked = reconcile_pratt(r, rehash(s))
        self.assertEqual(checked['numeric_values_compared'], 0)
        self.assertIn('empty_source_evidence', checked['finding_counts'])
        self.assertIn('no_source_quantity_checks', checked['finding_counts'])

    def test_old_workbook_hash_not_rebound_implicitly(self):
        r = report()
        s = source(r)
        s['expected_workbook_sha256'] = '0' * 64
        checked = reconcile_pratt(r, rehash(s))
        self.assertEqual(checked['numeric_values_compared'], 0)
        self.assertIn('source_workbook_binding_changed', checked['finding_counts'])

    def test_source_lists_cannot_be_merged_or_swapped(self):
        r = report()
        s = source(r)
        s['lists']['main']['list_key'] = LIST_KEYS['multi']
        with self.assertRaises(WorkflowError):
            reconcile_pratt(r, rehash(s))

    def test_future_capture_rejected(self):
        r = report()
        s = source(r)
        s['captured_at'] = '2099-01-01T00:00:00Z'
        with self.assertRaises(WorkflowError):
            reconcile_pratt(r, rehash(s))

    def test_builder_checks_original_hash_before_projection(self):
        binding = dict(main_grain=MAIN_GRAIN, multi_grain=MULTI_GRAIN, client_summary_binding=SUMMARY_BINDING,
                       source_sha256={MAIN_REF: '0' * 64, MULTI_REF: '0' * 64})
        with self.assertRaisesRegex(WorkflowError, 'hash differs'):
            build_pratt_source(json.dumps(binding).encode(), b'{}', b'{}', captured_at='2026-11-04T03:20:00Z', main_source_reference=MAIN_REF, multi_source_reference=MULTI_REF)

    def test_builder_does_not_fill_absent_quantity_field(self):
        main, multi = snapshots()
        del main['data'][0]['ActualQty_x002e_']
        with self.assertRaisesRegex(WorkflowError, 'lacks explicitly'):
            source(main=main, multi=multi)

    def test_missing_failure_quantity_remains_unknown(self):
        r = report()
        s = source(r)
        s['lists']['main']['records'][1]['actual_quantity'] = None
        checked = reconcile_pratt(r, rehash(s))
        self.assertIn('source_quantity_missing', checked['finding_counts'])
        self.assertEqual(checked['numeric_values_compared'], 172)
        self.assertEqual(checked['source_difference_count'], 1)

    def test_binding_changes_when_evidence_changes(self):
        r = report()
        s = source(r)
        a = reconcile_pratt(r, s)
        s['captured_at'] = '2026-11-04T03:21:00Z'
        b = reconcile_pratt(r, rehash(s))
        self.assertNotEqual(a['review_binding_sha256'], b['review_binding_sha256'])

    def test_report_and_source_are_not_mutated(self):
        r = report()
        s = source(r)
        before_report = copy.deepcopy(r)
        before_source = copy.deepcopy(s)
        reconcile_pratt(r, s)
        self.assertEqual(r, before_report)
        self.assertEqual(s, before_source)

    def test_failure_actions_never_claim_completion(self):
        r = report()
        checked = reconcile_pratt(r, source(r))
        self.assertFalse(checked['source_write_enabled'])
        self.assertEqual(checked['records'][0]['resolution'], 'Inspect at shipping')
        self.assertNotIn('correction_completed', checked)


if __name__ == '__main__':
    unittest.main()


# The current template union adds explicit unknown production fields plus typed
# completed Main and child rows. Every value below is synthetic.
def union_workbook():
    from datetime import datetime
    from collections import Counter
    from hosting.pratt_reports import (ADDITIONAL_LABELS, ADDITIONAL_DEFECT_LABELS,
        ADDITIONAL_DEFECT_MAP, BILLING_LABELS, PRODUCTION_LABELS, SHIFT_NAMES, UNION_SHIFT_HEADERS)
    main, multi = snapshots()
    w = workbook()
    for col, label in enumerate(('Reject quantity', 'Comments', 'Defect Photo', 'Original'), 10):
        w['Failure Summary'].cell(6, col, label)
    sheet = w.create_sheet('Project and Production')
    sheet['A2'] = 'Project and production results'; sheet['A3'] = BRAND + ' | November 3, 2026'
    sheet['A6'] = 'Project details'; sheet['B6'] = 'Value'
    labels = ('Project Number', 'Report Date', 'Paying Client', 'Client Contact Name', 'Worksite Location', 'Area',
              'Project Service Type', 'Project Description', 'Prepared By PPG', 'Overall Daily Notes')
    for rownum, label in enumerate(labels, 7): sheet.cell(rownum, 1, label)
    sheet['B8'] = datetime(2026, 11, 3)
    for header_row, title, start, labels in ((19, 'Production measure', 20, PRODUCTION_LABELS), (35, 'Billing and productivity', 36, BILLING_LABELS)):
        for col, value in enumerate((title, '1st shift', '2nd shift', '3rd shift', 'Daily total'), 1): sheet.cell(header_row, col, value)
        for rownum, label in enumerate(labels, start): sheet.cell(rownum, 1, label)
    sheet['A47'] = 'Inspection notes'; sheet['A53'] = 'Shift comments'; sheet['B53'] = 'Comments'
    for rownum, label in enumerate(('1st shift', '2nd shift', '3rd shift'), 54): sheet.cell(rownum, 1, label)
    sheet['A59'] = 'Audit totals and outcomes are reported separately in Daily Summary. Production quantities are a different measure.'
    sheet = w.create_sheet('Additional Template Fields')
    sheet['A2'] = 'Additional inspection fields'; sheet['A3'] = BRAND + ' | November 3, 2026'
    for header_row, title, start, labels in ((6, 'Inspection measure', 7, ADDITIONAL_LABELS), (31, 'Pratt audit defect field', 32, ADDITIONAL_DEFECT_LABELS)):
        for col, value in enumerate((title, '1st shift', '2nd shift', '3rd shift', 'Daily total'), 1): sheet.cell(header_row, col, value)
        for rownum, label in enumerate(labels, start):
            sheet.cell(rownum, 1, label)
            if rownum in ADDITIONAL_DEFECT_MAP:
                for col, shift in enumerate(('1', '2', '3'), 2):
                    sheet.cell(rownum, col, sum(r['Status'] == 'Complete' and r['AuditResult'] == 'Fail' and r['Shift'] == shift and ADDITIONAL_DEFECT_MAP[rownum] in (r['Defects'] or {}).get('results', []) for r in main['data']))
                sheet.cell(rownum, 5, '=SUM(B%d:D%d)' % (rownum, rownum))
    for name, shift in SHIFT_NAMES.items():
        sheet = w.create_sheet(name)
        sheet['A2'] = name + ' inspection detail'; sheet['A3'] = BRAND + ' | November 3, 2026 | Completed records'
        for col, label in enumerate(UNION_SHIFT_HEADERS, 1): sheet.cell(6, col, label)
        rownum = 7
        for role, rows in (('main', main['data']), ('multi', multi['data'])):
            for r in rows:
                if r['Status'] != 'Complete' or r['Shift' if role == 'main' else 'Shift_x002e_'] != shift: continue
                defects = (r['Defects' if role == 'main' else 'Defect'] or {}).get('results', [])
                values = [datetime(2026, 11, 3), int(shift), r['Location'], r['PartNumber'], r['DeliveryNote'], None, None, None, None, None, None, None, None,
                          r['AuditResult'], '; '.join(defects) or None, r['OrderQty_x002e_'], r['ActualQty_x002e_'], r['Export'],
                          '; '.join((r.get('Resolution') or {}).get('results', [])) or None, 'Main audit' if role == 'main' else 'Multi-pack child']
                for col, value in enumerate(values, 1): sheet.cell(rownum, col, value)
                rownum += 1
        end = rownum - 1
        sheet.cell(end + 3, 1, 'Shift inspection results'); sheet.cell(end + 3, 2, 'Value')
        for rownum, label in enumerate(('Boxes Inspected', 'Total Parts Inspected', 'Total Certified', 'Total Rejected', 'Defect Rate', 'Inspection Pass %'), end + 4): sheet.cell(rownum, 1, label)
        sheet.cell(end + 12, 1, 'Billing'); sheet.cell(end + 12, 2, 'Value')
        for rownum, label in enumerate(('Team Member / Inspector', 'Hours on Shift', 'Working Supervisor', '2-Hour Supervisor Charge', 'Billable Shift Hours', 'Shift Productivity'), end + 13): sheet.cell(rownum, 1, label)
    sheet = w.create_sheet('Results by Part')
    sheet['A2'] = 'Results by part number'; sheet['A3'] = 'Completed Main audits | November 3, 2026'
    for col, label in enumerate(('Part number', 'Part name', 'Inspected for', 'Inspected', 'Certified', 'Rejected', 'Defect rate', 'Main audits'), 1): sheet.cell(6, col, label)
    for rownum, (part, count) in enumerate(Counter(r['PartNumber'] for r in main['data'] if r['Status'] == 'Complete').items(), 7):
        sheet.cell(rownum, 1, part); sheet.cell(rownum, 8, count)
    return w


class PrattUnionTests(unittest.TestCase):
    def test_union_reconciles_all_completed_rows_and_separate_counts(self):
        r = report(union_workbook()); checked = reconcile_pratt(r, source(r))
        self.assertEqual(r['template_id'], 'pratt-kn-template-union/2')
        self.assertEqual(r['template_mapping_status'], 'matched')
        self.assertEqual(r['formula_count'], 22)
        self.assertEqual(r['record_count'], 5)
        self.assertEqual(checked['matched_completed_record_count'], 5)
        self.assertEqual(checked['source_difference_count'], 0)
        self.assertEqual(checked['numeric_values_compared'], 195)
        self.assertGreater(r['unreported_template_field_count'], 100)
        self.assertEqual(checked['release_status'], 'held')

    def test_union_unknown_production_value_not_inferred_as_valid(self):
        w = union_workbook(); w['1st Shift']['G7'] = 7
        r = report(w)
        self.assertIn('unmapped_production_value', r['finding_counts'])
        self.assertEqual(r['release_status'], 'held')

    def test_union_case_sensitive_completed_identifier_mismatch(self):
        w = union_workbook(); w['1st Shift']['D7'] = 'AB-0011'
        r = report(w); checked = reconcile_pratt(r, source(r))
        self.assertIn('completed_record_group_difference', checked['finding_counts'])
        self.assertEqual(checked['source_reconciliation_status'], 'mismatch')

    def test_union_main_child_swap_not_hidden_by_total(self):
        w = union_workbook(); w['1st Shift']['T7'] = 'Multi-pack child'
        r = report(w); checked = reconcile_pratt(r, source(r))
        self.assertIn('completed_record_group_difference', checked['finding_counts'])

    def test_union_part_count_not_order_quantity(self):
        w = union_workbook(); w['Results by Part']['H7'] = 7
        r = report(w); checked = reconcile_pratt(r, source(r))
        self.assertIn('part_audit_count_difference', checked['finding_counts'])

    def test_union_additional_defect_count_is_independently_compared(self):
        w = union_workbook(); w['Additional Template Fields']['B35'] = 9
        r = report(w); checked = reconcile_pratt(r, source(r))
        self.assertIn('additional_defect_difference', checked['finding_counts'])

    def test_union_missing_sheet_fails_closed(self):
        w = union_workbook(); del w['Additional Template Fields']
        r = report(w)
        self.assertEqual(r['template_mapping_status'], 'incomplete')
        self.assertEqual(r['internal_check_status'], 'unverified')
        with self.assertRaises(WorkflowError): reconcile_pratt(r, source(r))

    def test_union_unknown_extra_cell_fails_closed(self):
        w = union_workbook(); w['Project and Production']['H20'] = 999
        self.assertEqual(report(w)['template_mapping_status'], 'incomplete')

    def test_union_float_quantities_compare_numerically(self):
        r = report(union_workbook()); s = source(r)
        s['lists']['main']['records'][0]['actual_quantity'] = 7.0
        checked = reconcile_pratt(r, rehash(s))
        self.assertEqual(checked['source_difference_count'], 0)

    def test_union_blank_quantity_is_unknown_not_zero(self):
        w = union_workbook(); w['1st Shift']['Q7'] = None
        r = report(w); checked = reconcile_pratt(r, source(r))
        self.assertIn('quantity_missing', checked['finding_counts'])
        self.assertIn('completed_record_group_difference', checked['finding_counts'])


class PrattCaptureTests(unittest.TestCase):
    def test_unknown_capture_is_explicit_and_never_fresh(self):
        r = report(union_workbook()); s = source(r)
        s['captured_at'] = None; s['capture_time_status'] = 'unknown'
        checked = reconcile_pratt(r, rehash(s))
        self.assertIsNone(checked['source_captured_at'])
        self.assertEqual(checked['source_capture_time_status'], 'unknown')
        self.assertEqual(checked['source_freshness_status'], 'unverified')
        self.assertIn('source_capture_time_unverified', checked['finding_counts'])
        self.assertEqual(checked['source_difference_count'], 0)
        self.assertEqual(checked['release_status'], 'held')

    def test_unknown_capture_requires_explicit_status(self):
        r = report(); s = source(r); s['captured_at'] = None
        with self.assertRaises(WorkflowError): reconcile_pratt(r, rehash(s))

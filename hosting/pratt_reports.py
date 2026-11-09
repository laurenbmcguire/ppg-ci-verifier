"""Explicit Pratt / K+N audit-report mapping and read-only source comparison.

Main audit IDs and multi-pack child IDs belong to separate list namespaces.
Summary counts are audit counts, never order/actual quantities. Formulas are
matched as text, never executed. No input file, source service, or release gate
is changed here. A match to retained evidence remains provisional and held.
"""
from collections import Counter, defaultdict
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json
import re

from hosting.branded_reports import canonical, fingerprint, quantity
from hosting.report_workflow import WorkflowError, parse, stamp

TEMPLATE_ID = 'pratt-kn-daily-audit/1'
SOURCE_SCHEMA = 'verifier-pratt-source/1'
BRAND = 'Pratt & Whitney / K+N'
LOCATIONS = ('Commercial Bulk', 'Commercial Small', 'Military Small', '446')
DEFECTS = ('Export Documents Missing & Not legible',
           'Back Label Missing & Not legible', 'Not Certified')
MAIN_HEADERS = ('Main audits', 'Completed', 'Pass', 'Fail', 'Pass rate', 'In progress')
MULTI_HEADERS = ('Multi-pack child records', 'Completed', 'Pass', 'Fail', 'In progress')
DETAIL_HEADERS = ('Shift', 'Location', 'DN type', 'Completed', 'Pass', 'Fail',
                  'Export docs', 'Back label', 'Not certified')
FAILURE_HEADERS = ('Delivery note', 'Part number', 'Order qty', 'Actual qty',
                   'Defects', 'Export', 'Shift', 'Location', 'Recorded action')
MAIN_GRAIN = 'DCA Main Audit list + ID, Status Complete'
MULTI_GRAIN = 'Multi-pack list + ID, separately displayed; not added to Main'
SUMMARY_BINDING = {'completed': 'Daily Summary!B24', 'pass': 'Daily Summary!C24',
                   'fail': 'Daily Summary!D24', 'in_progress': 'Daily Summary!F24',
                   'multi_complete': 'Daily Summary!B31',
                   'defect_occurrences': 'Daily Summary!B40'}
LIST_KEYS = {'main': 'DCA Main Audit', 'multi': 'Multi-pack'}
from hosting.limits import MAX_FINDINGS  # every finding is stored (bounded only against hostile files)
MAX_RECORDS = 10000
COUNT_FIELDS = ('completed', 'pass', 'fail', 'in_progress')


def cell(rows, row, column):
    return rows[row - 1][column - 1] if row <= len(rows) and column <= len(rows[row - 1]) else None


def coordinates(column, row):
    name = ''
    while column:
        column, remainder = divmod(column - 1, 26)
        name = chr(65 + remainder) + name
    return name + str(row)


def formula_map():
    result = {}
    for row in (21, 22, 23, 24):
        result[('Daily Summary', 'E' + str(row))] = '=C%d/B%d' % (row, row)
    for column in ('B', 'C', 'D', 'F'):
        result[('Daily Summary', column + '24')] = '=SUM(%s21:%s23)' % (column, column)
    for column in ('B', 'C', 'D', 'E'):
        result[('Daily Summary', column + '31')] = '=SUM(%s28:%s30)' % (column, column)
    result[('Daily Summary', 'B40')] = '=SUM(B37:B39)'
    for column in ('D', 'E', 'F', 'G', 'H', 'I'):
        result[('Shift Detail', column + '31')] = '=SUM(%s7:%s30)' % (column, column)
    return result


def _total(rows, field):
    values = [row[field] for row in rows]
    if not values or any(value is None for value in values):
        return None
    total = sum(values)
    return total if total <= 10**15 else None


def _text(value):
    return isinstance(value, str) and bool(value.strip()) and not value.startswith('=')


def _findings(result):
    counts = Counter(result.get('finding_counts', {}))

    def add(code, message, sheet='Workbook', row=None, column=None):
        counts[code] += 1
        if len(result['findings']) < MAX_FINDINGS:
            result['findings'].append(dict(code=code, severity='blocking', message=message,
                                           sheet=sheet, row=row, column=column))
    return counts, add


def _finish(result, counts):
    result.update(finding_counts=dict(counts), finding_count=sum(counts.values()),
                  findings_truncated=sum(counts.values()) > MAX_FINDINGS,
                  approval_effective=False, release_status='held',
                  source_write_enabled=False, send_enabled=False)
    return result


def _inspect_summary(sources, base):
    """Map only the exact documented three-sheet layout, never infer row offsets."""
    sheets = dict(sources)
    if cell(sheets.get('Daily Summary', []), 2, 1) != BRAND:
        return None
    result = dict(base, template_id=TEMPLATE_ID, template_mapping_status='matched',
                  findings=[], finding_counts={}, sheets=[], records=[], formulas=[],
                  main_shift_counts=[], multi_shift_counts=[], detail_groups=[],
                  defect_occurrences={}, numeric_cells_checked=0,
                  source_authenticity='not_supplied', source_reconciliation_status='not_performed',
                  report_identity={'project_number': BRAND, 'report_date': None},
                  main_grain=MAIN_GRAIN, multi_grain=MULTI_GRAIN,
                  limitations=[
                      'Explicit three-sheet Pratt / K+N layout only; no nearby-header guessing.',
                      'Counts refer to Main audits or separately displayed multi-pack children, not quantities.',
                      'Only failure rows are individually displayed. Other audit rows can be compared only as aggregates.',
                      'Formula text is checked, never executed; cached values and chart/prose contents are not verified.',
                      'Source completeness, stable service list identity, business timezone and shift closeout remain unverified.',
                  ])
    counts, finding = _findings(result)

    def mapping(condition, message, sheet, row=None, column=None):
        if not condition:
            finding('template_mapping_mismatch', message, sheet, row, column)
            result['template_mapping_status'] = 'incomplete'
        return condition

    def number(value, sheet, row, column):
        number_value = quantity(value)
        if number_value is None:
            finding('quantity_missing' if value in (None, '') else 'quantity_invalid',
                    'Unknown or invalid count/quantity was not converted to zero.', sheet, row, column)
        else:
            result['numeric_cells_checked'] += 1
        return number_value

    mapping(len(sources) == 3 and set(sheets) == {'Daily Summary', 'Shift Detail', 'Failure Summary'},
            'Expected precisely Daily Summary, Shift Detail and Failure Summary.', 'Workbook')
    summary = sheets.get('Daily Summary', [])
    title = cell(summary, 3, 1)
    if isinstance(title, str) and title.startswith('Daily audit report | '):
        try:
            result['report_identity']['report_date'] = datetime.strptime(title[21:], '%B %d, %Y').date().isoformat()
        except ValueError:
            pass
    mapping(result['report_identity']['report_date'] is not None,
            'Report date must be in the mapped daily report title.', 'Daily Summary', 3, 1)
    main_mapped = mapping(tuple(cell(summary, 20, c) for c in range(1, 7)) == MAIN_HEADERS,
                          'Main header row must match exactly.', 'Daily Summary', 20)
    multi_mapped = mapping(tuple(cell(summary, 27, c) for c in range(1, 6)) == MULTI_HEADERS,
                           'Multi-pack header row must match exactly.', 'Daily Summary', 27)
    mapping(cell(summary, 33, 1) == 'Multi-pack children are shown separately. Do not add these records to the Main audit total.',
            'The Main / child grain separation notice is required.', 'Daily Summary', 33, 1)
    for role, start, columns, mapped in (
            ('main', 21, (2, 3, 4, 6), main_mapped),
            ('multi', 28, (2, 3, 4, 5), multi_mapped)):
        for shift, rownum in enumerate(range(start, start + 3), 1):
            mapping(cell(summary, rownum, 1) == 'Shift ' + str(shift),
                    'Expected explicit shift order 1, 2, 3.', 'Daily Summary', rownum, 1)
            if not mapped:
                continue
            values = {field: number(cell(summary, rownum, col), 'Daily Summary', rownum, col)
                      for field, col in zip(COUNT_FIELDS, columns)}
            if all(values[field] is not None for field in ('completed', 'pass', 'fail')):
                if values['completed'] != values['pass'] + values['fail']:
                    finding('audit_result_balance_mismatch', 'Completed must equal Pass plus Fail for the mapped layout.',
                            'Daily Summary', rownum)
            result[role + '_shift_counts'].append(dict(shift=str(shift), row=rownum, **values))
        mapping(cell(summary, start + 3, 1) == 'Daily total', 'Daily total label is required.',
                'Daily Summary', start + 3, 1)
    defect_mapped = mapping(tuple(cell(summary, 36, c) for c in (1, 2)) == ('Recorded defect category', 'Occurrences'),
                             'Defect header must match exactly.', 'Daily Summary', 36)
    for rownum, category in enumerate(DEFECTS, 37):
        mapping(cell(summary, rownum, 1) == category, 'Defect category order must match exactly.', 'Daily Summary', rownum, 1)
        if defect_mapped:
            result['defect_occurrences'][category] = number(cell(summary, rownum, 2), 'Daily Summary', rownum, 2)
    mapping(cell(summary, 40, 1) == 'Total occurrences', 'Defect total label is required.', 'Daily Summary', 40, 1)
    detail = sheets.get('Shift Detail', [])
    detail_mapped = mapping(cell(detail, 2, 1) == 'Completed Main audits by shift' and
                            tuple(cell(detail, 6, c) for c in range(1, 10)) == DETAIL_HEADERS,
                            'Completed Main detail title and row-6 headers must match.', 'Shift Detail', 6)
    rownum = 7
    for shift in ('1', '2', '3'):
        for location in LOCATIONS:
            for export in ('No', 'Yes'):
                actual_shift = cell(detail, rownum, 1)
                label = 'Domestic' if export == 'No' else 'Export'
                mapping(type(actual_shift) is int and actual_shift == int(shift) and
                        cell(detail, rownum, 2) == location and cell(detail, rownum, 3) == label,
                        'Detail group keys must retain exact shift, location and Domestic / Export type.', 'Shift Detail', rownum)
                if detail_mapped:
                    values = {field: number(cell(detail, rownum, col), 'Shift Detail', rownum, col)
                              for field, col in zip(('completed', 'pass', 'fail', *DEFECTS), range(4, 10))}
                    if all(values[field] is not None for field in ('completed', 'pass', 'fail')) and values['completed'] != values['pass'] + values['fail']:
                        finding('audit_result_balance_mismatch', 'Detail completed count must equal Pass plus Fail.', 'Shift Detail', rownum)
                    result['detail_groups'].append(dict(shift=shift, location=location, export=export,
                                                        row=rownum, **values))
                rownum += 1
    mapping(cell(detail, 31, 1) == 'Daily total', 'Detail total label is required.', 'Shift Detail', 31, 1)
    failures = sheets.get('Failure Summary', [])
    failure_mapped = mapping(cell(failures, 2, 1) == 'Failure summary' and
                             tuple(cell(failures, 6, c) for c in range(1, 10)) == FAILURE_HEADERS,
                             'Failure title and row-6 headers must match.', 'Failure Summary', 6)
    if failure_mapped:
        for rownum in range(7, len(failures) + 1):
            row = [cell(failures, rownum, c) for c in range(1, 10)]
            if row[0] == 'Recorded actions are source instructions, not confirmed corrections. Order and actual quantities are not audit counts.':
                mapping(all(value in (None, '') for value in row[1:]), 'Failure notice row must not conceal data.', 'Failure Summary', rownum)
                continue
            if not any(value not in (None, '') for value in row):
                continue
            for column in (1, 2):
                if not _text(row[column - 1]):
                    finding('typed_identifier_invalid', 'Delivery notes and part numbers must remain text, with leading zeros and case preserved.',
                            'Failure Summary', rownum, column)
            for column in (5, 6, 8, 9):
                if not _text(row[column - 1]):
                    finding('failure_field_missing', 'Failure classification/action must be an explicit text field.', 'Failure Summary', rownum, column)
            if type(row[6]) is not int or row[6] not in (1, 2, 3) or row[5] not in ('No', 'Yes') or row[7] not in LOCATIONS:
                finding('failure_scope_invalid', 'Failure shift, export or location is outside the mapped scope.', 'Failure Summary', rownum)
            defects = row[4].split('; ') if _text(row[4]) else []
            if not defects or len(set(defects)) != len(defects) or any(category not in DEFECTS for category in defects):
                finding('failure_defects_invalid', 'Failure categories are missing, duplicated or outside the explicit category mapping.', 'Failure Summary', rownum, 5)
            result['records'].append(dict(sheet='Failure Summary', row=rownum, delivery_note=row[0], part_number=row[1],
                                          order_quantity=number(row[2], 'Failure Summary', rownum, 3),
                                          actual_quantity=number(row[3], 'Failure Summary', rownum, 4),
                                          defects=defects, export=row[5], shift=str(row[6]) if type(row[6]) is int else None,
                                          location=row[7], resolution=row[8]))
    expected = formula_map()
    for (name, address), formula in expected.items():
        match = re.fullmatch(r'([A-Z]+)([0-9]+)', address)
        col = 0
        for character in match[1]:
            col = col * 26 + ord(character) - 64
        if cell(sheets.get(name, []), int(match[2]), col) != formula:
            finding('summary_formula_mapping_mismatch', 'Formula must reference precisely the mapped Main, child or defect range; it was not executed.', name, int(match[2]), col)
    for name, rows in sources:
        for rownum, row in enumerate(rows, 1):
            for col, value in enumerate(row, 1):
                if isinstance(value, str) and value.startswith('='):
                    result['formulas'].append(dict(sheet=name, row=rownum, column=col, formula=value))
                    if (name, coordinates(col, rownum)) not in expected:
                        finding('unmapped_formula', 'Formula lies outside the explicit mapped cells and was not evaluated.', name, rownum, col)
    for role in ('main', 'multi'):
        result[role + '_totals'] = {field: _total(result[role + '_shift_counts'], field) for field in COUNT_FIELDS}
        for field in COUNT_FIELDS:
            values = [row[field] for row in result[role + '_shift_counts']]
            if values and all(value is not None for value in values) and sum(values) > 10**15:
                finding('quantity_aggregate_limit', 'Aggregate count exceeds the supported exact numeric range; no rounded total was accepted.')
    # Content outside this documented mapping cannot silently vanish from checks.
    allowed = {'Daily Summary': {(2, 1), (3, 1), (33, 1), (42, 1), (44, 1), (45, 1), (46, 1), (47, 1)},
               'Shift Detail': {(2, 1), (3, 1), (34, 1), (35, 1)},
               'Failure Summary': {(2, 1), (3, 1)}}
    for first, last, width in ((20, 24, 6), (27, 31, 5), (36, 40, 2)):
        allowed['Daily Summary'].update((r, c) for r in range(first, last + 1) for c in range(1, width + 1))
    allowed['Shift Detail'].update((r, c) for r in range(6, 31) for c in range(1, 10))
    allowed['Shift Detail'].update((31, c) for c in (1, 4, 5, 6, 7, 8, 9))
    allowed['Failure Summary'].update((6, c) for c in range(1, 10))
    allowed['Failure Summary'].update((record['row'], c) for record in result['records'] for c in range(1, 10))
    for r, row in enumerate(failures, 1):
        if cell(failures, r, 1) == 'Recorded actions are source instructions, not confirmed corrections. Order and actual quantities are not audit counts.':
            allowed['Failure Summary'].add((r, 1))
    for name, rows in sources:
        for r, row in enumerate(rows, 1):
            for c, value in enumerate(row, 1):
                if value not in (None, '') and (r, c) not in allowed.get(name, set()):
                    mapping(False, 'Populated cell is outside the documented Pratt mapping; it was not silently ignored.', name, r, c)
    if result['main_totals']['fail'] is not None:
        if cell(failures, 3, 1) != '%d completed Main audits with recorded failures' % result['main_totals']['fail']:
            finding('failure_title_count_mismatch', 'Failure title count disagrees with mapped Main failure counts.', 'Failure Summary', 3, 1)
    for summary_row in result['main_shift_counts']:
        groups = [group for group in result['detail_groups'] if group['shift'] == summary_row['shift']]
        for field in ('completed', 'pass', 'fail'):
            total = _total(groups, field)
            if total is not None and summary_row[field] is not None and total != summary_row[field]:
                finding('detail_summary_mismatch', 'Main detail and summary counts disagree.', 'Daily Summary', summary_row['row'])
    if failure_mapped and result['main_totals']['fail'] is not None and len(result['records']) != result['main_totals']['fail']:
        finding('failure_row_count_mismatch', 'Failure detail count differs from Main failed-audit count.', 'Failure Summary')
    for category, value in result['defect_occurrences'].items():
        observed = sum(record['defects'].count(category) for record in result['records'])
        detail_total = _total(result['detail_groups'], category)
        if value is not None and (value != observed or (detail_total is not None and value != detail_total)):
            finding('defect_occurrence_mismatch', 'Category occurrences disagree with mapped failed audits or detail groups.', 'Daily Summary')
    result.update(record_count=len(result['records']), row_count=sum(len(rows) for _, rows in sources),
                  cell_count=sum(len(row) for _, rows in sources for row in rows),
                  formula_count=len(result['formulas']), formula_sha256=fingerprint(result['formulas']),
                  formulas_preserved=True, formulas_evaluated=False,
                  verification_scope='mapped_pratt_internal_checks_only',
                  internal_check_status='findings' if result['numeric_cells_checked'] and result['template_mapping_status'] == 'matched' else 'unverified')
    result['sheets'] = [dict(name=name, role=role, header_row=header, row_count=len(sheets.get(name, [])),
                              data_row_count=count, numeric_columns_checked=list(columns), mapping_status=result['template_mapping_status'])
                        for name, role, header, count, columns in (
                            ('Daily Summary', 'separate_audit_count_summaries', 20, 6, COUNT_FIELDS),
                            ('Shift Detail', 'completed_main_groups', 6, len(result['detail_groups']), ('completed', 'pass', 'fail', *DEFECTS)),
                            ('Failure Summary', 'failed_main_details', 6, len(result['records']), ('order_quantity', 'actual_quantity')))]
    if not result['numeric_cells_checked']:
        finding('no_quantity_checks_performed', 'No mapped numeric cells were checked; empty checks cannot pass.')
    finding('source_not_supplied', 'No separately supplied, hash-bound Main and multi-pack snapshots have been compared.')
    return _finish(result, counts)


def _collection(value, field):
    if value is None:
        return []
    if isinstance(value, dict):
        value = value.get('results')
    if not isinstance(value, list) or len(value) > 100 or any(not _text(item) for item in value):
        raise WorkflowError('Source ' + field + ' must be a bounded text collection or explicit null.')
    return value


def build_pratt_source(binding_raw, main_raw, multi_raw, *, captured_at,
                       main_source_reference, multi_source_reference, target_binding_raw=None):
    """Hash-check separately supplied original bytes, then project bounded fields.

    No filesystem/network access. Caller must keep the original evidence outside
    distributable code. ``captured_at`` is a caller-supplied observation timestamp,
    not a claim this module fetched the data. Original hashes are evidence bindings,
    not service authentication. Missing source fields are never synthesized.
    """
    if not all(isinstance(value, bytes) for value in (binding_raw, main_raw, multi_raw)):
        raise WorkflowError('Binding and both snapshots must be original bytes.')
    binding = parse(binding_raw)
    if binding.get('main_grain') != MAIN_GRAIN or binding.get('multi_grain') != MULTI_GRAIN or binding.get('client_summary_binding') != SUMMARY_BINDING:
        raise WorkflowError('Pratt source binding must name the exact Main/child grains and mapped summary cells.')
    source_hashes = binding.get('source_sha256')
    if not isinstance(source_hashes, dict) or set(source_hashes) != {main_source_reference, multi_source_reference} or main_source_reference == multi_source_reference:
        raise WorkflowError('Binding must identify exactly two separate source artifacts.')
    source = dict(schema=SOURCE_SCHEMA, provenance_kind='retained_supplied_snapshot',
                  captured_at=captured_at, capture_time_status='unknown' if captured_at is None else 'recorded',
                  report_date=binding.get('report_date'),
                  expected_workbook_sha256=binding.get('file_sha256'),
                  library_file_id=binding.get('library_file_id'),
                  source_reference='separate hash-bound Main and multi-pack retained snapshots',
                  binding_sha256=hashlib.sha256(binding_raw).hexdigest(),
                  main_grain=MAIN_GRAIN, multi_grain=MULTI_GRAIN, lists={})
    if target_binding_raw is not None:
        if not isinstance(target_binding_raw, bytes):
            raise WorkflowError('Updated target binding must be original bytes.')
        target = parse(target_binding_raw)
        required_cells = {'Main completed': 'Daily Summary!B24', 'Main pass': 'Daily Summary!C24', 'Main fail': 'Daily Summary!D24',
                          'Main in progress': 'Daily Summary!F24', 'Multi completed': 'Daily Summary!B31', 'Defect occurrences': 'Daily Summary!B40'}
        if target.get('summary_cells') != required_cells:
            raise WorkflowError('Updated target binding must preserve the explicit separate summary cell meanings.')
        source.update(expected_workbook_sha256=target.get('client_sha256'), library_file_id=target.get('client_library_file_id'),
                      target_binding_sha256=hashlib.sha256(target_binding_raw).hexdigest())
    for role, raw, reference in (('main', main_raw, main_source_reference), ('multi', multi_raw, multi_source_reference)):

        digest = hashlib.sha256(raw).hexdigest()
        if source_hashes[reference] != digest:
            raise WorkflowError('Original ' + role + ' source hash differs from the binding.')
        snapshot = parse(raw)
        if snapshot.get('status') != 200 or not isinstance(snapshot.get('data'), list) or len(snapshot['data']) > MAX_RECORDS:
            raise WorkflowError('Source snapshot must contain successful, bounded data rows.')
        projected = []
        for row in snapshot['data']:
            if not isinstance(row, dict):
                raise WorkflowError('Source rows must be objects.')
            required = {'ID', 'Id', 'Date', 'Status', 'AuditResult', 'DeliveryNote', 'PartNumber',
                        'OrderQty_x002e_', 'ActualQty_x002e_', 'Export', 'Location',
                        'Shift' if role == 'main' else 'Shift_x002e_', 'Defects' if role == 'main' else 'Defect'}
            if not required.issubset(row):
                raise WorkflowError('Source row lacks explicitly required fields; missing is not zero/null.')
            projected.append(dict(id=row['ID'], id_alias=row['Id'], source_date=row['Date'],
                                  status=row['Status'], audit_result=row['AuditResult'],
                                  shift=row['Shift' if role == 'main' else 'Shift_x002e_'],
                                  delivery_note=row['DeliveryNote'], part_number=row['PartNumber'],
                                  order_quantity=row['OrderQty_x002e_'], actual_quantity=row['ActualQty_x002e_'],
                                  export=row['Export'], location=row['Location'],
                                  defects=_collection(row['Defects' if role == 'main' else 'Defect'], 'defects'),
                                  resolution=_collection(row.get('Resolution'), 'resolution'),
                                  comments=row.get('Comments'), comments_presence='absent' if 'Comments' not in row else ('null' if row['Comments'] is None else 'present')))
        source['lists'][role] = dict(list_key=LIST_KEYS[role], source_reference=reference,
                                     original_sha256=digest, source_run_id=snapshot.get('run'), terminal_page_observed='next' in snapshot and snapshot['next'] is None,
                                     records=projected)
    source['source_sha256'] = fingerprint(source)
    # Applies the same byte limit as source reconciliation/storage.
    return parse(source)


def _reconcile_summary(report, source):
    """Compare known numeric fields and failure-row multisets; remain held always."""
    source = parse(source)
    claimed = source.get('source_sha256')
    if source.get('schema') != SOURCE_SCHEMA or claimed != fingerprint({key: value for key, value in source.items() if key != 'source_sha256'}):
        raise WorkflowError('Unsupported Pratt source schema or source evidence hash mismatch.')
    if report.get('template_id') != TEMPLATE_ID or report.get('template_mapping_status') != 'matched':
        raise WorkflowError('Pratt reconciliation requires the exact supported template mapping.')
    if source.get('main_grain') != MAIN_GRAIN or source.get('multi_grain') != MULTI_GRAIN or source.get('provenance_kind') != 'retained_supplied_snapshot':
        raise WorkflowError('Explicit retained-source provenance and separate audit grains are required.')
    if source.get('captured_at') is None:
        if source.get('capture_time_status') != 'unknown':
            raise WorkflowError('Absent source capture time requires explicit unknown status.')
    elif stamp(source['captured_at']) > datetime.now(timezone.utc) + timedelta(minutes=5):
        raise WorkflowError('Source capture time is in the future.')
    lists = source.get('lists')
    if not isinstance(lists, dict) or set(lists) != set(LIST_KEYS):
        raise WorkflowError('Separate Main and multi-pack list projections are required.')
    for key in ('expected_workbook_sha256', 'binding_sha256'):
        if not isinstance(source.get(key), str) or not re.fullmatch('[a-f0-9]{64}', source[key]):
            raise WorkflowError('Exact workbook and original binding hashes are required.')
    result = deepcopy(report)
    result['findings'] = [item for item in result['findings'] if item['code'] != 'source_not_supplied']
    result['finding_counts'].pop('source_not_supplied', None)
    counts, finding = _findings(result)
    comparisons = []
    numeric_compared = 0
    differences = 0
    scope_valid = (source.get('expected_workbook_sha256') == report.get('file_sha256') and
                   source.get('report_date') == report['report_identity']['report_date'])
    if not scope_valid:
        finding('source_workbook_binding_changed', 'Exact workbook hash or report date differs from supplied source binding. No source values were compared.')
        differences += 1
    aggregates = {}
    groups = defaultdict(Counter)
    failure_records = []
    occurrence_counts = Counter()
    source_record_count = 0
    typed_identifiers_checked = 0
    nonfailure_defects = 0
    for role, expected_list_key in LIST_KEYS.items():
        entry = lists[role]
        if not isinstance(entry, dict) or entry.get('list_key') != expected_list_key or not isinstance(entry.get('records'), list) or len(entry['records']) > MAX_RECORDS:
            raise WorkflowError('Source lists require exact names and bounded records.')
        if not isinstance(entry.get('original_sha256'), str) or not re.fullmatch('[a-f0-9]{64}', entry['original_sha256']):
            raise WorkflowError('Original source hash is required for each list.')
        if not isinstance(entry.get('source_reference'), str) or not entry['source_reference']:
            raise WorkflowError('Separate source references are required.')
        if entry.get('terminal_page_observed') is not True:
            finding('source_coverage_unverified', 'Terminal source page was not observed for ' + role + '.')
        seen = set()
        role_counts = {shift: Counter({field: 0 for field in COUNT_FIELDS}) for shift in ('1', '2', '3')}
        if not entry['records']:
            scope_valid = False
            finding('empty_source_evidence', 'Empty ' + role + ' evidence cannot establish verified zero activity.')
        source_record_count += len(entry['records'])
        for row in entry['records']:
            required = {'id', 'id_alias', 'source_date', 'status', 'audit_result', 'shift', 'delivery_note', 'part_number',
                        'order_quantity', 'actual_quantity', 'export', 'location', 'defects', 'resolution', 'comments', 'comments_presence'}
            if not isinstance(row, dict) or set(row) != required:
                raise WorkflowError('Source projection fields must be explicit and exact.')
            if type(row['id']) is not int or row['id'] <= 0 or type(row['id_alias']) is not int or row['id_alias'] != row['id'] or row['id'] in seen:
                raise WorkflowError('Source list + integer ID must be unique; ID aliases and types must agree.')
            seen.add(row['id'])
            if not _text(row['delivery_note']) or not _text(row['part_number']):
                raise WorkflowError('Source delivery notes and part numbers must retain their exact string types.')
            if row['comments_presence'] not in ('absent', 'null', 'present') or (row['comments_presence'] in ('absent', 'null') and row['comments'] is not None) or (row['comments_presence'] == 'present' and not isinstance(row['comments'], str)):
                raise WorkflowError('Source comments and field-presence evidence disagree.')
            typed_identifiers_checked += 2
            if row['status'] not in ('Complete', 'In Progress') or row['shift'] not in ('1', '2', '3'):
                scope_valid = False
                finding('source_status_or_shift_unmapped', 'Unknown source status/shift was not silently excluded.')
                continue
            try:
                date_value = datetime.fromisoformat(row['source_date'].replace('Z', '+00:00'))
                valid_date = date_value.tzinfo is not None and date_value.date().isoformat() == source.get('report_date')
            except (TypeError, ValueError, AttributeError):
                valid_date = False
            if not valid_date:
                scope_valid = False
                finding('source_date_scope_mismatch', 'Source Date field is absent, malformed or outside the selected report date.')
            if not isinstance(row['defects'], list) or not isinstance(row['resolution'], list):
                raise WorkflowError('Projected defects and resolution must be text collections.')
            if any(not _text(value) for value in row['defects'] + row['resolution']) or len(set(row['defects'])) != len(row['defects']):
                raise WorkflowError('Source defect categories/actions must be nonduplicated text collections.')
            if row['status'] == 'In Progress':
                role_counts[row['shift']]['in_progress'] += 1
                continue
            if row['audit_result'] not in ('Pass', 'Fail'):
                scope_valid = False
                finding('source_result_unmapped', 'Completed audit has an unknown result; it was not treated as Pass.')
                continue
            role_counts[row['shift']]['completed'] += 1
            role_counts[row['shift']][row['audit_result'].lower()] += 1
            if role != 'main':
                continue
            if row['location'] not in LOCATIONS or row['export'] not in ('No', 'Yes'):
                scope_valid = False
                finding('source_detail_group_unmapped', 'Completed Main audit has an unmapped location/export classification.')
                continue
            group = groups[(row['shift'], row['location'], row['export'])]
            group['completed'] += 1
            group[row['audit_result'].lower()] += 1
            if row['audit_result'] != 'Fail':
                if row['defects']:
                    nonfailure_defects += 1
                continue
            if not row['defects'] or any(category not in DEFECTS for category in row['defects']):
                scope_valid = False
                finding('source_failure_defects_unmapped', 'Failed Main audit has absent or unmapped categories.')
            for category in row['defects']:
                group[category] += 1
                occurrence_counts[category] += 1
            normalized = dict(row)
            for field in ('order_quantity', 'actual_quantity'):
                normalized[field] = quantity(row[field])
                if normalized[field] is None:
                    finding('source_quantity_missing', 'Failed Main order/actual quantity is unknown; it cannot verify a numeric total.')
            failure_records.append(normalized)
        aggregates[role] = role_counts
    if lists['main']['source_reference'] == lists['multi']['source_reference']:
        raise WorkflowError('Main and multi-pack cannot share a source artifact reference.')

    def compare(sheet, row, column, field, actual, expected):
        nonlocal numeric_compared, differences
        if quantity(actual) is not None and quantity(expected) is not None:
            numeric_compared += 1
        if canonical(actual) != canonical(expected):
            differences += 1
            finding('source_value_difference', 'Mapped report value differs from separate retained source evidence.', sheet, row, column)
            if len(comparisons) < MAX_FINDINGS:
                comparisons.append(dict(code='source_value_difference', sheet=sheet, row=row, field=field,
                                        before=actual, source_value=expected, action='review_only', source_write=False))

    matched_failures = 0
    if scope_valid:
        for role, columns in (('main', (2, 3, 4, 6)), ('multi', (2, 3, 4, 5))):
            for row in report[role + '_shift_counts']:
                for field, column in zip(COUNT_FIELDS, columns):
                    compare('Daily Summary', row['row'], column, role + '.' + field,
                            row[field], aggregates[role][row['shift']][field])
        for row in report['detail_groups']:
            expected = groups[(row['shift'], row['location'], row['export'])]
            for field, column in zip(('completed', 'pass', 'fail', *DEFECTS), range(4, 10)):
                compare('Shift Detail', row['row'], column, field, row[field], expected[field])
        for rownum, category in enumerate(DEFECTS, 37):
            compare('Daily Summary', rownum, 2, category, report['defect_occurrences'].get(category), occurrence_counts[category])
        # Source IDs do not exist in the workbook. Never pretend aggregate or
        # natural-key matches establish exact source-ID-to-workbook row identity.
        left = defaultdict(list)
        right = defaultdict(list)
        def key(row):
            return canonical([row['delivery_note'], row['part_number'], row['shift'], row['location']])
        def signature(row, source_side=False):
            return canonical({field: ('; '.join(row['resolution']) if source_side and field == 'resolution' else row[field])
                              for field in ('delivery_note', 'part_number', 'order_quantity', 'actual_quantity', 'defects', 'export', 'shift', 'location', 'resolution')})
        for row in report['records']:
            left[key(row)].append(row)
        for row in failure_records:
            right[key(row)].append(row)
        for record_key in set(left) | set(right):
            a, b = left[record_key], right[record_key]
            if len(a) == len(b) == 1:
                matched_failures += 1
                for field, column in zip(('delivery_note', 'part_number', 'order_quantity', 'actual_quantity', 'defects', 'export', 'shift', 'location', 'resolution'), range(1, 10)):
                    expected = '; '.join(b[0]['resolution']) if field == 'resolution' else b[0][field]
                    compare('Failure Summary', a[0]['row'], column, field, a[0][field], expected)
            else:
                if Counter(signature(row) for row in a) != Counter(signature(row, True) for row in b):
                    differences += 1
                    finding('source_failure_group_difference', 'Failed-audit detail multiset differs, or a failure row is missing/extra. No row identity was guessed.', 'Failure Summary')
                else:
                    matched_failures += len(a)
                if a and b:
                    finding('failure_row_mapping_ambiguous', 'Repeated failure identifiers permit multiset comparison only; source-ID row mapping is unresolved.', 'Failure Summary')
    if not numeric_compared:
        finding('no_source_quantity_checks', 'No known numeric source values were compared. No pass is possible.')
    if source.get('captured_at') is None:
        finding('source_capture_time_unverified', 'Independent source capture timestamps were not retained. Capture time is unknown and freshness cannot be established from report text or file modification times.')
    finding('historical_supplied_source', 'Original source hashes were bound at projection time; retained evidence is not a freshly authenticated source connection.')
    finding('source_completeness_unverified', 'Terminal pages do not prove consistent full-day coverage, late-scan policy or shift completion.')
    finding('source_timezone_unverified', 'Source Date calendar component is retained; business-timezone policy is not independently confirmed.')
    finding('source_list_identity_unverified', 'Named Main and multi-pack namespaces are separate; stable service site/list GUIDs are not established by these snapshots.')
    finding('summary_source_id_mapping_unavailable', 'Only failure rows are displayed. Main pass and child details are aggregate comparisons, not per-ID workbook reconciliation.')
    if nonfailure_defects:
        finding('nonfailure_defects_excluded', 'Source contains %d completed Pass audits with defect selections; occurrence totals here cover failed Main audits only.' % nonfailure_defects)
    source_totals = {role: {field: sum(values[field] for values in shifts.values()) for field in COUNT_FIELDS}
                     for role, shifts in aggregates.items()}
    result.update(source_sha256=claimed, source_captured_at=source['captured_at'],
                  source_capture_time_status=source.get('capture_time_status', 'recorded'), source_freshness_status='unverified',
                  source_reference=source.get('source_reference'), source_provenance_kind=source['provenance_kind'],
                  source_authenticity='not_authenticated', source_record_count=source_record_count,
                  source_totals=source_totals, source_defect_occurrences=dict(occurrence_counts),
                  source_nonfailure_defect_records=nonfailure_defects,
                  source_scope=dict(report_date=source['report_date'], main_list=LIST_KEYS['main'], multi_list=LIST_KEYS['multi']),
                  source_original_sha256={role: entry['original_sha256'] for role, entry in lists.items()},
                  numeric_values_compared=numeric_compared, typed_source_identifiers_checked=typed_identifiers_checked,
                  matched_observed_record_count=matched_failures, source_differences=comparisons,
                  source_difference_count=differences,
                  source_reconciliation_status='mismatch' if differences else ('observed_values_compared_with_limits' if numeric_compared else 'unverified'),
                  verification_scope='mapped_pratt_vs_separate_retained_sources', internal_check_status='findings',
                  review_binding_sha256=fingerprint(dict(workbook_sha256=report['file_sha256'], source_sha256=claimed,
                                                         template_id=TEMPLATE_ID, checker_version=report.get('checker_version'),
                                                         formula_sha256=report['formula_sha256'])))
    return _finish(result, counts)


UNION_TEMPLATE_ID = 'pratt-kn-template-union/2'
SHIFT_NAMES = {'1st Shift': '1', '2nd Shift': '2', '3rd Shift': '3'}
UNION_SHIFT_HEADERS = ('Date', 'Shift', 'Location', 'Part Number', 'Identifier / Delivery note',
                       'Inspected For', 'Quantity Inspected', 'Condition', 'Reject Reason', 'Reject Quantity',
                       'Quantity Certified', 'Comments', 'Defect Photo', 'Audit outcome', 'Audit defects',
                       'Order qty', 'Actual qty', 'Export', 'Recorded action', 'Record type')
PRODUCTION_LABELS = ('Status', 'Boxes inspected', 'Batches identified', 'Parts inspected', 'Parts accepted',
                     'Parts certified', 'Parts rejected', 'Parts reworked', 'Pending results', 'Defect rate',
                     'Inspection pass %', 'Billable labor hours', 'Parts per labor hour')
BILLING_LABELS = ('Team Member / Inspector', 'Hours on Shift', 'Working Supervisor', '2-Hour Supervisor Charge',
                  'Billable Shift Hours', 'Inspector Productivity', 'Inspected per inspector hour',
                  'Shift Productivity', 'Inspected per billable labor hour')
ADDITIONAL_LABELS = ('Vehicle', 'Vehicles inspected', 'Rear Lamps Inspected', 'Rear Lamps Accepted', 'Rear Lamps Rejected',
                     'Outside mirrors inspected', 'Mirrors rejected', 'LH Rear Lamps', 'RH Rear Lamps',
                     'LH mirror function', 'RH mirror function', 'LH BSD indicator', 'RH BSD indicator',
                     'Condition - LH', 'Condition - RH', 'Quantity OK - LH', 'Quantity OK - RH', 'Quantity NOK - LH',
                     'Quantity NOK - RH', 'Quantity Variance', 'Total Right Hand and Left Hand Rear Lamps Inspected', 'Lamps Per Labor Hour')
ADDITIONAL_DEFECT_LABELS = ('Short', 'Over', 'Wrong', 'Missing Shipping label', 'Missing Export docs', 'Missing Cert Docs',
                            'Missing Invoices', 'Sealed boxes', 'MP Short', 'MP Over', 'MP Wrong Part',
                            'MP Customer To Ship', 'Customer to Ship', 'Auditor Not Present', 'Not Certified')
ADDITIONAL_DEFECT_MAP = {35: DEFECTS[1], 36: DEFECTS[0], 46: DEFECTS[2]}


def inspect_pratt(sources, base):
    sheets = dict(sources)
    if cell(sheets.get('Daily Summary', []), 2, 1) != BRAND:
        return None
    union = any(name in sheets for name in ('Project and Production', 'Additional Template Fields', *SHIFT_NAMES, 'Results by Part'))
    if not union:
        return _inspect_summary(sources, base)
    # Core cells are checked by the original mapping; extension cells have their
    # own explicit checks below. They are never discarded from verification.
    core = [(name, [tuple(row[:9]) if name == 'Failure Summary' else row for row in sheets.get(name, [])])
            for name in ('Daily Summary', 'Shift Detail', 'Failure Summary')]
    result = _inspect_summary(core, base)
    result['template_id'] = UNION_TEMPLATE_ID
    result['completed_records'] = []
    result['part_counts'] = []
    result['additional_defect_counts'] = []
    result['failure_comments'] = []
    result['unreported_template_field_count'] = 0
    result['limitations'] = [
        'Explicit nine-sheet Pratt template-union layout only; no general template inference.',
        'Main audits and multi-pack children remain separate list grains; quantities are not audit counts.',
        'Production, certified/rejected, labor and other unavailable fields stay unknown, never zero.',
        'Formula text is checked, never executed; cached values, images and unstructured notes are not authenticated.',
        'Source service identity, completeness, business timezone and shift closeout remain unverified.',
        'Workbook does not display source list IDs; matching displayed rows cannot prove direct source-ID bindings.',
    ]
    counts, finding = _findings(result)
    allowed = defaultdict(set)

    def mapping(condition, message, name, row=None, column=None):
        if not condition:
            finding('template_mapping_mismatch', message, name, row, column)
            result['template_mapping_status'] = 'incomplete'

    def expect(name, row, column, value):
        allowed[name].add((row, column))
        mapping(cell(sheets.get(name, []), row, column) == value, 'Expected explicit template label or scope.', name, row, column)

    def unavailable(name, row, column):
        allowed[name].add((row, column))
        if cell(sheets.get(name, []), row, column) not in (None, ''):
            finding('unmapped_production_value', 'Field has no approved source quantity/production mapping; it must not be inferred from audit counts or order quantities.', name, row, column)
        else:
            result['unreported_template_field_count'] += 1

    def number(name, row, column):
        allowed[name].add((row, column))
        value = quantity(cell(sheets.get(name, []), row, column))
        if value is None:
            finding('quantity_missing', 'Required mapped audit count or source order/actual quantity is unknown.', name, row, column)
        else:
            result['numeric_cells_checked'] += 1
        return value

    expected_sheets = {'Daily Summary', 'Shift Detail', 'Failure Summary', 'Project and Production', 'Additional Template Fields', 'Results by Part', *SHIFT_NAMES}
    mapping(len(sources) == 9 and set(sheets) == expected_sheets, 'Expected precisely the documented nine-sheet template union.', 'Workbook')
    day = result['report_identity']['report_date']
    parsed_day = datetime.strptime(day, '%Y-%m-%d') if day else None
    date_label = (parsed_day.strftime('%B') + ' ' + str(parsed_day.day) + ', ' + str(parsed_day.year)) if parsed_day else ''
    name = 'Project and Production'
    expect(name, 2, 1, 'Project and production results')
    expect(name, 3, 1, BRAND + ' | ' + date_label)
    expect(name, 6, 1, 'Project details'); expect(name, 6, 2, 'Value')
    project_labels = ('Project Number', 'Report Date', 'Paying Client', 'Client Contact Name', 'Worksite Location', 'Area',
                      'Project Service Type', 'Project Description', 'Prepared By PPG', 'Overall Daily Notes')
    for rownum, label in enumerate(project_labels, 7):
        expect(name, rownum, 1, label)
        if rownum == 8:
            value = cell(sheets.get(name, []), rownum, 2)
            mapping(isinstance(value, datetime) and value.isoformat() == str(day) + 'T00:00:00', 'Project date must match report date.', name, rownum, 2)
            allowed[name].add((rownum, 2))
        elif rownum == 11:
            allowed[name].add((rownum, 2))
            if cell(sheets.get(name, []), rownum, 2) not in (None, ''):
                finding('project_metadata_not_source_bound', 'Worksite label is displayed but not independently established by the audit source fields.', name, rownum, 2)
        else:
            unavailable(name, rownum, 2)
    for header_row, title, start, labels in ((19, 'Production measure', 20, PRODUCTION_LABELS), (35, 'Billing and productivity', 36, BILLING_LABELS)):
        for col, value in enumerate((title, '1st shift', '2nd shift', '3rd shift', 'Daily total'), 1):
            expect(name, header_row, col, value)
        for rownum, label in enumerate(labels, start):
            expect(name, rownum, 1, label)
            for col in range(2, 6): unavailable(name, rownum, col)
    expect(name, 47, 1, 'Inspection notes')
    expect(name, 53, 1, 'Shift comments'); expect(name, 53, 2, 'Comments')
    for rownum, label in enumerate(('1st shift', '2nd shift', '3rd shift'), 54):
        expect(name, rownum, 1, label); unavailable(name, rownum, 2)
    expect(name, 59, 1, 'Audit totals and outcomes are reported separately in Daily Summary. Production quantities are a different measure.')
    name = 'Additional Template Fields'
    expect(name, 2, 1, 'Additional inspection fields'); expect(name, 3, 1, BRAND + ' | ' + date_label)
    union_formulas = dict(formula_map())
    for header_row, title, start, labels in ((6, 'Inspection measure', 7, ADDITIONAL_LABELS), (31, 'Pratt audit defect field', 32, ADDITIONAL_DEFECT_LABELS)):
        for col, value in enumerate((title, '1st shift', '2nd shift', '3rd shift', 'Daily total'), 1): expect(name, header_row, col, value)
        for rownum, label in enumerate(labels, start):
            expect(name, rownum, 1, label)
            if rownum in ADDITIONAL_DEFECT_MAP:
                for col, shift in enumerate(('1', '2', '3'), 2):
                    result['additional_defect_counts'].append(dict(row=rownum, column=col, shift=shift, category=ADDITIONAL_DEFECT_MAP[rownum], value=number(name, rownum, col)))
                address = 'E' + str(rownum)
                formula = '=SUM(B%d:D%d)' % (rownum, rownum)
                union_formulas[(name, address)] = formula
                allowed[name].add((rownum, 5))
                if cell(sheets.get(name, []), rownum, 5) != formula:
                    finding('summary_formula_mapping_mismatch', 'Additional defect total must sum only its three mapped shifts.', name, rownum, 5)
            else:
                for col in range(2, 6): unavailable(name, rownum, col)
    failure_rows = {row['row']: row for row in result['records']}
    for col, label in enumerate(('Reject quantity', 'Comments', 'Defect Photo', 'Original'), 10):
        mapping(cell(sheets.get('Failure Summary', []), 6, col) == label, 'Extended failure header must match exactly.', 'Failure Summary', 6, col)
    for rownum, row in failure_rows.items():
        for col in (10, 12, 13): unavailable('Failure Summary', rownum, col)
        comment = cell(sheets.get('Failure Summary', []), rownum, 11)
        if comment is not None and not isinstance(comment, str): finding('comment_type_invalid', 'Comments must preserve text or null.', 'Failure Summary', rownum, 11)
        result['failure_comments'].append(dict(row=rownum, delivery_note=row['delivery_note'], part_number=row['part_number'], shift=row['shift'], location=row['location'], comments=comment))
    for name, shift in SHIFT_NAMES.items():
        expect(name, 2, 1, name + ' inspection detail')
        expect(name, 3, 1, BRAND + ' | ' + date_label + ' | Completed records')
        for col, label in enumerate(UNION_SHIFT_HEADERS, 1): expect(name, 6, col, label)
        rows = sheets.get(name, [])
        rownum = 7
        while rownum <= len(rows) and cell(rows, rownum, 1) not in (None, ''):
            values = [cell(rows, rownum, col) for col in range(1, 21)]
            allowed[name].update((rownum, col) for col in range(1, 21))
            value = values[0]
            mapping(isinstance(value, datetime) and value.isoformat() == str(day) + 'T00:00:00' and type(values[1]) is int and values[1] == int(shift), 'Shift row date and numeric shift must match its sheet.', name, rownum)
            mapping(values[19] in ('Main audit', 'Multi-pack child'), 'Record type must explicitly preserve Main versus child grain.', name, rownum, 20)
            if not _text(values[3]) or not _text(values[4]): finding('typed_identifier_invalid', 'Part and delivery identifiers must retain exact text.', name, rownum)
            if values[13] not in ('Pass', 'Fail') or values[17] not in ('Yes', 'No') or values[2] not in LOCATIONS:
                finding('completed_record_scope_invalid', 'Completed record outcome, location or export value is unmapped.', name, rownum)
            for col in (6, 7, 8, 9, 10, 11, 13): unavailable(name, rownum, col)
            result['completed_records'].append(dict(sheet=name, row=rownum, date=day, shift=shift, location=values[2],
                part_number=values[3], delivery_note=values[4], audit_result=values[13],
                defects=values[14].split('; ') if _text(values[14]) else [],
                order_quantity=number(name, rownum, 16), actual_quantity=number(name, rownum, 17),
                export=values[17], resolution=values[18], comments=values[11],
                role='main' if values[19] == 'Main audit' else 'multi'))
            rownum += 1
        end = rownum - 1
        expect(name, end + 3, 1, 'Shift inspection results'); expect(name, end + 3, 2, 'Value')
        for row, label in enumerate(('Boxes Inspected', 'Total Parts Inspected', 'Total Certified', 'Total Rejected', 'Defect Rate', 'Inspection Pass %'), end + 4):
            expect(name, row, 1, label); unavailable(name, row, 2)
        expect(name, end + 12, 1, 'Billing'); expect(name, end + 12, 2, 'Value')
        for row, label in enumerate(('Team Member / Inspector', 'Hours on Shift', 'Working Supervisor', '2-Hour Supervisor Charge', 'Billable Shift Hours', 'Shift Productivity'), end + 13):
            expect(name, row, 1, label); unavailable(name, row, 2)
        result['sheets'].append(dict(name=name, role='completed_main_and_child_records', header_row=6,
            row_count=len(rows), data_row_count=end - 6, numeric_columns_checked=['order_quantity', 'actual_quantity'], mapping_status='explicit_separate_grains'))
    name = 'Results by Part'
    expect(name, 2, 1, 'Results by part number'); expect(name, 3, 1, 'Completed Main audits | ' + date_label)
    for col, label in enumerate(('Part number', 'Part name', 'Inspected for', 'Inspected', 'Certified', 'Rejected', 'Defect rate', 'Main audits'), 1): expect(name, 6, col, label)
    for rownum in range(7, len(sheets.get(name, [])) + 1):
        if not any(value not in (None, '') for value in sheets[name][rownum - 1]): continue
        part = cell(sheets[name], rownum, 1)
        allowed[name].add((rownum, 1))
        if not _text(part): finding('typed_identifier_invalid', 'Part identifiers must remain exact text.', name, rownum, 1)
        for col in range(2, 8): unavailable(name, rownum, col)
        result['part_counts'].append(dict(row=rownum, part_number=part, count=number(name, rownum, 8)))
    for name, rows in sources:
        if name in ('Daily Summary', 'Shift Detail'): continue
        for rownum, row in enumerate(rows, 1):
            for col, value in enumerate(row, 1):
                if value in (None, ''): continue
                if name == 'Failure Summary':
                    if col <= 9 or (rownum == 6 and 10 <= col <= 13) or (rownum in failure_rows and 10 <= col <= 13): continue
                elif (rownum, col) in allowed[name]: continue
                mapping(False, 'Nonempty extension cell lies outside the documented mapping.', name, rownum, col)
    all_formulas = []
    for name, rows in sources:
        for rownum, row in enumerate(rows, 1):
            for col, value in enumerate(row, 1):
                if isinstance(value, str) and value.startswith('='):
                    all_formulas.append(dict(sheet=name, row=rownum, column=col, formula=value))
                    if (name, coordinates(col, rownum)) not in union_formulas:
                        finding('unmapped_formula', 'Extension formula has no approved mapping and was not evaluated.', name, rownum, col)
    result.update(formulas=all_formulas, formula_count=len(all_formulas), formula_sha256=fingerprint(all_formulas),
                  row_count=sum(len(rows) for _, rows in sources), cell_count=sum(len(row) for _, rows in sources for row in rows),
                  completed_record_count=len(result['completed_records']), record_count=len(result['completed_records']),
                  failure_record_count=len(result['records']), verification_scope='mapped_pratt_template_union_internal_checks_only',
                  internal_check_status='findings' if result['template_mapping_status'] == 'matched' else 'unverified')
    for name in ('Project and Production', 'Additional Template Fields', 'Results by Part'):
        result['sheets'].append(dict(name=name, role='explicit_template_extension', row_count=len(sheets.get(name, [])),
            data_row_count=len(result['part_counts']) if name == 'Results by Part' else 0, numeric_columns_checked=[], mapping_status='explicit'))
    finding('unreported_production_fields', 'Unavailable production, inspection and labor fields remain blank/unknown. Audit counts and order quantities do not fill them.')
    finding('additional_defect_alias_review', 'Additional Shipping label / Export docs labels map to the explicit failed-Main Back Label / Export Documents categories; broader semantic equivalence is not established.')
    return _finish(result, counts)


def reconcile_pratt(report, source):
    if report.get('template_id') != UNION_TEMPLATE_ID:
        return _reconcile_summary(report, source)
    source = parse(source)
    core = deepcopy(report)
    core['template_id'] = TEMPLATE_ID
    result = _reconcile_summary(core, source)
    result['template_id'] = UNION_TEMPLATE_ID
    counts, finding = _findings(result)
    differences = result['source_difference_count']
    numeric_compared = result['numeric_values_compared']
    matched = 0
    ambiguous_groups = 0
    if numeric_compared:
        source_rows = []
        part_counts = Counter()
        defect_counts = defaultdict(Counter)
        for role, entry in source['lists'].items():
            for row in entry['records']:
                if row['status'] != 'Complete': continue
                normalized = dict(row, role=role, date=source['report_date'], resolution='; '.join(row['resolution']) or None,
                                  order_quantity=quantity(row['order_quantity']), actual_quantity=quantity(row['actual_quantity']))
                if normalized['order_quantity'] is None or normalized['actual_quantity'] is None:
                    finding('source_quantity_missing', 'A completed source order/actual quantity is unknown; it was not filled with zero.')
                source_rows.append(normalized)
                if role == 'main':
                    part_counts[row['part_number']] += 1
                    if row['audit_result'] == 'Fail': defect_counts[row['shift']].update(row['defects'])
        fields = ('role', 'date', 'shift', 'location', 'part_number', 'delivery_note', 'audit_result', 'defects', 'order_quantity', 'actual_quantity', 'export', 'resolution', 'comments')
        def key(row): return canonical([row['role'], row['shift'], row['delivery_note'], row['part_number'], row['location']])
        def signature(row): return canonical({field: row[field] for field in fields})
        left = defaultdict(list); right = defaultdict(list)
        for row in report['completed_records']: left[key(row)].append(row)
        for row in source_rows: right[key(row)].append(row)
        def difference(code, message, sheet, row=None, field=None, before=None, expected=None):
            nonlocal differences
            differences += 1
            finding(code, message, sheet, row)
            if len(result['source_differences']) < MAX_FINDINGS:
                result['source_differences'].append(dict(code=code, sheet=sheet, row=row, field=field,
                    before=before, source_value=expected, action='review_only', source_write=False))
        for record_key in set(left) | set(right):
            a, b = left[record_key], right[record_key]
            if Counter(signature(row) for row in a) != Counter(signature(row) for row in b):
                difference('completed_record_group_difference', 'Completed Main/child row multiset differs from its separately supplied list; no source ID was guessed.', a[0]['sheet'] if a else 'Workbook', a[0]['row'] if a else None)
            else:
                matched += len(a)
            if len(a) > 1 or len(b) > 1: ambiguous_groups += 1
            numeric_compared += sum(min(sum(quantity(row[field]) is not None for row in a), sum(quantity(row[field]) is not None for row in b)) for field in ('order_quantity', 'actual_quantity'))
        observed_parts = Counter()
        for row in report['part_counts']:
            if row['part_number'] in observed_parts:
                difference('duplicate_part_summary', 'Repeated exact part key is not a separate source group.', 'Results by Part', row['row'])
            observed_parts[row['part_number']] += row['count'] or 0
            if row['count'] is not None: numeric_compared += 1
            if row['count'] != part_counts[row['part_number']]:
                difference('part_audit_count_difference', 'Main audit count per exact typed part differs from source.', 'Results by Part', row['row'], 'main_audits', row['count'], part_counts[row['part_number']])
        if set(observed_parts) != set(part_counts):
            difference('part_summary_coverage_difference', 'Part summary does not cover precisely all completed Main source parts.', 'Results by Part')
        for row in report['additional_defect_counts']:
            expected = defect_counts[row['shift']][row['category']]
            if row['value'] is not None: numeric_compared += 1
            if row['value'] != expected:
                difference('additional_defect_difference', 'Additional failed-Main category count differs from source.', 'Additional Template Fields', row['row'], row['category'], row['value'], expected)
        for row in report['failure_comments']:
            matches = [r for r in source_rows if r['role'] == 'main' and r['audit_result'] == 'Fail' and all(r[field] == row[field] for field in ('shift', 'delivery_note', 'part_number', 'location'))]
            if len(matches) == 1 and row['comments'] != matches[0]['comments']:
                difference('failure_comment_difference', 'Failure comments differ from retained source.', 'Failure Summary', row['row'], 'comments', row['comments'], matches[0]['comments'])
        if ambiguous_groups:
            finding('completed_row_mapping_ambiguous', '%d repeated natural-key groups were compared as whole multisets; individual source-ID mapping is unproven.' % ambiguous_groups)
    result.update(source_difference_count=differences, numeric_values_compared=numeric_compared,
                  matched_completed_record_count=matched, ambiguous_completed_record_groups=ambiguous_groups,
                  source_id_mapping_status='source_list_ids_checked_for_uniqueness_but_not_displayed_in_workbook',
                  source_reconciliation_status='mismatch' if differences else ('observed_values_compared_with_limits' if numeric_compared else 'unverified'),
                  verification_scope='mapped_pratt_template_union_vs_separate_retained_sources',
                  review_binding_sha256=fingerprint(dict(workbook_sha256=report['file_sha256'], source_sha256=source['source_sha256'],
                      template_id=UNION_TEMPLATE_ID, checker_version=report.get('checker_version'), formula_sha256=report['formula_sha256'])))
    # The extension displays completed rows, so replace the core aggregate-only caveat.
    counts.pop('summary_source_id_mapping_unavailable', None)
    result['findings'] = [item for item in result['findings'] if item['code'] != 'summary_source_id_mapping_unavailable']
    finding('source_id_not_displayed', 'All mapped completed rows are compared as typed natural-key multisets, not direct workbook source-ID bindings.')
    return _finish(result, counts)

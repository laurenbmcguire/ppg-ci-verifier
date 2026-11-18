"""Source-mapping readiness: an inventory of what a list's schema shows, and a validator for one assignment.

Pure and read-only. It takes a schema snapshot somebody already holds (from a reviewed read-only capture, or a
synthetic fixture) and an assignment, and reports what is missing before that list could be run. It reads no
source, changes no setting, approves nothing and marks nothing verified.

Two kinds of fact are kept strictly apart:

  DISCOVERED  what the schema itself shows: column names, IDs, types, choice values, the list's identity.
              This can be checked mechanically, and drift from what the assignment pinned is detected.
  ATTESTED    what the data MEANS: that a field is the actual scan time and not arrival metadata, which shift
              values exist and what they mean, that the date field is the business date, the timezone, what the
              quantities count, that this list is this client's job, that the header is right. No schema can
              show this. It needs a named person's attestation, bound to the exact contract it is about, so the
              attestation stops counting the moment that contract changes.

A discovered fact never stands in for an attestation. A date-time column is not thereby a scan time; the flags
``verified`` and ``timestamp_semantics_verified`` on an assignment are not attestations either.
"""
import re
from zoneinfo import ZoneInfo

from hosting import source_first as sf
from hosting.last_scan_drafts import FORBIDDEN_SCAN_FIELDS, instant

GUID = r'[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}'
FACETS = ('dateTime', 'number', 'currency', 'choice', 'lookup', 'boolean', 'calculated', 'thumbnail', 'personOrGroup', 'text')
SEMANTICS = {
    'identity_and_registry': 'This exact site and list is this client\'s job.',
    'scan_time_meaning': 'The scan field records when the part was actually scanned, not when the row was created, saved or synced.',
    'shift_values': 'The recorded shift values are complete and mean these shifts.',
    'business_date': 'The date field is the business date the report is for.',
    'timezone': 'The site works in this timezone.',
    'quantities': 'Inspected, rejected and certified count what the report says they count.',
    'template_and_header': 'This mapping, header and template are right for this client\'s report.',
}
QUANTITY_TYPES, SHIFT_TYPES = ('number', 'text'), ('choice', 'text', 'number')


def column_type(column):
    for facet in FACETS:
        if isinstance(column.get(facet), dict):
            return facet
    return 'unknown'


def inventory(schema):
    """What the schema shows, column by column. Discovered metadata only: no meaning is assigned to anything."""
    columns = []
    names = [str(c.get('displayName') or '').strip().lower() for c in (schema.get('columns') or {}).values()]
    for name, c in sorted((schema.get('columns') or {}).items()):
        kind = column_type(c)
        columns.append(dict(name=name, id=c.get('id'), display_name=c.get('displayName'), type=kind,
                            format=(c.get('dateTime') or {}).get('format') if kind == 'dateTime' else None,
                            choices=list((c.get('choice') or {}).get('choices') or []) if kind == 'choice' else None,
                            read_only=bool(c.get('readOnly')),
                            display_name_shared=names.count(str(c.get('displayName') or '').strip().lower()) > 1,
                            basis='discovered'))
    return dict(site_id=schema.get('site_id'), list_id=schema.get('list_id'), list=schema.get('list'),
                captured_at=schema.get('captured_at'), columns=columns,
                note='Discovered metadata only. Nothing here says what a column means or that a mapping is right.')


def schema_digest(schema):
    return sf.digest(dict(site_id=schema.get('site_id'), list_id=schema.get('list_id'),
                          columns={n: dict(id=c.get('id'), type=column_type(c), format=(c.get('dateTime') or {}).get('format'),
                                           choices=(c.get('choice') or {}).get('choices'), displayName=c.get('displayName'))
                                   for n, c in (schema.get('columns') or {}).items()}))


def _contract(assignment, key):
    return (assignment.get('field_contract') or {}).get(key)


def attestation_bindings(assignment):
    """For each business meaning, the hash of exactly what an attestation of it would be about. A person records
    this hash with their attestation; if the contract later changes, the hash no longer matches. This function
    creates no attestation."""
    from hosting import source_first_workbook as wb
    a, where = assignment, dict(site_id=assignment.get('site_id'), list_id=assignment.get('list_id'))
    parts = {
        'identity_and_registry': dict(where, client_id=a.get('client_id'), job=a.get('job')),
        'scan_time_meaning': dict(where, scan_field=a.get('scan_field'), contract=_contract(a, 'scan')),
        'shift_values': dict(where, shift_field=a.get('shift_field'), contract=_contract(a, 'shift'),
                             recorded_shifts=a.get('recorded_shifts'), shifts=(a.get('mapping') or {}).get('shifts')),
        'business_date': dict(where, contract=_contract(a, 'date')),
        'timezone': dict(where, zone=a.get('zone')),
        'quantities': dict(where, contracts={q: _contract(a, q) for q in sf.QUANTITIES}),
        'template_and_header': dict(where, mapping=a.get('mapping'), header=a.get('header'), template=wb.contract()['clean_sha256']),
    }
    return {k: sf.digest(v) for k, v in parts.items()}


def validate(assignment, schema, registry_entries, sample_rows=None):
    """One assignment against one schema snapshot. Returns a report; never raises for bad input, never mutates.

    ``ready_for_dry_run`` is True only when every check passes. It is a statement about this report, not an
    approval: activation still needs everything in docs/LAST_SCAN_ACTIVATION_CHECKLIST.md."""
    from hosting import last_scan_runner as runner
    a = assignment if isinstance(assignment, dict) else {}
    checks = []
    def check(code, basis, ok, detail='', evidence_needed=''):
        checks.append(dict(check=code, basis=basis, ok=bool(ok), detail='' if ok else detail, evidence_needed='' if ok else evidence_needed))
        return bool(ok)
    text = lambda v: isinstance(v, str) and bool(v.strip()) and v == v.strip()
    columns = (schema or {}).get('columns') if isinstance(schema, dict) else None
    mapping = a.get('mapping') if isinstance(a.get('mapping'), dict) else {}
    fields = mapping.get('fields') if isinstance(mapping.get('fields'), dict) else {}

    # ---- identity (discovered + exact configuration)
    site = str(a.get('site_id') or '').split(',')
    check('assignment_identity_complete', 'configured', all(text(a.get(k)) for k in ('client_id', 'job', 'site_id', 'list_id')),
          'Client, job, site id and list id must each be exact text.')
    check('site_and_list_ids_are_exact_identifiers', 'configured',
          len(site) == 3 and site[0].endswith('.sharepoint.com') and all(re.fullmatch(GUID, p) for p in site[1:]) and bool(re.fullmatch(GUID, str(a.get('list_id') or ''))),
          'The site id must be host,site-guid,web-guid and the list id a GUID. Names and URLs are not identifiers.')
    have_schema = check('schema_snapshot_supplied', 'discovered', isinstance(columns, dict) and bool(columns),
                        'No schema snapshot for this list was supplied, so nothing about its columns can be checked.',
                        'A read-only capture of the list\'s columns, taken by an approved reader.')
    columns = columns if isinstance(columns, dict) else {}
    if have_schema:
        check('schema_is_for_this_site_and_list', 'discovered', schema.get('site_id') == a.get('site_id') and schema.get('list_id') == a.get('list_id')
              and ((schema.get('list') or {}).get('id') in (None, a.get('list_id'))),
              'The schema snapshot belongs to another site or list.')
    problem = None
    try:
        problem = 'job_registry_unavailable' if registry_entries is None else runner._registry_problem(a, registry_entries)
    except Exception as error:
        problem = 'registry_check_failed:' + type(error).__name__
    check('exact_verified_registry_entry', 'configured', problem is None, str(problem),
          'A verified job registry entry with exactly this client key, job, site id and list id.')

    # ---- timezone
    zone_ok = False
    try:
        zone_ok = text(a.get('zone')) and bool(ZoneInfo(a['zone']))
    except Exception:
        zone_ok = False
    check('timezone_is_a_real_zone', 'configured', zone_ok, 'The timezone must be a real zone name such as America/Chicago.')

    # ---- every mapped report field: exists once, pinned by id and type, no drift
    def pinned(key, name, allowed=None, label=None):
        label = label or key
        column = columns.get(name) if text(name) else None
        if not check(label + '_column_exists', 'discovered', column is not None, f'No column named {name!r} exists in the schema snapshot.'):
            return None
        shared = [n for n, c in columns.items() if n != name and str(c.get('displayName') or '').strip().lower() in
                  (str(column.get('displayName') or '').strip().lower(), str(name).lower())]
        check(label + '_column_unambiguous', 'discovered', not shared,
              f'Column {name!r} shares its name or display name with {sorted(shared)}; the mapping could mean either.',
              'A person confirming, by column ID, which column is meant.')
        contract, kind = _contract(a, key), column_type(column)
        check(label + '_pinned_by_column_id_and_type', 'configured',
              isinstance(contract, dict) and contract.get('column') == name and text(contract.get('column_id')) and text(contract.get('type')),
              f'The assignment does not pin {label} to a column ID and type.', 'field_contract entry: column, column_id, type.')
        if isinstance(contract, dict):
            check(label + '_column_id_unchanged', 'discovered', contract.get('column_id') == column.get('id'),
                  f'{name!r} now has column ID {column.get("id")!r}, not the pinned {contract.get("column_id")!r}: the column was replaced.')
            check(label + '_type_unchanged', 'discovered', contract.get('type') == kind and (contract.get('format') in (None, (column.get('dateTime') or {}).get('format'))),
                  f'{name!r} is now type {kind!r}, not the pinned {contract.get("type")!r}.')
        check(label + '_type_known', 'discovered', kind != 'unknown', f'The schema does not say what type {name!r} is.')
        if allowed:
            check(label + '_type_supported', 'discovered', kind in allowed, f'{label} is a {kind} column; supported: {", ".join(allowed)}.')
        return column

    check('mapping_covers_every_report_field', 'configured', all(text(fields.get(f)) for f in sf.FIELDS),
          'The mapping must name a column for every report field: ' + ', '.join(f for f in sf.FIELDS if not text(fields.get(f))))
    used = [fields.get(f) for f in sf.FIELDS if text(fields.get(f))]
    check('no_column_mapped_to_two_report_fields', 'configured', len(used) == len(set(used)),
          'One column is mapped to more than one report field.')
    for field in sf.FIELDS:
        if text(fields.get(field)):
            allowed = QUANTITY_TYPES if field in sf.QUANTITIES else None
            column = pinned(field, fields[field], allowed)
            if field == 'date' and column is not None:
                check('date_is_a_date_only_column', 'discovered', column_type(column) == 'dateTime' and (column.get('dateTime') or {}).get('format') == 'dateOnly',
                      'The business-date column must be a date-only column.')

    # ---- scan field: never date/arrival metadata; a real date-time; meaning cannot be discovered
    scan = a.get('scan_field')
    acceptable = text(scan) and scan.lower() not in FORBIDDEN_SCAN_FIELDS and scan != fields.get('date')
    check('scan_field_is_not_date_or_arrival_metadata', 'configured', acceptable,
          'The scan field is missing, or is the date column or creation/modification metadata. Those are never the actual scan time.')
    if acceptable:
        column = pinned('scan', scan, ('dateTime',), 'scan_field')
        if column is not None:
            check('scan_field_is_a_date_and_time', 'discovered', (column.get('dateTime') or {}).get('format') == 'dateTime',
                  'The scan field must hold a date AND time; a date-only column cannot time a quiet hour.')
            check('scan_field_is_entered_not_computed', 'discovered', column_type(column) != 'calculated' and not column.get('readOnly'),
                  'The scan field is computed or read-only, which is typical of system metadata.')

    # ---- shift field and values
    shift, recorded = a.get('shift_field'), a.get('recorded_shifts')
    recorded_ok = isinstance(recorded, list) and bool(recorded) and all(text(v) for v in recorded) and len(recorded) == len(set(recorded))
    check('recorded_shifts_listed', 'configured', recorded_ok, 'recorded_shifts must list each exact shift value once.')
    check('shift_field_is_the_mapped_shift_column', 'configured', text(shift) and shift == fields.get('shift'),
          'The shift field used to split shifts must be the same column the report maps as shift.')
    shift_column = columns.get(shift) if text(shift) else None
    if shift_column is not None and isinstance(_contract(a, 'shift'), dict):
        check('shift_field_type_supported', 'discovered', column_type(shift_column) in SHIFT_TYPES, 'The shift column must be a choice, text or number column.')
    if recorded_ok:
        shifts = mapping.get('shifts') if isinstance(mapping.get('shifts'), dict) else {}
        unmapped = [v for v in recorded if shifts.get(v) not in sf.SHIFT_NAMES]
        check('every_recorded_shift_maps_to_a_template_shift', 'configured', not unmapped,
              f'No template shift is mapped for recorded value(s) {unmapped}.')
        if shift_column is not None and column_type(shift_column) == 'choice':
            choices = list((shift_column.get('choice') or {}).get('choices') or [])
            check('recorded_shifts_exist_as_choices', 'discovered', all(v in choices for v in recorded),
                  f'Recorded shift value(s) {[v for v in recorded if v not in choices]} are not choices of the shift column {choices}.')
            check('every_shift_choice_is_covered', 'discovered', all(c in recorded for c in choices),
                  f'Shift choice(s) {[c for c in choices if c not in recorded]} are in no assignment; rows with them would be in no draft.',
                  'Either add the value to recorded_shifts (with its meaning attested) or confirm it is never used.')

    # ---- template / header
    try:
        from hosting import source_first_workbook as wb
        extra = set((a.get('header') or {})) - set(wb.HEADER_FIELDS) - {'photo_choices', 'evidence'} if isinstance(a.get('header'), dict) else {'(no header)'}
        check('header_uses_only_template_fields', 'configured', not extra, f'Unknown header field(s): {sorted(extra)}.')
        check('pinned_template_is_intact', 'discovered', bool(wb.contract()['clean_sha256']), 'The pinned report template failed its hash check.')
    except Exception as error:
        check('pinned_template_is_intact', 'discovered', False, 'The pinned report template could not be checked: ' + type(error).__name__)

    # ---- sample rows: observations only, and red flags; never evidence FOR a meaning
    observations = []
    if sample_rows:
        scan_values = [r.get(scan) for r in sample_rows] if text(scan) else []
        bad = 0
        for v in scan_values:
            try:
                instant(v)
            except Exception:
                bad += 1
        check('sample_scan_values_are_offset_aware_times', 'discovered', not bad, f'{bad} of {len(scan_values)} sample scan values are missing or not offset-aware date-times.')
        if text(shift) and recorded_ok:
            seen = sorted({str(r.get(shift)) for r in sample_rows if r.get(shift) is not None} - set(recorded))
            check('sample_shift_values_are_all_recorded_shifts', 'discovered', not seen, f'Sample rows carry shift value(s) {seen} that no assignment covers.')
            check('sample_rows_all_have_a_shift', 'discovered', all(r.get(shift) is not None for r in sample_rows), 'Some sample rows have no shift value.')
        for arrival in ('Created', 'Modified', 'createdDateTime', 'lastModifiedDateTime'):
            pairs = [(r.get(scan), r.get(arrival)) for r in sample_rows if r.get(scan) is not None and r.get(arrival) is not None]
            if pairs and all(x == y for x, y in pairs):
                check('scan_values_differ_from_' + arrival.lower(), 'discovered', False,
                      f'In every sample row the scan value equals {arrival}. It looks like arrival metadata under another name.',
                      'Evidence of how the scan field is filled in on the tablet.')
        for q in sf.QUANTITIES:
            if text(fields.get(q)):
                values = [r.get(fields[q]) for r in sample_rows]
                observations.append(dict(field=q, absent_or_blank=sum(1 for v in values if v in (None, '')), structured=sum(1 for v in values if isinstance(v, (dict, list))), rows=len(values)))
        observations.append(dict(note='Sample observations describe these rows only. They are never evidence that a field means what the assignment says.'))

    # ---- attested business meaning: bound to the exact contract, never inferred, never created here
    bindings = attestation_bindings(a) if mapping else {}
    given = a.get('attestations') if isinstance(a.get('attestations'), dict) else {}
    for semantic, statement in SEMANTICS.items():
        record = given.get(semantic)
        complete = isinstance(record, dict) and all(text(record.get(k)) for k in ('attested_by', 'attested_at', 'evidence'))
        check(semantic + '_attested', 'attested', complete and record.get('bound_to') == bindings.get(semantic),
              ('An attestation exists but is for a different contract; what it was about has changed since.' if complete else 'No attestation by a named person.'),
              f'{statement} Attest with: attested_by, attested_at, evidence, bound_to={bindings.get(semantic)}')
    for flag in ('verified', 'timestamp_semantics_verified'):
        if a.get(flag) is True and not all(c['ok'] for c in checks if c['basis'] == 'attested'):
            check(flag + '_flag_is_backed_by_attestations', 'attested', False,
                  f'The assignment says {flag}=true, but the attestations that would justify it are missing or stale. A flag is not evidence.')

    blocked = [c['check'] for c in checks if not c['ok']]
    return dict(client_id=a.get('client_id'), job=a.get('job'), site_id=a.get('site_id'), list_id=a.get('list_id'),
                ready_for_dry_run=not blocked, status='blocked' if blocked else 'ready_for_dry_run_only',
                blocked_reasons=blocked, missing_evidence=[dict(check=c['check'], needed=c['evidence_needed']) for c in checks if not c['ok'] and c['evidence_needed']],
                checks=checks, attestation_bindings=bindings, schema_digest=schema_digest(schema) if have_schema else None,
                sample_observations=observations, marked_verified_by_this_tool=False,
                note='This report approves nothing. Even with every check passing the list is not production-ready: see the activation checklist.')


def report_text(report):
    """The same report as plain lines a reviewer can read."""
    lines = [f"Source mapping readiness: {report.get('client_id')} / {report.get('job')}",
             f"Result: {'BLOCKED' if report['blocked_reasons'] else 'READY FOR A DRY RUN ONLY'}  ({len(report['blocked_reasons'])} open of {len(report['checks'])} checks)",
             'Nothing was read, changed, approved or marked verified to produce this.', '']
    for basis, title in (('configured', 'Configured in the assignment'), ('discovered', 'Discovered in the schema or sample (mechanical)'),
                         ('attested', 'Business meaning (needs a named person; cannot be discovered)')):
        lines.append(title + ':')
        for c in report['checks']:
            if c['basis'] == basis:
                lines.append(('  ok       ' if c['ok'] else '  BLOCKED  ') + c['check'] + ('' if c['ok'] else ' — ' + c['detail']))
                if c['evidence_needed']:
                    lines.append('             needed: ' + c['evidence_needed'])
        lines.append('')
    lines.append(report['note'])
    return '\n'.join(lines)

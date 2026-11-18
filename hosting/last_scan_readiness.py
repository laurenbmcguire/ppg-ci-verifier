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

Input is validated shape by shape and anything malformed becomes a named blocked reason. There is no catch-all:
only the specific errors a bad VALUE can cause (an unknown zone, an unparseable time) are handled, so a coding
error in this module or in the registry rule still surfaces as an exception.
"""
import math
import re
from zoneinfo import ZoneInfo

from hosting import source_first as sf
from hosting.last_scan_drafts import FORBIDDEN_SCAN_FIELDS, instant

GUID = r'[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}'
FACETS = ('dateTime', 'number', 'currency', 'choice', 'lookup', 'boolean', 'calculated', 'thumbnail', 'personOrGroup', 'text')
# Column properties that are not type facets. Anything else on a column is treated as an unrecognised facet and
# blocks: this validator must not guess that an unknown property leaves the column's type alone.
PROPERTIES = ('name', 'displayName', 'id', 'description', 'columnGroup', 'enforceUniqueValues', 'hidden', 'indexed', 'readOnly',
              'required', 'isDeletable', 'isReorderable', 'isSealed', 'propagateChanges', 'type', 'sourceColumn', 'validation',
              'defaultValue', 'sourceContentType')
FLAGS = ('readOnly', 'hidden', 'required', 'indexed', 'enforceUniqueValues')
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


def _text(v):
    return isinstance(v, str) and bool(v.strip()) and v == v.strip()


def _dict(v):
    return v if isinstance(v, dict) else {}


def _plain(v):
    """The value as plain JSON data. Anything that is not (a set, an object, a non-text key, NaN) becomes a marker
    naming its type, so hashing never fails and never treats two different odd values as the same real value."""
    if v is None or isinstance(v, (str, bool, int)):
        return v
    if isinstance(v, float):
        return v if math.isfinite(v) else {'__not_plain_data__': 'float:' + repr(v)}
    if isinstance(v, (list, tuple)):
        return [_plain(x) for x in v]
    if isinstance(v, dict):
        return {(k if isinstance(k, str) else '__key__:' + type(k).__name__ + ':' + repr(k)): _plain(x) for k, x in v.items()}
    return {'__not_plain_data__': type(v).__name__}


def _is_plain(v):
    if v is None or isinstance(v, (str, bool, int)):
        return True
    if isinstance(v, float):
        return math.isfinite(v)
    if isinstance(v, list):
        return all(_is_plain(x) for x in v)
    if isinstance(v, dict):
        return all(isinstance(k, str) and _is_plain(x) for k, x in v.items())
    return False


def _digest(v):
    return sf.digest(_plain(v))


def column_type(column):
    """The column's type, from its facets alone. Exactly one well-formed facet names a type; anything else is
    'unknown' (no facet), 'contradictory' (more than one, whatever their values) or 'malformed'."""
    if not isinstance(column, dict):
        return 'malformed'
    present = [f for f in FACETS if f in column]
    if not present:
        return 'unknown'
    if len(present) > 1:
        return 'contradictory'
    facet, value = present[0], column[present[0]]
    if not isinstance(value, dict):
        return 'malformed'
    if facet == 'dateTime' and value.get('format') not in ('dateOnly', 'dateTime'):
        return 'malformed'
    if facet == 'choice' and not (isinstance(value.get('choices'), list) and all(_text(c) for c in value['choices'])):
        return 'malformed'
    return facet


def column_problems(column):
    """Readable reasons this column's definition cannot be relied on. Empty when it is well formed."""
    if not isinstance(column, dict):
        return ['the column definition is not an object']
    out, present = [], [f for f in FACETS if f in column]
    kind = column_type(column)
    if kind == 'contradictory':
        out.append('it carries more than one type facet: ' + ', '.join(present))
    if kind == 'malformed':
        out.append(f'its {present[0]} facet is empty or malformed')
    unknown = sorted(str(k) for k in column if k not in FACETS and k not in PROPERTIES and not (isinstance(k, str) and k.startswith('@odata.')))
    if unknown:
        out.append('unrecognised column properties: ' + ', '.join(unknown))
    for flag in FLAGS:
        if flag in column and not isinstance(column[flag], bool):
            out.append(f'{flag} is not true or false')
    for key in ('id', 'displayName'):
        if key in column and not _text(column[key]):
            out.append(f'{key} is not text')
    if not _is_plain(column):
        out.append('it contains values that are not plain data')
    return out


def column_evidence(column):
    """Hash of the WHOLE column definition as captured: id, names, every facet with its contents, readOnly, hidden,
    required, default value, validation and anything else present. A field contract pins this, so any change to
    what the reviewer and the attester looked at is detected."""
    return _digest(column)


def _columns(schema):
    """(well-formed columns by name, names of malformed entries). Never raises."""
    raw = schema.get('columns') if isinstance(schema, dict) else None
    if not isinstance(raw, dict):
        return {}, []
    good = {n: c for n, c in raw.items() if isinstance(n, str) and isinstance(c, dict)}
    return good, sorted(repr(n) for n in raw if n not in good)


def inventory(schema):
    """What the schema shows, column by column. Discovered metadata only: no meaning is assigned to anything."""
    s = _dict(schema)
    raw = s.get('columns') if isinstance(s.get('columns'), dict) else {}
    names = [str(c.get('displayName') or '').strip().lower() for c in raw.values() if isinstance(c, dict)]
    columns = []
    for name, c in sorted(raw.items(), key=lambda item: str(item[0])):
        kind, d = column_type(c), _dict(c)
        columns.append(dict(name=name, id=d.get('id'), display_name=d.get('displayName'), type=kind,
                            facets=[f for f in FACETS if f in d],
                            format=d['dateTime'].get('format') if kind == 'dateTime' else None,
                            choices=list(d['choice']['choices']) if kind == 'choice' else None,
                            read_only=d.get('readOnly'), has_default_value='defaultValue' in d,
                            display_name_shared=names.count(str(d.get('displayName') or '').strip().lower()) > 1,
                            problems=column_problems(c), basis='discovered'))
    return dict(site_id=s.get('site_id'), list_id=s.get('list_id'), list=s.get('list'),
                captured_at=s.get('captured_at'), columns=columns,
                note='Discovered metadata only. Nothing here says what a column means or that a mapping is right.')


def schema_digest(schema):
    """Hash of the schema's identity and every column's full definition."""
    s = _dict(schema)
    raw = s.get('columns') if isinstance(s.get('columns'), dict) else None
    return _digest(dict(shape=type(schema).__name__, site_id=s.get('site_id'), list_id=s.get('list_id'), list=s.get('list'),
                        columns=None if raw is None else {k: column_evidence(c) for k, c in _plain(raw).items()}))


def _contract(assignment, key):
    entry = _dict(_dict(assignment).get('field_contract')).get(key)
    return entry if isinstance(entry, dict) else None


def attestation_bindings(assignment):
    """For each business meaning, the hash of exactly what an attestation of it would be about, including the pinned
    hash of each column definition involved. A person records this hash with their attestation; if the contract or
    the column evidence it pins later changes, the hash no longer matches. This function creates no attestation."""
    from hosting import source_first_workbook as wb
    a = _dict(assignment)
    where = dict(site_id=a.get('site_id'), list_id=a.get('list_id'))
    parts = {
        'identity_and_registry': dict(where, client_id=a.get('client_id'), job=a.get('job')),
        'scan_time_meaning': dict(where, scan_field=a.get('scan_field'), contract=_contract(a, 'scan')),
        'shift_values': dict(where, shift_field=a.get('shift_field'), contract=_contract(a, 'shift'),
                             recorded_shifts=a.get('recorded_shifts'), shifts=_dict(a.get('mapping')).get('shifts')),
        'business_date': dict(where, contract=_contract(a, 'date')),
        'timezone': dict(where, zone=a.get('zone')),
        'quantities': dict(where, contracts={q: _contract(a, q) for q in sf.QUANTITIES}),
        'template_and_header': dict(where, mapping=a.get('mapping'), header=a.get('header'), contracts=a.get('field_contract'),
                                    template=wb.contract()['clean_sha256']),
    }
    return {k: _digest(v) for k, v in parts.items()}


def _registry_shape_ok(entries):
    scalar = lambda v: v is None or isinstance(v, (str, int))
    return isinstance(entries, (list, tuple)) and all(
        isinstance(e, dict) and all(scalar(e.get(k)) for k in ('client_key', 'job', 'source', 'site_id', 'list_id')) for e in entries)


def validate(assignment, schema, registry_entries, sample_rows=None):
    """One assignment against one schema snapshot. Returns a report and never mutates its input.

    Malformed input of any nesting becomes a blocked reason, not an exception. Errors that are not about the input
    (a bug here or in the registry rule, a broken pinned template) are deliberately NOT caught.

    ``ready_for_dry_run`` is True only when every check passes. It is a statement about this report, not an
    approval: activation still needs everything in docs/LAST_SCAN_ACTIVATION_CHECKLIST.md."""
    from hosting import last_scan_runner as runner
    from hosting import source_first_workbook as wb
    a = _dict(assignment)
    checks = []
    def check(code, basis, ok, detail='', evidence_needed=''):
        checks.append(dict(check=code, basis=basis, ok=bool(ok), detail='' if ok else detail, evidence_needed='' if ok else evidence_needed))
        return bool(ok)
    text = _text
    mapping = _dict(a.get('mapping'))
    fields = _dict(mapping.get('fields'))
    columns, malformed_columns = _columns(schema)
    raw_contract = a.get('field_contract')

    # ---- identity (discovered + exact configuration)
    identity = check('assignment_identity_complete', 'configured', all(text(a.get(k)) for k in ('client_id', 'job', 'site_id', 'list_id')),
                     'Client, job, site id and list id must each be exact text.')
    site = a['site_id'].split(',') if text(a.get('site_id')) else []
    check('site_and_list_ids_are_exact_identifiers', 'configured',
          len(site) == 3 and site[0].endswith('.sharepoint.com') and all(re.fullmatch(GUID, p) for p in site[1:])
          and text(a.get('list_id')) and bool(re.fullmatch(GUID, a['list_id'])),
          'The site id must be host,site-guid,web-guid and the list id a GUID. Names and URLs are not identifiers.')
    have_schema = check('schema_snapshot_supplied', 'discovered', isinstance(schema, dict) and isinstance(schema.get('columns'), dict) and bool(schema['columns']),
                        'No schema snapshot for this list was supplied, or its columns are not a name-to-definition object, so nothing about its columns can be checked.',
                        'A read-only capture of the list\'s columns, taken by an approved reader.')
    if have_schema:
        check('schema_columns_are_well_formed', 'discovered', not malformed_columns,
              f'These schema entries are not column definitions: {", ".join(malformed_columns)}. A snapshot with damaged entries cannot be trusted for the rest.')
        listed = schema.get('list')
        check('schema_is_for_this_site_and_list', 'discovered',
              schema.get('site_id') == a.get('site_id') and schema.get('list_id') == a.get('list_id')
              and (listed is None or (isinstance(listed, dict) and listed.get('id') in (None, a.get('list_id')))),
              'The schema snapshot belongs to another site or list, or its list identity is malformed.')
    if registry_entries is None:
        problem = 'job_registry_unavailable'
    elif not check('registry_entries_are_well_formed', 'configured', _registry_shape_ok(registry_entries),
                   'The job registry must be a list of entries whose client key, job, source, site id and list id are plain values.'):
        problem = 'job_registry_malformed'
    elif not identity:
        problem = 'assignment_identity_incomplete'
    else:
        problem = runner._registry_problem(a, registry_entries)             # not wrapped: a bug in the rule must surface
    check('exact_verified_registry_entry', 'configured', problem is None, str(problem),
          'A verified job registry entry with exactly this client key, job, site id and list id.')

    # ---- timezone
    zone_ok = False
    if text(a.get('zone')):
        try:
            zone_ok = bool(ZoneInfo(a['zone']))
        except (KeyError, ValueError, OSError):                              # unknown, malformed, or a path that is not a zone
            zone_ok = False
    check('timezone_is_a_real_zone', 'configured', zone_ok, 'The timezone must be a real zone name such as America/Chicago.')

    # ---- field contract shape
    check('field_contract_is_well_formed', 'configured',
          raw_contract is None or (isinstance(raw_contract, dict) and all(isinstance(k, str) and isinstance(v, dict) for k, v in raw_contract.items())),
          'field_contract must be an object of entries, each an object with column, column_id, type and definition.')

    # ---- every mapped report field: exists once, well formed, not calculated, pinned by id, type and definition, no drift
    def pinned(key, name, allowed=None, label=None):
        label = label or key
        column = columns.get(name) if text(name) else None
        missing = (f'The schema entry for {name!r} is not a column definition.' if repr(name) in malformed_columns
                   else f'No column named {name!r} exists in the schema snapshot.')
        if not check(label + '_column_exists', 'discovered', column is not None, missing):
            return None
        shared = [n for n, c in columns.items() if n != name and str(c.get('displayName') or '').strip().lower() in
                  (str(column.get('displayName') or '').strip().lower(), name.lower())]
        check(label + '_column_unambiguous', 'discovered', not shared,
              f'Column {name!r} shares its name or display name with {sorted(shared)}; the mapping could mean either.',
              'A person confirming, by column ID, which column is meant.')
        problems = column_problems(column)
        check(label + '_type_facets_consistent', 'discovered', not problems,
              f'The definition of {name!r} cannot be relied on: ' + '; '.join(problems) + '.',
              'A clean read-only capture of this column showing exactly one type.')
        check(label + '_is_not_calculated', 'discovered', 'calculated' not in column,
              f'{name!r} carries a calculated facet. A computed column is derived from other data and is never accepted, whatever else the column says.')
        contract, kind = _contract(a, key), column_type(column)
        check(label + '_pinned_by_column_id_and_type', 'configured',
              contract is not None and contract.get('column') == name and all(text(contract.get(k)) for k in ('column_id', 'type', 'definition')),
              f'The assignment does not pin {label} to a column ID, type and definition hash.', 'field_contract entry: column, column_id, type, definition.')
        if contract is not None:
            check(label + '_column_id_unchanged', 'discovered', contract.get('column_id') == column.get('id'),
                  f'{name!r} now has column ID {column.get("id")!r}, not the pinned {contract.get("column_id")!r}: the column was replaced.')
            check(label + '_type_unchanged', 'discovered',
                  contract.get('type') == kind and contract.get('format') in (None, column['dateTime'].get('format') if kind == 'dateTime' else None),
                  f'{name!r} is now type {kind!r}, not the pinned {contract.get("type")!r}.')
            check(label + '_definition_unchanged', 'discovered', contract.get('definition') == column_evidence(column),
                  f'The definition of {name!r} (facets, read-only, hidden, required, default, validation or names) is not the one that was pinned.',
                  'Review the changed column, re-pin it, and have the affected meaning attested again.')
        check(label + '_type_known', 'discovered', kind not in ('unknown', 'contradictory', 'malformed'), f'The schema does not say unambiguously what type {name!r} is ({kind}).')
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
            column = pinned(field, fields[field], QUANTITY_TYPES if field in sf.QUANTITIES else None)
            if field == 'date' and column is not None:
                check('date_is_a_date_only_column', 'discovered', column_type(column) == 'dateTime' and column['dateTime'].get('format') == 'dateOnly',
                      'The business-date column must be a date-only column.')

    # ---- scan field: never date/arrival metadata; a real, entered date-time; meaning cannot be discovered
    scan = a.get('scan_field')
    acceptable = text(scan) and scan.lower() not in FORBIDDEN_SCAN_FIELDS and scan != fields.get('date')
    check('scan_field_is_not_date_or_arrival_metadata', 'configured', acceptable,
          'The scan field is missing, or is the date column or creation/modification metadata. Those are never the actual scan time.')
    if acceptable:
        column = pinned('scan', scan, ('dateTime',), 'scan_field')
        if column is not None:
            check('scan_field_is_a_date_and_time', 'discovered', column_type(column) == 'dateTime' and column['dateTime'].get('format') == 'dateTime',
                  'The scan field must hold a date AND time; a date-only column cannot time a quiet hour.')
            check('scan_field_is_entered_not_computed', 'discovered', 'calculated' not in column and column.get('readOnly', False) is False,
                  'The scan field is computed or read-only, which is typical of system metadata.')
            check('scan_field_has_no_default_value', 'discovered', 'defaultValue' not in column,
                  'The scan field has a default value. A default such as "now" would stamp the time the row was created, not the scan.')

    # ---- shift field and values
    shift, recorded = a.get('shift_field'), a.get('recorded_shifts')
    recorded_ok = isinstance(recorded, list) and bool(recorded) and all(text(v) for v in recorded) and len(recorded) == len(set(recorded))
    check('recorded_shifts_listed', 'configured', recorded_ok, 'recorded_shifts must list each exact shift value once.')
    check('shift_field_is_the_mapped_shift_column', 'configured', text(shift) and shift == fields.get('shift'),
          'The shift field used to split shifts must be the same column the report maps as shift.')
    shift_column = columns.get(shift) if text(shift) else None
    if shift_column is not None and _contract(a, 'shift') is not None:
        check('shift_field_type_supported', 'discovered', column_type(shift_column) in SHIFT_TYPES, 'The shift column must be a choice, text or number column.')
    if recorded_ok:
        shifts = _dict(mapping.get('shifts'))
        unmapped = [v for v in recorded if not (isinstance(shifts.get(v), str) and shifts[v] in sf.SHIFT_NAMES)]
        check('every_recorded_shift_maps_to_a_template_shift', 'configured', not unmapped,
              f'No template shift is mapped for recorded value(s) {unmapped}.')
        if shift_column is not None and column_type(shift_column) == 'choice':
            choices = list(shift_column['choice']['choices'])
            check('recorded_shifts_exist_as_choices', 'discovered', all(v in choices for v in recorded),
                  f'Recorded shift value(s) {[v for v in recorded if v not in choices]} are not choices of the shift column {choices}.')
            check('every_shift_choice_is_covered', 'discovered', all(c in recorded for c in choices),
                  f'Shift choice(s) {[c for c in choices if c not in recorded]} are in no assignment; rows with them would be in no draft.',
                  'Either add the value to recorded_shifts (with its meaning attested) or confirm it is never used.')

    # ---- template / header (a broken pinned template is not an input problem and raises)
    header = a.get('header')
    extra = sorted(str(k) for k in header if k not in wb.HEADER_FIELDS and k not in ('photo_choices', 'evidence')) if isinstance(header, dict) else ['(no header object)']
    check('header_uses_only_template_fields', 'configured', not extra, f'Unknown header field(s): {extra}.')

    # ---- sample rows: observations only, and red flags; never evidence FOR a meaning
    observations = []
    if sample_rows is not None and check('sample_rows_are_well_formed', 'discovered', isinstance(sample_rows, list) and all(isinstance(r, dict) for r in sample_rows),
                                         'Sample rows must be a list of row objects; these are not, so none of them was examined.') and sample_rows:
        if text(scan):
            bad = 0
            for r in sample_rows:
                try:
                    instant(r.get(scan))
                except (ValueError, OverflowError):                          # missing, not text, unparseable, no offset, or out of range
                    bad += 1
            check('sample_scan_values_are_offset_aware_times', 'discovered', not bad, f'{bad} of {len(sample_rows)} sample scan values are missing or not offset-aware date-times.')
        if text(shift) and recorded_ok:
            seen = sorted({str(r.get(shift)) for r in sample_rows if r.get(shift) is not None and not (isinstance(r.get(shift), str) and r.get(shift) in recorded)})
            check('sample_shift_values_are_all_recorded_shifts', 'discovered', not seen, f'Sample rows carry shift value(s) {seen} that no assignment covers.')
            check('sample_rows_all_have_a_shift', 'discovered', all(r.get(shift) is not None for r in sample_rows), 'Some sample rows have no shift value.')
        if text(scan):
            for arrival in ('Created', 'Modified', 'createdDateTime', 'lastModifiedDateTime'):
                pairs = [(r.get(scan), r.get(arrival)) for r in sample_rows if r.get(scan) is not None and r.get(arrival) is not None]
                if pairs and all(isinstance(x, str) and x == y for x, y in pairs):
                    check('scan_values_differ_from_' + arrival.lower(), 'discovered', False,
                          f'In every sample row the scan value equals {arrival}. It looks like arrival metadata under another name.',
                          'Evidence of how the scan field is filled in on the tablet.')
        for q in sf.QUANTITIES:
            if text(fields.get(q)):
                values = [r.get(fields[q]) for r in sample_rows]
                observations.append(dict(field=q, absent_or_blank=sum(1 for v in values if v is None or (isinstance(v, str) and v == '')),
                                         structured=sum(1 for v in values if isinstance(v, (dict, list))), rows=len(values)))
        observations.append(dict(note='Sample observations describe these rows only. They are never evidence that a field means what the assignment says.'))

    # ---- attested business meaning: bound to the exact contract, never inferred, never created here
    bindings = attestation_bindings(a) if mapping else {}
    given = _dict(a.get('attestations'))
    for semantic, statement in SEMANTICS.items():
        record = given.get(semantic)
        complete = isinstance(record, dict) and all(text(record.get(k)) for k in ('attested_by', 'attested_at', 'evidence'))
        check(semantic + '_attested', 'attested', complete and semantic in bindings and record.get('bound_to') == bindings[semantic],
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

"""TEST-ONLY helpers that build a synthetic schema snapshot and a fully attested synthetic assignment.

``attested`` writes attestations by a fictitious reviewer so tests can exercise the ready path. Nothing in the
application can do this: the validator never creates an attestation and the runner never reads this module."""
from copy import deepcopy
from hosting import source_first as sf
from hosting import last_scan_readiness as readiness

QUANTITY_COLUMNS = ('inspected', 'rejected', 'certified')


def schema_for(assignment, extra_columns=(), choices=None):
    """A synthetic schema whose columns have IDs and types, shaped to fit the given assignment's mapping."""
    fields = assignment['mapping']['fields']
    names = list(dict.fromkeys(list(fields.values()) + [assignment.get('scan_field') or 'ActualScan', 'DefectPhoto'] + list(extra_columns)))
    columns = {}
    for i, name in enumerate(n for n in names if isinstance(n, str) and n):
        h = sf.digest(dict(site=assignment['site_id'], list=assignment['list_id'], column=name))      # column IDs are specific to the list
        col = {'name': name, 'displayName': name, 'id': '%s-%s-4%s-8%s-%s' % (h[:8], h[8:12], h[12:15], h[15:18], h[18:30]), 'text': {}}
        columns[name] = col
    def facet(name, key, value):
        if name in columns:
            columns[name].pop('text', None); columns[name][key] = value
    facet(fields['date'], 'dateTime', {'format': 'dateOnly'})
    for q in QUANTITY_COLUMNS:
        facet(fields[q], 'number', {})
    facet(fields['shift'], 'choice', {'choices': list(choices if choices is not None else assignment.get('recorded_shifts') or [])})
    facet('DefectPhoto', 'thumbnail', {})
    for name in [assignment.get('scan_field')] + list(extra_columns):
        if isinstance(name, str) and name in columns and name not in fields.values():
            facet(name, 'dateTime', {'format': 'dateTime'})
    return dict(site_id=assignment['site_id'], list_id=assignment['list_id'], captured_at='2026-11-10T21:00:00+00:00',
                list=dict(id=assignment['list_id'], displayName='Synthetic list'), columns=columns)


def pin(assignment, schema):
    """field_contract from the schema: what a reviewer would copy down after looking at the columns."""
    a = deepcopy(assignment); fields = a['mapping']['fields']; contract = {}
    def entry(name):
        c = schema['columns'][name]
        out = dict(column=name, column_id=c.get('id'), type=readiness.column_type(c), definition=readiness.column_evidence(c))
        if out['type'] == 'dateTime':
            out['format'] = c['dateTime'].get('format')
        return out
    for field in sf.FIELDS:
        if fields.get(field) in schema['columns']:
            contract[field] = entry(fields[field])
    if a.get('scan_field') in schema['columns']:
        contract['scan'] = entry(a['scan_field'])
    if a.get('shift_field') in schema['columns']:
        contract['shift'] = entry(a['shift_field'])
    a['field_contract'] = contract
    return a


def attested(assignment, schema=None, by='Synthetic reviewer (test fixture)', only=None):
    """TEST ONLY: pin the contract and attach attestations bound to it."""
    schema = schema or schema_for(assignment)
    a = pin(assignment, schema)
    bindings = readiness.attestation_bindings(a)
    a['attestations'] = {k: dict(attested_by=by, attested_at='2026-11-10', evidence='tests/readiness_fixture.py (synthetic)', bound_to=v)
                         for k, v in bindings.items() if only is None or k in only}
    return a, schema

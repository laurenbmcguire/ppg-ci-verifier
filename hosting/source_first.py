"""Sealed, source-first DAILY review. Source facts are never edited in place.

This is an internal held-report pilot, not shift completion or customer release.
The reviewed snapshot remains usable after its live freshness receipt expires.
"""
from copy import deepcopy
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import re

VERSION = 'source-first-daily-v1'
FRESH_SECONDS = 600
MAX_ROWS = 2000
# Match the existing private API pilot budget, below Vercel's 4.5 MB limit.
# This pilot has no verified streaming transport.
MAX_RESPONSE_BYTES = 3 * 1024 * 1024
FIELDS = ('date', 'shift', 'location', 'part', 'identifier', 'inspected_for',
          'inspected', 'condition', 'reject_reason', 'rejected', 'certified', 'comments')
QUANTITIES = ('inspected', 'rejected', 'certified')
REQUIRED = ('date', 'shift', 'part', 'identifier', *QUANTITIES)
SHIFT_NAMES = ('1st Shift', '2nd Shift', '3rd Shift')
DEFAULT_SHIFTS = {'1': '1st Shift', '1st': '1st Shift', '1st Shift': '1st Shift',
                  '2': '2nd Shift', '2nd': '2nd Shift', '2nd Shift': '2nd Shift',
                  '3': '3rd Shift', '3rd': '3rd Shift', '3rd Shift': '3rd Shift'}


class ReviewError(ValueError):
    pass


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False).encode()


def digest(value):
    return hashlib.sha256(value if isinstance(value, bytes) else canonical(value)).hexdigest()


def now():
    return datetime.now(timezone.utc).isoformat()


def actor(user):
    if not isinstance(user, dict) or user.get('virtual') or user.get('role') != 'admin' or not user.get('id'):
        raise PermissionError('A signed-in administrator is required. A simulated reviewer cannot approve source facts.')
    return {'id': str(user['id']), 'name': str(user.get('name') or user.get('email') or user['id'])[:200]}


def scope(value):
    if not isinstance(value, dict):
        raise ReviewError('Choose the exact job, site, list, and business date.')
    out = {k: value.get(k) for k in ('job', 'site_id', 'list_id', 'business_date')}
    if any(not isinstance(v, str) or not v.strip() or len(v) > 300 or any(ord(c) < 32 for c in v) for v in out.values()):
        raise ReviewError('The exact source scope is incomplete.')
    if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', out['business_date']):
        raise ReviewError('Use a business date in YYYY-MM-DD format.')
    date.fromisoformat(out['business_date'])
    return out


def typed(fields, key):
    if not key or key not in fields:
        return {'type': 'absent'}
    v = fields[key]
    if v is None:
        return {'type': 'null'}
    if type(v) is str:
        return {'type': 'text', 'value': v}
    if type(v) in (int, float):
        canonical(v)
        return {'type': 'number', 'value': v}
    if type(v) is bool:
        return {'type': 'boolean', 'value': v}
    return {'type': 'structured', 'value': deepcopy(v)}


def replacement(value):
    if not isinstance(value, dict) or value.get('type') not in ('null', 'absent', 'text', 'number'):
        raise ReviewError('Use a typed text, number, null, or absent replacement.')
    if value['type'] in ('null', 'absent'):
        if set(value) != {'type'}:
            raise ReviewError('Null or absent must not contain a value.')
    elif set(value) != {'type', 'value'} or (value['type'] == 'text' and type(value['value']) is not str) or (value['type'] == 'number' and type(value['value']) not in (int, float)):
        raise ReviewError('Replacement type does not match its value.')
    if len(canonical(value)) > 8000:
        raise ReviewError('Replacement is too long.')
    return deepcopy(value)


def missing(value):
    return value['type'] in ('absent', 'null') or (value['type'] == 'text' and not value['value'].strip())


def quantity(value):
    if missing(value) or value['type'] not in ('text', 'number'):
        return None
    try:
        # No comma stripping, rounding, booleans or other guessed conversions.
        text = str(value['value'])
        if not re.fullmatch(r'\d+(?:\.0+)?', text):
            return None
        d = Decimal(text)
        return int(d) if d.is_finite() and 0 <= d <= 10**12 else None
    except (InvalidOperation, ValueError):
        return None


def mapping(value, columns):
    if not isinstance(value, dict) or set(value) != {'fields', 'shifts'} or not isinstance(value['fields'], dict) or not isinstance(value['shifts'], dict):
        raise ReviewError('Review the explicit field and shift mapping.')
    if set(value['fields']) != set(FIELDS):
        raise ReviewError('Map every report field, using null for an unavailable optional field.')
    for label, field in value['fields'].items():
        if field is not None and (not isinstance(field, str) or field not in columns):
            raise ReviewError(f'Unknown source column for {label}.')
    if value['fields']['date'] != 'Date':
        raise ReviewError('This pilot requires the verified internal Date business-date field.')
    for key, target in value['shifts'].items():
        if not isinstance(key, str) or len(key) > 100 or target not in SHIFT_NAMES:
            raise ReviewError('Shift mapping must name an explicit supported shift.')
    return deepcopy(value)


def semantic_capture(capture):
    """Renewing a timezone receipt alone does not change the reviewed content."""
    contract = deepcopy(capture.get('date_contract'))
    if contract:
        contract.pop('captured_at', None)
        contract.pop('expires_at', None)
    return dict(scope=capture['scope'], columns=capture['columns'], date_contract=contract,
                records=sorted(capture['records'], key=lambda x: x['id']),
                uncertain_dates=capture.get('uncertain_dates', []), photos=capture.get('photos', []))


def seal(capture):
    capture = deepcopy(capture)
    scope(capture['scope'])
    coverage = capture.get('coverage') or {}
    if not coverage.get('items_terminal') or not coverage.get('columns_terminal') or coverage.get('remaining_cursor') is not None:
        raise ReviewError('Terminal item and schema pagination evidence is required.')
    if not coverage.get('pages') or any(p.get('status') != 200 or not p.get('page_hash') for p in coverage['pages']):
        raise ReviewError('Successful page receipts are required.')
    for kind in ('items', 'columns'):
        chain = [p for p in coverage['pages'] if p.get('kind') == kind]
        if not chain or not chain[-1].get('terminal') or chain[-1].get('next_cursor_hash') is not None or any(p.get('terminal') for p in chain[:-1]):
            raise ReviewError('Terminal pagination receipts do not match the capture claim.')
    if len(capture['records']) > MAX_ROWS:
        raise ReviewError('This pilot source scope exceeds its record limit. Nothing was truncated.')
    ids = [r.get('id') for r in capture['records']]
    if any(not isinstance(i, str) or not i for i in ids) or len(ids) != len(set(ids)):
        raise ReviewError('Missing or duplicate source IDs require a new complete capture.')
    if any(not isinstance(r.get('raw'), dict) for r in capture['records']):
        raise ReviewError('Raw source values are missing.')
    capture['snapshot_hash'] = digest(semantic_capture(capture))
    capture['envelope_hash'] = digest({k: v for k, v in capture.items() if k != 'envelope_hash'})
    return capture


def verify_seal(capture):
    if capture != seal({k: v for k, v in capture.items() if k not in ('envelope_hash', 'snapshot_hash')}):
        raise ReviewError('Saved source snapshot failed its integrity check.')


def proposal(capture, record_id, field, after, evidence, user, existing=()):
    actor_ref = actor(user)
    if field not in FIELDS:
        raise ReviewError('Unknown report field.')
    row = next((r for r in capture['records'] if r['id'] == record_id), None)
    if row is None:
        raise ReviewError('The source record is not in this exact snapshot.')
    if not isinstance(evidence, str) or not 5 <= len(evidence.strip()) <= 2000:
        raise ReviewError('Record evidence for this correction, not merely an acknowledgment.')
    return dict(record_id=record_id, field=field, replacement=replacement(after), evidence=evidence.strip(),
                snapshot_hash=capture['snapshot_hash'], record_hash=digest(row), actor=actor_ref, at=now())


def reviewed_rows(capture, mapping_value, overlays):
    verify_seal(capture)
    m = mapping(mapping_value, capture['columns'])
    rows, findings = [], []
    def issue(code, text, record_id=None, field=None, blocking=True):
        findings.append(dict(code=code, text=text, record_id=record_id, field=field, blocking=blocking))
    if capture.get('uncertain_dates'):
        issue('uncertain_coverage', 'Some list records have unknown business dates. Correct their dates at source and recapture before approving this day.')
    if not capture['records']:
        issue('empty_scope', 'No source records were found for this business date. An empty capture is not a completed shift.')
    applied = {}
    for overlay in overlays:
        r = next((r for r in capture['records'] if r['id'] == overlay.get('record_id')), None)
        if not r or overlay.get('snapshot_hash') != capture['snapshot_hash'] or overlay.get('record_hash') != digest(r):
            raise ReviewError('Source changed. The correction must be reviewed again.')
        field = overlay.get('field')
        if field not in FIELDS or overlay.get('original') != typed(r['raw'], m['fields'][field]):
            raise ReviewError('Correction original or mapping differs from the reviewed source.')
        applied[(r['id'], field)] = replacement(overlay['replacement'])
    duplicates = {}
    for record in capture['records']:
        values = {f: applied.get((record['id'], f), typed(record['raw'], m['fields'][f])) for f in FIELDS}
        resolved = {f: None if missing(v) else v.get('value') for f, v in values.items()}
        for field, value in values.items():
            if value['type'] in ('structured', 'boolean'):
                issue('unsupported_value_type', f'{field} has an unsupported {value["type"]} source value. Review an exact typed replacement.', record['id'], field)
                resolved[field] = None
        for field in REQUIRED:
            if missing(values[field]):
                issue('missing_value', f'{field} is {values[field]["type"]} or blank, not zero.', record['id'], field)
        for field in QUANTITIES:
            resolved[field] = quantity(values[field])
            if not missing(values[field]) and resolved[field] is None:
                issue('invalid_quantity', f'{field} must be an explicit nonnegative whole quantity.', record['id'], field)
        for field in ('part', 'identifier'):
            if not missing(values[field]) and values[field]['type'] != 'text':
                issue('identifier_type', f'{field} is not text. Confirm its exact representation and any leading zeros.', record['id'], field)
        date_overlay = applied.get((record['id'], 'date'))
        resolved['date'] = date_overlay.get('value') if date_overlay else record.get('day')
        if resolved['date'] != capture['scope']['business_date']:
            issue('date_scope', 'The approved record date must equal this report business date.', record['id'], 'date')
        raw_shift = resolved['shift']
        if type(raw_shift) in (int, float):
            raw_shift = str(raw_shift)
        resolved['shift'] = m['shifts'].get(raw_shift)
        if not resolved['shift']:
            issue('unknown_shift', 'No approved exact mapping for the recorded shift.', record['id'], 'shift')
        if all(resolved[f] is not None for f in QUANTITIES) and resolved['inspected'] != resolved['rejected'] + resolved['certified']:
            issue('quantity_consistency', 'Inspected must equal rejected plus certified. Review source facts; no automatic adjustment.', record['id'])
        if not isinstance(record.get('version'), str) or not record['version'].strip():
            issue('missing_version', 'Source version evidence is missing.', record['id'])
        key = tuple(str(resolved.get(f)) for f in ('date', 'shift', 'part', 'identifier'))
        duplicates.setdefault(key, []).append(record['id'])
        photos = [p for p in capture.get('photos', []) if p['record_id'] == record['id']]
        if resolved.get('rejected') and not any(p.get('status') == 'supported' for p in photos):
            issue('photo_unavailable', 'No supported, verified defect photo is available for this rejected record.', record['id'], blocking=False)
        rows.append(dict(id=record['id'], record_hash=digest(record), source_version=record['version'],
                         original_values={f: typed(record['raw'], m['fields'][f]) for f in FIELDS},
                         mapped_fields=deepcopy(m['fields']),
                         values=values, resolved=resolved, photos=photos))
    for ids in duplicates.values():
        if len(ids) > 1:
            issue('duplicate_records', 'Repeated date/shift/part/identifier records remain separate: ' + ', '.join(ids))
    return rows, findings


def binding(capture, mapping_value, overlays, header, template_hash, contract_hash, generator_hash):
    return dict(snapshot_hash=capture['snapshot_hash'], overlay_hash=digest(overlays), mapping_hash=digest(mapping_value),
                header_hash=digest(header), template_hash=template_hash, contract_hash=contract_hash,
                generator_hash=generator_hash, model_version=VERSION)


def fresh(receipt, snapshot_hash, at=None):
    if not receipt or receipt.get('snapshot_hash') != snapshot_hash or receipt.get('match') is not True:
        return False
    try:
        stamp = datetime.fromisoformat(receipt['compared_at'])
        elapsed = ((at or datetime.now(timezone.utc)) - stamp).total_seconds()
        return stamp.tzinfo is not None and 0 <= elapsed < FRESH_SECONDS
    except (ValueError, TypeError, KeyError):
        return False


def changes(old, new):
    a, b = ({r['id']: digest(r) for r in c['records']} for c in (old, new))
    return dict(added=sorted(b.keys() - a.keys()), deleted=sorted(a.keys() - b.keys()),
                changed=sorted(k for k in a.keys() & b.keys() if a[k] != b[k]),
                semantics_changed=digest({k: v for k, v in semantic_capture(old).items() if k != 'records'}) !=
                                  digest({k: v for k, v in semantic_capture(new).items() if k != 'records'}))

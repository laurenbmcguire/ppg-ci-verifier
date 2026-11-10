"""Exact, expiring source support for pattern hints. Never a cell correction."""
from collections import Counter
from datetime import datetime, timezone
from hosting.branded_reports import fingerprint

CODES = {'part_format_differs': 'part', 'identifier_format_differs': 'identifier'}
MAX_AGE_SECONDS = 600


def capture(records, snap, day, same_list, snapshot_id, bound):
    from hosting.adaptive_reports import exact_label_for_header, shape
    columns = {}
    for name, col in (snap.get('columns') or {}).items():
        if not col.get('hidden'):
            field = exact_label_for_header(col.get('displayName') or name)
            if field in CODES.values():
                columns.setdefault(field, []).append(name)
    items = snap.get('items') or []
    item_counts = Counter(i.get('item') for i in items)
    record_counts = Counter((r.get('source') or {}).get('item') for r in records)
    locations = Counter((r.get('sheet'), r.get('row')) for r in records)
    by_item = {i.get('item'): i for i in items}
    # Retain evidence only for pattern outliers, not a second copy of every identifier.
    # This mirrors the deterministic check; a future unsupported rule fails closed.
    targets = set()
    for field in CODES.values():
        values = [(r, (r.get('labels', {}).get(field) or '').strip()) for r in records if r.get('labels', {}).get(field)]
        if len(values) < 4:
            continue
        common, count = Counter(shape(value) for _, value in values).most_common(1)[0]
        if count >= .75 * len(values):
            targets.update((r['sheet'], r['row'], field) for r, value in values if shape(value) != common)
    proofs = []
    for r in records:
        if len(proofs) > 2000:
            break
        src = r.get('source') or {}; key = src.get('item'); it = by_item.get(key)
        if (not key or same_list(src) is not True or record_counts[key] != 1 or item_counts[key] != 1
                or locations[(r.get('sheet'), r.get('row'))] != 1 or not it or it.get('day') != day):
            continue
        for field, names in columns.items():
            if (r['sheet'], r['row'], field) not in targets:
                continue
            value = (r.get('exact_labels') or {}).get(field)
            col = (r.get('label_columns') or {}).get(field)
            if len(names) != 1 or not col or not isinstance(value, str) or not value or len(value) > 4096:
                continue
            source = (it.get('raw') or {}).get(names[0])
            if type(source) is not str or source != value:
                continue
            proofs.append(dict(field=field, sheet=r['sheet'], row=r['row'], cell=f"{col}{r['row']}",
                               item=key, record_source=src, value=value, source_value=source, source_column=names[0],
                               source_version=it.get('version'), source_modified=it.get('modified')))
    body = dict(schema='exact-source-pattern/1', snapshot_id=snapshot_id, bound_to=bound,
                site_id=snap.get('site_id'), list_id=snap.get('list_id'), day=day,
                captured_at=snap.get('captured_at'), report_rows=len(records),
                complete=len(proofs) <= 2000 and bool(snap.get('coverage', {}).get('complete')), proofs=proofs[:2000])
    return dict(body, digest=fingerprint(body))


def supported(result, issue, context):
    from hosting import reconcile
    from hosting.spreadsheets import CHECKER_VERSION
    field = CODES.get(issue.get('code'))
    if not field or issue.get('source') != 'Checks' or not context or context.get('valid') is not True:
        return None
    if (result.get('checker_version') != CHECKER_VERSION or result.get('records_truncated')
            or (result.get('dataset_selection') or {}).get('status') == 'ambiguous'):
        return None
    rec = reconcile.current(result)
    if (not rec or reconcile.Coverage(rec.get('coverage') or {}).incomplete() or rec.get('rows_truncated')
            or rec.get('unidentified_rows') or rec.get('unconfirmed_rows')):
        return None
    receipt = rec.get('identifier_pattern_evidence') or {}
    if not isinstance(receipt, dict) or not isinstance(receipt.get('proofs'), list) or any(not isinstance(p, dict) for p in receipt['proofs']):
        return None
    if (receipt.get('schema') != 'exact-source-pattern/1' or receipt.get('complete') is not True
            or receipt.get('digest') != fingerprint({k:v for k,v in receipt.items() if k != 'digest'})
            or any(not receipt.get(k) or receipt.get(k) != rec.get(k) for k in ('snapshot_id', 'site_id', 'list_id', 'day', 'captured_at', 'bound_to'))
            or receipt.get('report_rows') != result.get('record_count') or len(result.get('records') or []) != result.get('record_count')
            or context.get('site_id') != rec.get('site_id') or context.get('list_id') != rec.get('list_id')):
        return None
    try:
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(receipt['captured_at'].replace('Z', '+00:00'))).total_seconds()
        if not 0 <= age <= MAX_AGE_SECONDS:
            return None
    except (ValueError, TypeError, KeyError):
        return None
    records = [r for r in result.get('records') or [] if r.get('sheet') == issue.get('sheet') and r.get('row') == issue.get('row')]
    proofs = [p for p in receipt.get('proofs') or [] if p.get('sheet') == issue.get('sheet') and p.get('row') == issue.get('row') and p.get('field') == field]
    if len(records) != 1 or len(proofs) != 1:
        return None
    r, proof = records[0], proofs[0]
    value = (r.get('exact_labels') or {}).get(field)
    col = (r.get('label_columns') or {}).get(field)
    src = r.get('source') or {}
    if (not isinstance(value, str) or not value or not col or proof.get('cell') != f"{col}{r['row']}"
            or proof.get('value') != value or proof.get('source_value') != value or not proof.get('source_column')
            or proof.get('record_source') != src or proof.get('item') != src.get('item')
            or sum((x.get('source') or {}).get('item') == src.get('item') for x in result.get('records') or []) != 1):
        return None
    return dict(kind='source_confirmed_pattern', snapshot_id=rec['snapshot_id'], cell=proof['cell'],
                field=field, item=proof['item'], source_column=proof['source_column'], captured_at=receipt['captured_at'],
                message='Exact identifier matches the assigned source. Different shape is informational; the cell was not changed.')

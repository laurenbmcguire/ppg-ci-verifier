"""Narrow append-only recovery of a legacy self-link, never guessed ancestry.

A receipt seals the complete prior check prefix. Old records and upload bytes
remain untouched. Only their self-edges are excluded from the effective graph.
Foreign ancestry, approvals, sends and changed evidence fail closed.
"""
from copy import deepcopy
from contextlib import contextmanager
from hosting.branded_reports import fingerprint

KIND = 'isolated-self-cycle-v1'
FIELD = 'history_recovery'


def isolated_prefix(key, base, records):
    if not records or base.get('file_hash') != key or base.get('fixed_from') or base.get('fixes_applied'):
        raise ValueError('A clean original review root could not be proved.')
    if records[0].get('fixed_from') or records[0].get('fixes_applied'):
        raise ValueError('The initial clean check record is missing.')
    parent = None
    fixes = []
    for r in records:
        if r.get('file_hash') != key or FIELD in r:
            raise ValueError('Recovery requires the exact unrecovered version history.')
        if r.get('approval') or r.get('sent') or r.get('send_pending') or r.get('send_in_progress'):
            raise ValueError('Approved, sent or sending history cannot be recovered here.')
        link = r.get('fixed_from')
        out = r.get('fixes_applied') or []
        if link:
            if not isinstance(link, dict) or link.get('file_hash') != key:
                raise ValueError('A different parent must be reviewed manually.')
            if parent is not None and parent != link:
                raise ValueError('Conflicting parent metadata cannot be recovered here.')
            parent = link
        elif parent is not None:
            raise ValueError('Parent history is incomplete.')
        if not isinstance(out, list) or any(not isinstance(x, dict) or x.get('fixed_file_hash') != key for x in out):
            raise ValueError('A different correction destination must be reviewed manually.')
        if fixes and out != fixes:
            raise ValueError('Correction history is inconsistent beyond an isolated self-link.')
        if out:
            if len(out) != 1:
                raise ValueError('Only one isolated legacy self-link can be recovered.')
            fixes = out
    if not parent or not fixes:
        raise ValueError('No isolated legacy self-link was proved.')
    return True


def cleared(record):
    out = deepcopy(record)
    out.pop('fixed_from', None)
    out['fixes_applied'] = []
    return out


def effective_histories(bases, histories):
    """Validate append-only receipts, then normalize only their sealed prefix.

    Invalid receipts are left untouched; their self-cycle remains unavailable.
    Later non-self correction edges retain the normal graph's strict guards.
    """
    effective = dict(histories)
    for key, records in histories.items():
        marked = [(i, r[FIELD]) for i, r in enumerate(records) if FIELD in r]
        if not marked:
            continue
        try:
            index, receipt = marked[0]
            if not isinstance(receipt, dict) or receipt.get('kind') != KIND or receipt.get('file_hash') != key:
                continue
            if type(receipt.get('prefix_count')) is not int or receipt['prefix_count'] != index or not index:
                continue
            if any(r != receipt for _, r in marked) or len(marked) != len(records) - index:
                continue
            prefix = records[:index]
            isolated_prefix(key, bases[key], prefix)
            if receipt.get('prefix_sha256') != fingerprint(prefix) or receipt.get('from_check') != prefix[-1].get('check_hash') or receipt.get('ignored_self_link') != {k:prefix[-1].get(k) for k in ('fixed_from','fixes_applied')}:
                continue
            if not isinstance(receipt.get('by'), dict) or receipt['by'].get('role') != 'admin' or not receipt['by'].get('id'):
                continue
            # The recovery record itself changes only graph metadata and audit.
            expected = cleared(prefix[-1])
            actual = deepcopy(records[index])
            for obj in (expected, actual):
                for field in ('check_hash', 'checked_at', FIELD):
                    obj.pop(field, None)
            if actual != expected:
                continue
            effective[key] = [cleared(r) for r in prefix] + records[index:]
        except (ValueError, TypeError, KeyError):
            continue
    return effective


def proposal(key, bases, histories):
    base = bases.get(key)
    records = histories.get(key, [])
    if not base:
        raise ValueError('Report not found.')
    isolated_prefix(key, base, records)
    # No other report can already name this report as parent or destination.
    for other in bases.keys() | histories.keys():
        checks = histories.get(other, [])
        if other == key:
            continue
        for r in [bases.get(other, {}), *checks]:
            link = r.get('fixed_from')
            if isinstance(link, dict) and link.get('file_hash') == key:
                raise ValueError('Another report refers to this history. Manual review is required.')
            if any(isinstance(x, dict) and x.get('fixed_file_hash') == key for x in (r.get('fixes_applied') or [])):
                raise ValueError('Another report refers to this history. Manual review is required.')
    receipt = dict(kind=KIND, file_hash=key, from_check=records[-1].get('check_hash'),
                   prefix_count=len(records), prefix_sha256=fingerprint(records),
                   ignored_self_link={k:deepcopy(records[-1].get(k)) for k in ('fixed_from','fixes_applied')})
    return dict(receipt=receipt, recovery_token=fingerprint(dict(bases=bases, histories=histories)),
                message='Restore review controls for this exact saved file by recording an audited exception for its self-link. Original bytes, all prior history, decisions and source holds stay unchanged. This does not verify, approve or send the report.')


@contextmanager
def outer_commit_only(inbox):
    """Recovery writes belong to the enclosing native workspace transaction."""
    pg = inbox.pg
    previous = pg.depth if pg is not None else None
    if pg is not None:
        pg.depth = previous + 1
    try:
        yield
    finally:
        if pg is not None:
            pg.depth = previous

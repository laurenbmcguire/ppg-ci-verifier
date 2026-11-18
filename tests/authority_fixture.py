"""TEST ONLY. Synthetic records in the shape hosting.last_scan_authority requires (version 1).

Nothing here is an approval system or a capture authority. The issuer names are invented, the validity windows are
fixed around the invented test dates, and a record made here proves nothing outside the test suite.
"""
from datetime import datetime, timedelta

from hosting import last_scan_authority as authority

APPROVAL_ISSUER, CAPTURE_ISSUER = 'synthetic-approval-authority', 'synthetic-capture-authority'
ISSUERS = dict(approval=(APPROVAL_ISSUER,), capture=(CAPTURE_ISSUER,))


def approval(digest, principals=('alice',), **change):
    """An active approval record, valid from 2026-11-10 to 2026-11-13 (the invented dates every test clock sits in).
    The record id follows from the digest, so a fake that builds its answer afresh on every call still answers with
    the SAME record, as a real authority would; a renewal has to be asked for with an explicit record_id."""
    record = dict(contract=authority.CONTRACT_APPROVAL, version=authority.VERSION, record_id='synthetic-approval-' + str(digest)[:16],
                  issuer=APPROVAL_ISSUER, status='active', snapshot_digest=digest, approved_by='synthetic approver',
                  approved_at='2026-11-10T00:00:00+00:00', principals=list(principals),
                  not_before='2026-11-10T00:00:00+00:00', expires_at='2026-11-13T00:00:00+00:00')
    record.update(change)
    return record


def receipt(fields, **change):
    """An active capture receipt around the six content fields a capture authority is asked to record. It expires one
    day after the capture, which is the longest the contract allows."""
    record = dict(fields, **change)
    try:
        expires = (datetime.fromisoformat(str(record.get('captured_at')).replace('Z', '+00:00')) + timedelta(days=1)).isoformat()
    except (ValueError, OverflowError):
        expires = None
    lifecycle = dict(contract=authority.CONTRACT_RECEIPT, version=authority.VERSION, issuer=CAPTURE_ISSUER, status='active',
                     record_id='synthetic-receipt-' + str(fields.get('snapshot_identity'))[:16], expires_at=expires)
    return dict(lifecycle, **record)

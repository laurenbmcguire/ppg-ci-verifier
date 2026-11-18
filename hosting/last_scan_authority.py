"""Record contract for what an approval authority and a capture authority must answer. CONTRACT ONLY.

This module implements no authority, holds no credential and reads nothing. It states, as plain data, exactly what a
record from an outside authority must look like before the last-scan runner will count it, and it judges one record
against a clock the HOST supplies. No real authority exists; the only ones are TEST-ONLY fakes in the test suite.

Why it exists: an authority that keeps a withdrawn or lapsed record and marks it with a field of its own choosing
("revoked": true, "expires": ...) used to be read as still valid, because unknown fields were ignored. Under this
contract every field is named, every unknown field is a refusal, and a record must say that it is active and for how
long. Anything missing, malformed, unsupported, not yet valid, expired or revoked is refused. There is no default.

Version 1, both kinds. Every value is JSON; every time is offset-aware ISO 8601 text; nothing may be added:

  approval (CONTRACT_APPROVAL)                          capture receipt (CONTRACT_RECEIPT)
    contract, version                                     contract, version
    record_id      the authority's own id for this        record_id
                   record; a renewal is a NEW id
    issuer         which authority wrote it; must be      issuer
                   one the host accepts for this kind
    status         exactly "active"                       status         exactly "active"
    snapshot_digest  what is approved                     snapshot_identity, site_id, list_id, schema_digest
    approved_by, approved_at                              collector, captured_at
    principals     who may act under it                   expires_at     at most MAX_RECEIPT_VALIDITY_SECONDS
    not_before, expires_at   validity bounds,                            after captured_at
                   at most MAX_APPROVAL_VALIDITY_SECONDS
    action         (only on an approval of an action
                   other than a run)
    revoked_at, revoked_by, revocation_reason             revoked_at, revoked_by, revocation_reason
                   optional; any of them set = revoked

Returning nothing still means "no record". Returning a record with status "revoked", or with any revocation field
set, is the explicit way to say it was withdrawn; both are refused, and the reason is reported.

The bounds below are placeholders pending an owner's decision. They are limits, not approvals.
"""
import json
from datetime import timezone

from hosting import source_first as sf
from hosting.last_scan_drafts import instant

VERSION = 1
CONTRACT_APPROVAL, CONTRACT_RECEIPT = 'last_scan.approval', 'last_scan.capture_receipt'
MAX_APPROVAL_VALIDITY_SECONDS = 7 * 24 * 3600      # placeholder: an approval may not be valid for longer than this
MAX_RECEIPT_VALIDITY_SECONDS = 24 * 3600           # placeholder: same bound as the capture age limit
FUTURE_SKEW_SECONDS = 300                          # a record dated further in the future than this is refused
REVOCATION = ('revoked_at', 'revoked_by', 'revocation_reason')
LIFECYCLE = ('contract', 'version', 'record_id', 'issuer', 'status')
FIELDS = {
    CONTRACT_APPROVAL: (LIFECYCLE + ('snapshot_digest', 'approved_by', 'approved_at', 'principals', 'not_before', 'expires_at'), ('action',) + REVOCATION),
    CONTRACT_RECEIPT: (LIFECYCLE + ('snapshot_identity', 'site_id', 'list_id', 'schema_digest', 'collector', 'captured_at', 'expires_at'), REVOCATION),
}
TIMES = {CONTRACT_APPROVAL: ('approved_at', 'not_before', 'expires_at'), CONTRACT_RECEIPT: ('captured_at', 'expires_at')}
KINDS = {'approval': CONTRACT_APPROVAL, 'capture': CONTRACT_RECEIPT}


def plain(answer):
    """ONE private copy of an authority's answer as plain JSON data, or None when it is not a record. Raises on
    anything that is not JSON. An object that answers differently each time it is asked is reduced to what it holds."""
    return json.loads(sf.canonical(answer)) if isinstance(answer, dict) else None


def freeze_issuers(value):
    """The accepted-issuer policy as a private immutable tuple of (kind, (issuers...)), or None (deny). The host's
    mapping is read exactly once; both kinds must be named, each with at least one exact identity."""
    try:
        taken = {k: tuple(v) for k, v in dict(value).items()} if isinstance(value, (dict, tuple)) else None   # a mapping, or this function's own output
    except (RuntimeError, TypeError, ValueError):
        return None
    if taken is None or set(taken) != set(KINDS):
        return None
    if any(not v or any(not isinstance(x, str) or not x.strip() or x != x.strip() for x in v) for v in taken.values()):
        return None
    return tuple(sorted((k, tuple(sorted(set(v)))) for k, v in taken.items()))


def _text(value):
    return isinstance(value, str) and bool(value.strip()) and value == value.strip()


def problems(record, kind, issuers, now, margin=0):
    """Why this one record does not count under the contract; [] when it does. ``now`` is the host's clock, never a
    time taken from the record. ``margin`` is how many more seconds the record must stay valid."""
    contract = KINDS[kind]
    if not isinstance(record, dict):
        return ['not_a_record']
    if record.get('contract') != contract:
        return ['unsupported_or_missing_contract']
    if type(record.get('version')) is not int or record['version'] != VERSION:
        return ['unsupported_version']
    required, optional = FIELDS[contract]
    out = []
    missing, unknown = sorted(set(required) - set(record)), sorted(set(record) - set(required) - set(optional))
    if missing:
        out.append('missing_fields:' + ','.join(missing))
    if unknown:                                                              # never ignored: an unknown field may mean "revoked"
        out.append('unknown_fields:' + ','.join(unknown))
    if not _text(record.get('record_id')):
        out.append('record_id_missing')
    accepted = dict(issuers or ()).get(kind)
    if not _text(record.get('issuer')) or accepted is None or record['issuer'] not in accepted:
        out.append('issuer_not_accepted')
    if record.get('status') == 'revoked' or any(record.get(k) is not None for k in REVOCATION):
        out.append('revoked')
    elif record.get('status') != 'active':
        out.append('status_not_active')
    try:
        current = instant(now)
        start_field = 'not_before' if kind == 'approval' else 'captured_at'
        start, end = instant(record.get(start_field)), instant(record.get('expires_at'))
        issued = instant(record.get('approved_at')) if kind == 'approval' else start
    except (ValueError, OverflowError, TypeError):
        return out + ['validity_missing_malformed_or_without_offset']
    limit = MAX_APPROVAL_VALIDITY_SECONDS if kind == 'approval' else MAX_RECEIPT_VALIDITY_SECONDS
    if end <= start or (end - start).total_seconds() > limit:
        out.append('validity_window_empty_or_longer_than_allowed')
    if (issued - current).total_seconds() > FUTURE_SKEW_SECONDS:
        out.append('issued_in_the_future')
    if kind == 'approval' and current < start:
        out.append('not_yet_valid')
    if (end - current).total_seconds() <= margin:
        out.append('expired' if current >= end else 'expires_too_soon_to_act_on')
    return out


def identity(record, kind):
    """One hash over the whole record with its times written canonically (UTC). The same instant written with another
    offset is the same record; any other difference, a renewal's new record_id or validity included, is another."""
    body = dict(record)
    for field in TIMES[KINDS[kind]]:
        try:
            body[field] = instant(body.get(field)).astimezone(timezone.utc).isoformat()
        except (ValueError, OverflowError, TypeError):
            pass
    return sf.digest(body)

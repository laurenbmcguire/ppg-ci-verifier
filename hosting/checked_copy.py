"""Read-only completion evidence for one immutable workbook, never an approval.

Counts use the entire server inventory before pagination. Setting a finding aside
is not a correction; a later version's correction does not clear its parent.
"""
from datetime import datetime, timezone, timedelta
import math
from hosting import reconcile
from hosting.spreadsheets import CHECKER_VERSION
from hosting.source_date_contracts import MAX_AGE_SECONDS
from hosting.finding_summary import inventory


def completion(item, review, holds, working, now=None):
    now = now or datetime.now(timezone.utc)
    reasons = []
    rows = list(review.get('issues') or []) + list(review.get('dismissed') or [])
    try:
        complete = inventory(review, holds)['coverage']['complete_available_inventory']
    except (ValueError, TypeError):
        complete = False
    if not complete:
        reasons.append('Finding coverage is incomplete or unknown. Recheck this report.')
    errors = [r for r in rows if r.get('severity') != 'info']
    ignored = sum(bool(r.get('dismissed_by')) or (r.get('decision') or {}).get('action') == 'reject' for r in errors)
    gaps = [r for r in rows if r.get('verification_gap') or r.get('code') in ('quantity_formulas_not_evaluated', 'no_data_rows', 'no_quantity_columns_detected')]
    if gaps:
        reasons.append(f'{len(gaps)} informational verification gaps remain; these are not counted as workbook errors.')
    if errors:
        reasons.append(f'{len(errors)} detected findings remain on this copy, including {ignored} set aside. Ignoring is not fixing.')
    if item.get('checker_version') != CHECKER_VERSION or item.get('internal_check_status') not in ('findings', 'no_findings_in_checks_performed') or not item.get('check_hash') or not item.get('checked_at'):
        reasons.append('The saved checks are missing or out of date. Recheck this version.')
    try:
        checked = datetime.fromisoformat(item.get('checked_at', '').replace('Z', '+00:00'))
        if checked.tzinfo is None or checked > now:
            raise ValueError('Invalid saved check time')
    except (ValueError, TypeError, AttributeError):
        reasons.append('The saved check time is invalid. Recheck this version.')
    if working.get('status') != 'current' or working.get('working_hash') != item.get('file_hash') or working.get('requested_hash') != item.get('file_hash'):
        reasons.append('This is not a confirmed current working copy. Choose the current version.')
    if working.get('read_only_reason') or item.get('approval') or item.get('send_pending') or item.get('send_in_progress') or any(s.get('sent') for s in item.get('sent') or []):
        reasons.append('This version is read-only. Its ordinary download remains available.')
    if holds:
        reasons.append('Report or source verification holds remain. Review the listed holds.')
    quantities = review.get('quantities') or {}
    if not all(isinstance(q := quantities.get(k), dict) and q.get('complete') is True and not q.get('partial') and type(q.get('value')) in (int, float) and math.isfinite(q['value']) and q['value'] >= 0 and q.get('unit') == 'parts' for k in ('inspected', 'certified', 'rejected')):
        reasons.append('Quantity completeness is not established. Unknown quantities are not zero.')
    rec = reconcile.current(item)
    if not rec or not reconcile.evidence_verified(rec) or (review.get('sharepoint') or {}).get('source_verified') is not True:
        reasons.append('A complete current exact-source comparison is required.')
    # Every checked-copy claim is bounded, including sources without Date contracts.
    # Do not silently fetch a provider when the deadline passes.
    expiry = None
    try:
        capture = datetime.fromisoformat((rec or {}).get('captured_at', '').replace('Z', '+00:00'))
        if capture.tzinfo is None or capture > now:
            raise ValueError('Invalid capture')
        expiry = capture + timedelta(seconds=MAX_AGE_SECONDS)
        contract = (rec or {}).get('date_contract')
        if contract is not None:
            expiry = min(expiry, datetime.fromisoformat(contract['expires_at'].replace('Z', '+00:00')))
        if expiry <= now:
            raise ValueError('Expired capture')
    except (ValueError, TypeError, KeyError, AttributeError, OverflowError):
        reasons.append('Source evidence is expired or its freshness is unknown. Recheck SharePoint explicitly.')
    ready = not reasons
    return dict(status='checked' if ready else 'not_verified', ready=ready,
                file_hash=item.get('file_hash'), check_hash=item.get('check_hash'), checked_at=item.get('checked_at'),
                expires_at=expiry.isoformat() if expiry else None,
                remaining_detected_errors=len(errors) if complete else None,
                set_aside_unresolved=ignored, verification_gaps=len(gaps), informational=sum(r.get('severity') == 'info' for r in rows),
                coverage_complete=complete, reasons=reasons,
                label='0 remaining detected errors' if ready else 'Verification not complete',
                scope='Saved checks and current exact-source comparison, not a guarantee that every possible workbook error is detectable.',
                download=f'/api/spreadsheets/download?id={item.get("file_hash")}&checked=1&check={item.get("check_hash")}' if ready else None)

"""Read-only regeneration / backfill planner for ONE exact source scope. OFFLINE. NOT CONNECTED.

Nothing imports this module: there is no route, schedule or operator screen that reaches it. It reads no network, holds
no credential and writes nothing. ``plan`` is a pure function over values the caller already holds:

  * the verified assignment list (which client / site / list / job / day may be reported at all),
  * one sealed, complete capture of that scope (hosting.source_first.seal), frozen at its own ``captured_at``,
  * the report versions that already exist for the scope (original, corrected, approved, sent, earlier drafts),
  * optionally the sealed capture an existing report was built from.

It answers one question: for this capture, is a draft missing, current, or out of date, and what changed? It never
decides that a report is right, complete or releasable.

What it will not do
  * It does not regenerate anything by itself and nothing calls it on a timer. A plan is a description.
  * It never overwrites, edits or supersedes an existing version. Every existing version is listed as kept unchanged.
  * It does not treat a missing quantity as zero. Totals are the sum of the known values plus a count of unknown rows.
  * It does not work out a shift from a clock time. A row whose recorded shift has no approved mapping is left out of
    the proposed draft and listed as unassigned; automated completion is then blocked, and a person decides.
  * It does not extend the 600-second date-evidence limit. The evidence must have been current at the moment the
    capture was taken; it is judged against the capture, never against a later clock.
  * It does not prove when a part was actually scanned. "Added since the earlier capture" is the only timing it states.
  * It refuses any Pratt & Whitney scope outright. That reporting is done elsewhere.

``propose`` writes a proposed draft and its checklist into a LOCAL folder the caller names, using the selected native
template (hosting.last_scan_local.NativeDraftPublisher). It writes only inside a new folder for that exact input and
refuses to write where anything already exists.
"""
from copy import deepcopy
from datetime import datetime
import json
from pathlib import Path
import re

from hosting import source_first as sf

VERSION = 'regeneration-plan-v1'
SCOPE_KEYS = ('client_id', 'site_id', 'list_id', 'job', 'business_date')
KINDS = ('original', 'corrected', 'approved', 'sent', 'draft')
DATE_EVIDENCE_SECONDS = 600
EXCLUDED = re.compile(r'pratt|whitney|p\s*&\s*w\b|\bpw\b|\bpwc\b', re.I)
NOTICES = (
    'This is a plan. Nothing was regenerated, sent, approved or changed, and nothing runs it automatically.',
    'The source is described exactly as captured. This is not a live reading and not proof of when parts were scanned.',
    'A missing quantity is unknown, not zero.',
    'Existing versions are never overwritten. A proposed draft is a new, held draft for a person to review.',
)


def _reason(code, text):
    """One stated reason. These are planner refusals and holds with their own wording, not verification findings."""
    return {'code': code, 'text': text}


class PlanError(ValueError):
    """The request itself is unusable (wrong shape). A refusal for a stated reason is a blocker in the plan instead."""


def _instant(value):
    try:
        stamp = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    except (TypeError, ValueError):
        return None
    return stamp if stamp.tzinfo is not None else None


def _exact(value):
    return isinstance(value, str) and bool(value) and value == value.strip() and len(value) <= 300 and not any(ord(c) < 32 for c in value)


def _same_scope(a, b):
    return all(a.get(k) == b.get(k) for k in SCOPE_KEYS)


def _assignment(assignments, scope, blockers):
    """The one verified assignment for this exact scope, or None with the reason added to ``blockers``."""
    def block(code, text):
        blockers.append(_reason(code, text))
    if not isinstance(scope, dict) or any(not _exact(scope.get(k)) for k in SCOPE_KEYS):
        block('scope_incomplete', 'Name the exact client, site, list, job and business date. Nothing is matched loosely.')
        return None
    if any(EXCLUDED.search(str(scope.get(k))) for k in ('client_id', 'job', 'site_id', 'list_id')) or \
            any(EXCLUDED.search(str(a.get(k) or '')) for a in assignments if isinstance(a, dict) and _same_scope(a, scope) for k in ('client_name', 'site_name', 'list_name')):
        block('excluded_scope', 'Pratt & Whitney reporting is excluded from this planner.')
        return None
    exact = [a for a in assignments if isinstance(a, dict) and _same_scope(a, scope)]
    # The same job and day reachable through another site or list, or listed twice: which one is the report is not
    # for this code to choose.
    same_job = [a for a in assignments if isinstance(a, dict) and all(a.get(k) == scope.get(k) for k in ('client_id', 'job', 'business_date'))]
    if len(exact) > 1 or len(same_job) > 1:
        block('duplicate_logical_job', f'This client, job and day is assigned {len(same_job)} times. Exactly one verified assignment is required.')
        return None
    if not exact:
        block('scope_not_assigned', 'No assignment names this exact client, site, list, job and day.')
        return None
    a = exact[0]
    if a.get('verified') is not True:
        block('assignment_not_verified', 'The site / list / job mapping for this scope is not verified.')
        return None
    if a.get('active') is False:
        block('assignment_not_active', 'This assignment is not active.')
        return None
    if not _exact(a.get('zone')):
        block('zone_missing', 'The assignment does not state the timezone of the business date.')
        return None
    return a


def _date_evidence(capture, assignment, blockers):
    """The Date-column evidence taken with this capture, judged at the capture's own instant. Never at a later one."""
    def block(code, text):
        blockers.append(_reason(code, text))
    c, scope, taken = capture.get('date_contract'), capture['scope'], _instant(capture.get('captured_at'))
    if taken is None:
        block('capture_time_missing', 'The capture does not state when it was taken, with an offset.')
        return None
    if not isinstance(c, dict):
        block('date_evidence_missing', 'No timezone and Date-column evidence was taken with this capture. Capture again; it cannot be added afterwards.')
        return None
    column = (capture.get('columns') or {}).get('Date') or {}
    since, until = _instant(c.get('captured_at')), _instant(c.get('expires_at'))
    problems = []
    if c.get('site_id') != scope['site_id'] or c.get('list_id') != scope['list_id']:
        problems.append('it names another site or list')
    if c.get('field') != 'Date' or c.get('format') != 'dateOnly' or (column.get('dateTime') or {}).get('format') != 'dateOnly':
        problems.append('the Date column is not shown to be date-only')
    if not c.get('column_id') or c.get('column_id') != column.get('id'):
        problems.append('its column identity is not the captured Date column')
    if c.get('zone') != assignment.get('zone'):
        problems.append('its timezone is not the assignment timezone')
    if since is None or until is None or not 0 < (until - since).total_seconds() <= DATE_EVIDENCE_SECONDS:
        problems.append('its validity is missing or longer than %d seconds' % DATE_EVIDENCE_SECONDS)
    elif not since <= taken < until:
        problems.append('it was not current when the capture was taken')
    if problems:
        block('date_evidence_not_current', 'The timezone and Date-column evidence cannot be used: ' + '; '.join(problems) +
              '. Capture again with fresh evidence. The limit is not extended and settings are not marked verified to pass it.')
        return None
    return dict(column_id=c['column_id'], zone=c['zone'], captured_at=c['captured_at'], expires_at=c['expires_at'],
                judged_at='capture', seconds_before_capture=int((taken - since).total_seconds()))


def _known_sum(rows, field):
    values = [r['resolved'][field] for r in rows]
    known = [v for v in values if v is not None]
    return dict(known_sum=sum(known), known_rows=len(known), unknown_rows=len(values) - len(known), complete=len(known) == len(values),
                **({} if len(known) == len(values) else {'note': f'{len(values) - len(known)} row(s) have no usable {field} value. They are unknown, not zero; the total is not complete.'}))


def _existing(existing, scope, input_hash, snapshot_hash):
    out = []
    for e in existing:
        if not isinstance(e, dict) or e.get('kind') not in KINDS or not _exact(e.get('file_hash')):
            raise PlanError('Each existing version needs a file hash and a kind: ' + ', '.join(KINDS) + '.')
        if not _same_scope(e, scope):
            continue
        bound = e.get('report_input_hash')
        out.append(dict(file_hash=e['file_hash'], kind=e['kind'], action='keep_unchanged', filename=e.get('filename'),
                        rows=e.get('rows'), inspected=e.get('inspected'), source_snapshot_hash=e.get('source_snapshot_hash'),
                        report_input_hash=bound,
                        relation=('same_input' if bound and bound == input_hash else
                                  'same_source_other_settings' if e.get('source_snapshot_hash') == snapshot_hash and bound else
                                  'earlier_or_other_input' if bound else 'not_bound_to_a_source_capture')))
    return out


def plan(assignments, scope, capture, existing=(), previous_capture=None, header=None):
    """A description of what regenerating this scope from this capture would mean. Writes nothing, calls nothing."""
    from hosting import source_first_workbook as wb
    blockers = []
    base = dict(version=VERSION, writes_nothing=True, scope={k: (scope or {}).get(k) for k in SCOPE_KEYS} if isinstance(scope, dict) else None,
                notices=list(NOTICES))
    def blocked():
        return dict(base, status='blocked', blockers=blockers, proposal=dict(allowed=False))
    if not isinstance(assignments, (list, tuple)):
        raise PlanError('The assignment list is required.')
    assignment = _assignment(assignments, scope, blockers)
    if assignment is None:
        return blocked()
    try:
        sf.verify_seal(capture)
    except Exception:
        blockers.append(_reason('capture_not_sealed', 'The capture is not a sealed, complete capture, or it was changed after sealing.'))
        return blocked()
    source_scope = dict(capture['scope'])
    if any(source_scope.get(k) != scope[k] for k in ('site_id', 'list_id', 'job', 'business_date')):
        blockers.append(_reason('capture_scope_mismatch', 'The capture is for another site, list, job or day.'))
        return blocked()
    evidence = _date_evidence(capture, assignment, blockers)
    if evidence is None:
        return blocked()
    try:
        mapping = sf.mapping(assignment.get('mapping'), capture['columns'])
    except sf.ReviewError as e:
        blockers.append(_reason('mapping_not_usable', 'The report field mapping of this assignment cannot be used: ' + str(e)))
        return blocked()
    if capture.get('uncertain_dates'):
        blockers.append(_reason('uncertain_dates', f'{len(capture["uncertain_dates"])} record(s) of this list have a business date that could not be read. Which day they belong to is unknown, so this day cannot be called covered.'))
        return blocked()
    rows, findings = sf.reviewed_rows(capture, mapping, [])
    assigned = [r for r in rows if r['resolved']['shift']]
    unassigned = [r for r in rows if not r['resolved']['shift']]
    recorded = set(assignment.get('recorded_shifts') or [])
    def raw_shift(r):
        v = r['values']['shift']
        return '(blank)' if sf.missing(v) else str(v.get('value'))
    if recorded:   # a shift this assignment was not verified for is not reported under it
        outside = [r for r in assigned if raw_shift(r) not in recorded]
        assigned = [r for r in assigned if raw_shift(r) in recorded]
        unassigned += outside
    unassigned_shifts = {}
    for r in unassigned:
        unassigned_shifts[raw_shift(r)] = unassigned_shifts.get(raw_shift(r), 0) + 1
    selected = sorted(r['id'] for r in assigned)
    view = deepcopy(capture)
    view['records'] = [r for r in capture['records'] if r['id'] in set(selected)]
    view['photos'] = [p for p in capture.get('photos', []) if p['record_id'] in set(selected)]
    view = sf.seal({k: v for k, v in view.items() if k not in ('snapshot_hash', 'envelope_hash')})
    head = wb.header(header if header is not None else (assignment.get('header') or {}), view)
    contract = wb.contract()
    binding = sf.binding(view, mapping, [], head, contract['clean_sha256'], sf.digest(contract), wb.generator_hash())
    input_hash = sf.digest(dict(binding=binding, scope={k: scope[k] for k in SCOPE_KEYS}, source_snapshot_hash=capture['snapshot_hash'],
                                source_ids=selected, unassigned_shifts=unassigned_shifts, plan_version=VERSION))
    versions = _existing(existing, scope, input_hash, capture['snapshot_hash'])
    same = [v for v in versions if v['relation'] == 'same_input']
    if previous_capture is not None:
        try:
            sf.verify_seal(previous_capture)
            if previous_capture['scope'] != capture['scope']:
                raise sf.ReviewError('other scope')
            delta = sf.changes(previous_capture, capture)
            changes = dict(basis='earlier_sealed_capture', earlier_snapshot_hash=previous_capture['snapshot_hash'], earlier_captured_at=previous_capture.get('captured_at'),
                           added=delta['added'], changed=delta['changed'], removed=delta['deleted'],
                           counts=dict(added=len(delta['added']), changed=len(delta['changed']), removed=len(delta['deleted'])),
                           meaning='Compared with the earlier capture only. "Added" means not in the earlier capture; it is not a scan time.')
        except Exception:
            blockers.append(_reason('earlier_capture_not_usable', 'The earlier capture is not sealed or is for another scope, so it cannot be compared.'))
            return blocked()
    else:
        changes = dict(basis='not_determined', meaning='No earlier sealed capture was supplied, so added, changed and removed records are not stated.')
    current_rows = len(rows)
    for v in versions:   # a row count is the only thing an unbound older report can be compared on, and it proves nothing more
        if isinstance(v.get('rows'), int) and v['rows'] != current_rows:
            v['differs'] = f'{v["rows"]} row(s) in that version, {current_rows} in this capture'
    quantities = {f: _known_sum(assigned, f) for f in sf.QUANTITIES}
    if same:
        status, reason = 'no_change', 'draft_current'
    elif not versions:
        status, reason = 'draft_proposed', 'missing_draft'
    elif all(v['relation'] == 'not_bound_to_a_source_capture' for v in versions):
        status, reason = 'draft_proposed', 'existing_report_not_bound_to_source'
    else:
        status, reason = 'draft_proposed', 'stale_draft'
    holds = []
    if unassigned:
        holds.append(_reason('unassigned_shift', f'{len(unassigned)} row(s) of this day have a recorded shift with no approved mapping ('
                          + ', '.join(f'{n} with "{s}"' for s, n in sorted(unassigned_shifts.items())) + '). They are in NO draft. '
                          'A shift is never worked out from a time. A person decides how they are reported.'))
    if any(not q['complete'] for q in quantities.values()):
        holds.append(_reason('unknown_quantities', 'Some quantities are missing at the source. They are shown as unknown; no total that needs them is complete.'))
    if not assigned:
        blockers.append(_reason('nothing_to_draft', 'No record of this day has an approved shift, so there is nothing a draft could hold.'
                             if rows else 'No source record was found for this day. An empty capture is not a completed day.'))
        return dict(blocked(), holds=holds, rows=dict(in_capture=len(rows), in_draft=0, unassigned=len(unassigned), unassigned_shifts=unassigned_shifts))
    operation = 'regen-' + input_hash[:32]
    return dict(base, status=status, reason=reason, blockers=[], holds=holds,
                automated_completion='blocked_by_unassigned_shift' if unassigned else 'not_claimed',
                day_completion='not_established', client_release='held',
                source=dict(snapshot_hash=capture['snapshot_hash'], envelope_hash=capture['envelope_hash'], captured_at=capture['captured_at'],
                            records=len(capture['records']), date_evidence=evidence, draft_view_snapshot_hash=view['snapshot_hash']),
                report_input_hash=input_hash, binding=binding, template=dict(sha256=contract['clean_sha256'], selected_sha256=contract.get('selected_sha256'), report_type=contract.get('report_type')),
                changes=changes, rows=dict(in_capture=len(rows), in_draft=len(assigned), unassigned=len(unassigned), unassigned_shifts=unassigned_shifts,
                                           source_ids=selected, unassigned_source_ids=sorted(r['id'] for r in unassigned)),
                quantities=quantities, findings=dict(total=len(findings), by_code=_count(f['code'] for f in findings)),
                existing_versions=versions, never_overwrites=True,
                proposal=dict(allowed=status == 'draft_proposed', operation_id=operation if status == 'draft_proposed' else None,
                              kind='new_held_draft', template='selected_native_daily_template',
                              note='A proposed draft is written only by propose(), only into a local folder, and only as a new operation folder.'))


def _count(values):
    out = {}
    for v in values:
        out[v] = out.get(v, 0) + 1
    return out


def propose(result, assignments, scope, capture, output, existing=(), previous_capture=None, header=None):
    """Write the proposed draft, its checklist and this plan into ``output/<operation>/``. LOCAL files only.

    The plan is worked out again from the same inputs and must be the same plan: a plan made for another capture,
    mapping, template or set of rows is refused. A folder that already exists is never written into."""
    from hosting.last_scan_local import NativeDraftPublisher
    again = plan(assignments, scope, capture, existing, previous_capture, header)
    if again.get('status') != 'draft_proposed' or not (again.get('proposal') or {}).get('allowed'):
        raise PlanError('This plan proposes no draft: ' + '; '.join(b['text'] for b in again.get('blockers') or []) if again.get('blockers') else 'This plan proposes no draft.')
    if not isinstance(result, dict) or result.get('report_input_hash') != again['report_input_hash'] or result.get('proposal', {}).get('operation_id') != again['proposal']['operation_id']:
        raise PlanError('The plan does not belong to these inputs. Plan again; nothing was written.')
    out = Path(output)
    if not out.is_absolute():
        raise PlanError('Name the local output folder by its full path.')
    assignment = next(a for a in assignments if isinstance(a, dict) and _same_scope(a, scope))
    op = again['proposal']['operation_id']
    target = out / op
    if target.exists():
        raise PlanError('A proposal for exactly these inputs was already written there. Nothing is overwritten.')
    bound = {k: scope[k] for k in ('client_id', 'site_id', 'list_id', 'job')}
    publisher = NativeDraftPublisher(out, assignment['mapping'], header if header is not None else (assignment.get('header') or {}), binding=bound)
    snapshot = dict(scope=dict(bound, business_date=scope['business_date']), source_capture=capture,
                    rows=[dict(id=i) for i in again['rows']['source_ids']], photo_blobs={}, unassigned_shifts=again['rows']['unassigned_shifts'])
    receipt = publisher(snapshot, op)
    record = dict(again, written=dict(folder=str(target), files=sorted(p.name for p in target.iterdir()) + ['regeneration-plan.json'],
                                      workbook_sha256=receipt['workbook_sha256'], checklist_sha256=receipt['checklist_sha256']))
    (target / 'regeneration-plan.json').write_bytes(json.dumps(record, indent=2, sort_keys=True, ensure_ascii=False).encode())
    return record

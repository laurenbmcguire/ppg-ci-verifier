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
  * It does not let a regenerated draft silently replace a documented correction that exists only in a report. If an
    earlier report version carries a correction (a part number typed in by a person, say) and the source is blank or
    different, the plan stops for review. It never copies the corrected value into the draft either: a value from an
    old report is a claim until a person has checked it against the exact report, source item and photo.
  * It does not make up a link to a source record. A link is kept only if the sealed capture carries it.

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
    if a.get('active') is not True:   # stated, and exactly true: a missing, empty, 0 or text value is not "active"
        block('assignment_not_active', 'This assignment is not stated to be active.')
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


LINK_KEYS = ('web_url', 'url')
CORRECTION_KEYS = {'report_file_hash', 'record_id', 'field', 'report_value', 'documented_by', 'reason', 'evidence'}
EVIDENCE_KINDS = ('report_cell', 'source_item', 'photo')
ACKNOWLEDGE = 'propose_with_source_value_and_list_conflict'


def _links(capture, ids):
    """({record id: link exactly as captured}, state). A link is whatever the sealed capture carries for the record under
    web_url or url, if it is a plain https address. Nothing is built from a site, list or item number."""
    out = {}
    for r in capture['records']:
        if r['id'] not in ids:
            continue
        for key in LINK_KEYS:
            v = r.get(key)
            if isinstance(v, str) and re.fullmatch(r'https://[^\s<>"\x00-\x1f]{1,2000}', v):
                out[r['id']] = v
                break
    state = 'not_in_capture' if not out else 'in_capture' if len(out) == len(ids) else 'partly_in_capture'
    return out, state


def _corrections(corrections, acknowledged, capture, mapping, versions, scope):
    """Documented report-only corrections, each judged against this capture. Nothing here trusts the corrected value.

    -> (records, unresolved): every correction with what the source says now, and the ones that still need a person.

    A review (acknowledgement) is of ONE conflict as it stood: this scope, field, report version and value, this source
    record at this version with this value, this capture, this evidence. All of that is in ``conflict_digest``, and an
    acknowledgement must quote it. If the source is captured again with another value, or the evidence changes, the
    digest changes and the old review no longer counts: the conflict is open again.

    The names in a correction or a review are whatever the caller wrote. This module is offline and authenticates no
    one; a name here is a statement, not proof of who reviewed. It also judges only the corrections it is given: it
    does not look for others, and an empty list is not a finding that none exist."""
    if not isinstance(corrections, (list, tuple)) or not isinstance(acknowledged, (list, tuple)):
        raise PlanError('Corrections and acknowledgements are lists.')
    acks = {}
    for a in acknowledged:
        if (not isinstance(a, dict) or set(a) != {'correction_id', 'conflict_digest', 'reviewed_by', 'decision', 'note'} or a['decision'] != ACKNOWLEDGE
                or not _exact(a.get('reviewed_by')) or not isinstance(a.get('note'), str) or len(a['note'].strip()) < 5 or not _exact(a.get('correction_id'))
                or not isinstance(a.get('conflict_digest'), str) or not re.fullmatch(r'[0-9a-f]{64}', a['conflict_digest'])):
            raise PlanError('An acknowledgement names the correction, the exact conflict it reviewed (conflict_digest), who reviewed it, a note, and the decision "%s".' % ACKNOWLEDGE)
        if a['correction_id'] in acks:
            raise PlanError('Two acknowledgements name the same correction.')
        acks[a['correction_id']] = a
    by_id = {r['id']: r for r in capture['records']}
    known_versions = {v['file_hash']: v for v in versions}
    out, seen = [], set()
    for c in corrections:
        if not isinstance(c, dict) or set(c) != CORRECTION_KEYS or c.get('field') not in sf.FIELDS:
            raise PlanError('A correction names its report version, source record, report field, the value in the report, who documented it, why, and its evidence.')
        if any(not _exact(c.get(k)) for k in ('report_file_hash', 'record_id', 'documented_by')) or not isinstance(c.get('report_value'), str) \
                or not isinstance(c.get('reason'), str) or not isinstance(c.get('evidence'), list) \
                or any(not isinstance(e, dict) or e.get('kind') not in EVIDENCE_KINDS for e in c['evidence']):
            raise PlanError('A correction is not complete, or names evidence of an unknown kind.')
        cid = 'corr-' + sf.digest(c)[:24]
        if cid in seen:
            continue
        seen.add(cid)
        record, version = by_id.get(c['record_id']), known_versions.get(c['report_file_hash'])
        photos = {p.get('sha256') for p in capture.get('photos', []) if p.get('record_id') == c['record_id'] and p.get('status') == 'supported'}
        gaps = []
        if version is None:
            gaps.append('the report version it names is not among the existing versions of this scope')
        if not any(e['kind'] == 'report_cell' and e.get('file_hash') == c['report_file_hash'] and _exact(e.get('sheet')) and _exact(e.get('cell')) for e in c['evidence']):
            gaps.append('no exact report cell is named')
        if not any(e['kind'] == 'source_item' and e.get('record_id') == c['record_id'] for e in c['evidence']):
            gaps.append('no source item is named')
        for e in c['evidence']:
            if e['kind'] == 'photo' and e.get('sha256') not in photos:
                gaps.append('a photo it names is not a captured photo of this record')
        if record is None:
            now, outcome = None, 'record_not_in_capture'
        else:
            now = sf.typed(record['raw'], mapping['fields'][c['field']])
            outcome = ('conflict_source_blank' if sf.missing(now) else
                       'source_agrees' if str(now.get('value')) == c['report_value'] else 'conflict_source_differs')
        conflict = outcome != 'source_agrees'
        status_of_evidence = 'exact_report_and_item_named' if not gaps else 'not_established'
        conflict_digest = sf.digest(dict(
            scope={k: scope[k] for k in SCOPE_KEYS}, correction_id=cid, field=c['field'], report_file_hash=c['report_file_hash'], report_value=c['report_value'],
            record_id=c['record_id'], outcome=outcome, source_value=now, source_version=record.get('version') if record else None,
            source_record_hash=sf.digest(record) if record else None, capture_snapshot_hash=capture['snapshot_hash'],
            evidence=c['evidence'], evidence_status=status_of_evidence, evidence_gaps=gaps,
            captured_photos=sorted(str(x) for x in photos), mapped_column=mapping['fields'][c['field']], plan_version=VERSION))
        given = acks.get(cid) if conflict else None
        ack = given if given and given['conflict_digest'] == conflict_digest else None
        out.append(dict(correction_id=cid, conflict_digest=conflict_digest,
                        review_no_longer_applies=bool(given and not ack),   # a review exists, but of another capture, value or evidence
                        reviewer_identity='stated_by_caller_not_authenticated',
                        report_file_hash=c['report_file_hash'], record_id=c['record_id'], field=c['field'],
                        report_value=c['report_value'], source_value_now=now, outcome=outcome, documented_by=c['documented_by'], reason=c['reason'],
                        evidence=deepcopy(c['evidence']), evidence_status=status_of_evidence,
                        evidence_gaps=gaps, value_trusted=False, carried_into_draft=False,
                        acknowledged=dict(reviewed_by=ack['reviewed_by'], note=ack['note'].strip(), decision=ack['decision'], conflict_digest=conflict_digest) if ack else None))
    unknown = sorted(set(acks) - {c['correction_id'] for c in out if c['outcome'] != 'source_agrees'})
    if unknown:
        raise PlanError('An acknowledgement names no open conflict of this plan: ' + ', '.join(unknown))
    return out, [c for c in out if c['outcome'] != 'source_agrees' and not c['acknowledged']]


def _earlier(previous, capture, blockers):
    """The changes since an earlier sealed capture of the same scope, or None with the reason added to ``blockers``.
    "Earlier" is checked, not assumed: a capture taken at the same time or later is refused."""
    try:
        sf.verify_seal(previous)
        if previous['scope'] != capture['scope']:
            raise sf.ReviewError('other scope')
    except Exception:
        blockers.append(_reason('earlier_capture_not_usable', 'The earlier capture is not sealed or is for another scope, so it cannot be compared.'))
        return None
    then, now = _instant(previous.get('captured_at')), _instant(capture.get('captured_at'))
    if then is None or now is None:
        blockers.append(_reason('earlier_capture_time_unknown', 'When the earlier capture was taken is not stated with an offset, so which capture is the earlier one is unknown.'))
        return None
    if not then < now:
        blockers.append(_reason('earlier_capture_not_earlier', 'The capture given as the earlier one was taken at the same time as, or after, this capture. Nothing is compared backwards.'))
        return None
    delta = sf.changes(previous, capture)
    return dict(basis='earlier_sealed_capture', earlier_snapshot_hash=previous['snapshot_hash'], earlier_captured_at=previous.get('captured_at'),
                seconds_between_captures=int((now - then).total_seconds()),
                added=delta['added'], changed=delta['changed'], removed=delta['deleted'],
                counts=dict(added=len(delta['added']), changed=len(delta['changed']), removed=len(delta['deleted'])),
                meaning='Compared with the earlier capture only. "Added" means not in the earlier capture; it is not a scan time.')


def _publisher(output, assignment, scope, header):
    from hosting.last_scan_local import NativeDraftPublisher
    bound = {k: scope[k] for k in ('client_id', 'site_id', 'list_id', 'job')}
    return NativeDraftPublisher(output, assignment['mapping'], header if header is not None else (assignment.get('header') or {}), binding=bound), bound


def plan(assignments, scope, capture, existing=(), previous_capture=None, header=None, corrections=(), acknowledged=()):
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
    # The writer itself is part of the input: its mapping, header, template contract, cell generator, binding, checklist
    # format and its own code. A plan made before any of that changed is a plan for another report.
    render_version = _publisher(Path('.'), assignment, scope, header)[0].render_version()
    listed = _existing(existing, scope, None, capture['snapshot_hash'])
    corrected, unresolved = _corrections(corrections, acknowledged, capture, mapping, listed, scope)
    links, link_state = _links(capture, set(selected))
    input_hash = sf.digest(dict(binding=binding, scope={k: scope[k] for k in SCOPE_KEYS}, source_snapshot_hash=capture['snapshot_hash'],
                                source_ids=selected, unassigned_shifts=unassigned_shifts, plan_version=VERSION, render_version=render_version,
                                corrections=[{k: c[k] for k in ('correction_id', 'conflict_digest', 'outcome', 'acknowledged')} for c in corrected]))
    versions = _existing(existing, scope, input_hash, capture['snapshot_hash'])
    same = [v for v in versions if v['relation'] == 'same_input']
    if previous_capture is not None:
        changes = _earlier(previous_capture, capture, blockers)
        if changes is None:
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
    conflicts = [c for c in corrected if c['outcome'] != 'source_agrees']
    if conflicts:
        holds.append(_reason('report_only_correction_conflict', f'{len(conflicts)} documented correction(s) exist only in an earlier report: the source is blank or '
                             'different for ' + ', '.join(f'record {c["record_id"]} {c["field"]}' for c in conflicts) + '. A regenerated draft shows the SOURCE value. '
                             'The corrected value is not copied in and is not treated as verified. A person must compare the exact report, source item and photo.'))
    if not assigned:
        blockers.append(_reason('nothing_to_draft', 'No record of this day has an approved shift, so there is nothing a draft could hold.'
                             if rows else 'No source record was found for this day. An empty capture is not a completed day.'))
        return dict(blocked(), holds=holds, rows=dict(in_capture=len(rows), in_draft=0, unassigned=len(unassigned), unassigned_shifts=unassigned_shifts))
    operation = 'regen-' + input_hash[:32]
    if unresolved and status == 'draft_proposed':   # nothing is proposed over an unreviewed correction
        status, reason = 'review_required', 'report_only_correction_not_reviewed'
    return dict(base, status=status, reason=reason, blockers=[], holds=holds,
                corrections=corrected, corrections_needing_review=[c['correction_id'] for c in unresolved], render_version=render_version,
                corrections_inventory='only_the_corrections_supplied_were_judged; none were looked for',
                automated_completion=('blocked_by_report_only_correction' if conflicts else 'blocked_by_unassigned_shift' if unassigned else 'not_claimed'),
                day_completion='not_established', client_release='held',
                source=dict(snapshot_hash=capture['snapshot_hash'], envelope_hash=capture['envelope_hash'], captured_at=capture['captured_at'],
                            records=len(capture['records']), date_evidence=evidence, draft_view_snapshot_hash=view['snapshot_hash'],
                            # A link to a source record is kept only if the sealed capture carries it. None is made up.
                            record_links=link_state, links=links),
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


def _photo_blobs(capture, source_ids, head, photo_bytes):
    """{sha256: base64} for exactly the photos the header selects, from local bytes the caller supplies.

    Every selected photo must be a captured, supported photo of a record in the draft, and its bytes must be supplied
    and hash to exactly that photo. Bytes for anything else are refused: nothing unbound to this capture is embedded."""
    import base64
    import hashlib
    if photo_bytes is None:
        photo_bytes = {}
    if not isinstance(photo_bytes, dict) or any(not isinstance(k, str) or not isinstance(v, (bytes, bytearray)) for k, v in photo_bytes.items()):
        raise PlanError('Photo bytes are given as {sha256: bytes}.')
    captured = {(p['record_id'], p.get('sha256')): p for p in capture.get('photos', []) if p.get('status') == 'supported' and p['record_id'] in source_ids}
    wanted = {}
    for item, sha in (head.get('photo_choices') or {}).items():
        photo = captured.get((item, sha))
        if photo is None:
            raise PlanError(f'The selected photo for record {item} is not a captured, supported photo of a record in this draft.')
        wanted[sha] = photo
    for sha, blob in photo_bytes.items():
        if sha not in wanted:
            raise PlanError('Photo bytes were supplied that no selected photo of this capture names. Nothing was written.')
        if hashlib.sha256(bytes(blob)).hexdigest() != sha or (isinstance(wanted[sha].get('bytes'), int) and wanted[sha]['bytes'] != len(blob)):
            raise PlanError('Supplied photo bytes are not the captured photo (hash or size differs). Nothing was written.')
    missing = sorted(set(wanted) - set(photo_bytes))
    if missing:
        raise PlanError(f'The bytes of {len(missing)} selected photo(s) were not supplied. Nothing was written.')
    return {sha: base64.b64encode(bytes(photo_bytes[sha])).decode() for sha in wanted}


def propose(result, assignments, scope, capture, output, existing=(), previous_capture=None, header=None, corrections=(), acknowledged=(), photo_bytes=None):
    """Write the proposed draft, its checklist and this plan into ``output/<operation>/``. LOCAL files only.

    The plan is worked out again from the same inputs and must be the same plan: a plan made for another capture,
    mapping, header, template, writer version, set of rows or set of reviewed corrections is refused. A folder that
    already exists is never written into. ``photo_bytes`` are local bytes for the photos the header selects."""
    from hosting import source_first_workbook as wb
    again = plan(assignments, scope, capture, existing, previous_capture, header, corrections, acknowledged)
    if again.get('status') != 'draft_proposed' or not (again.get('proposal') or {}).get('allowed'):
        raise PlanError('This plan proposes no draft' + (': ' + '; '.join(b['text'] for b in again['blockers']) if again.get('blockers') else
                        ': documented report-only corrections have not been reviewed.' if again.get('status') == 'review_required' else '.'))
    if not isinstance(result, dict) or result.get('report_input_hash') != again['report_input_hash'] or (result.get('proposal') or {}).get('operation_id') != again['proposal']['operation_id']:
        raise PlanError('The plan does not belong to these inputs. Plan again; nothing was written.')
    out = Path(output)
    if not out.is_absolute():
        raise PlanError('Name the local output folder by its full path.')
    assignment = next(a for a in assignments if isinstance(a, dict) and _same_scope(a, scope))
    op = again['proposal']['operation_id']
    target = out / op
    if target.exists():
        raise PlanError('A proposal for exactly these inputs was already written there. Nothing is overwritten.')
    publisher, bound = _publisher(out, assignment, scope, header)
    if publisher.render_version() != again['render_version']:
        raise PlanError('The draft writer changed since this plan was made. Plan again; nothing was written.')
    view = deepcopy(capture)
    view['records'] = [r for r in capture['records'] if r['id'] in set(again['rows']['source_ids'])]
    view['photos'] = [p for p in capture.get('photos', []) if p['record_id'] in set(again['rows']['source_ids'])]
    head = wb.header(header if header is not None else (assignment.get('header') or {}), view)
    blobs = _photo_blobs(capture, set(again['rows']['source_ids']), head, photo_bytes)
    snapshot = dict(scope=dict(bound, business_date=scope['business_date']), source_capture=capture,
                    rows=[dict(id=i) for i in again['rows']['source_ids']], photo_blobs=blobs, unassigned_shifts=again['rows']['unassigned_shifts'])
    try:
        files, receipt = publisher.build(snapshot, op)   # pure: worked out in full before anything is written
    except PlanError:
        raise
    except Exception as e:
        # The selected-template writer could not lay this day out. That is said as it is; no partial draft is left.
        raise PlanError('The draft writer could not render these source rows (%s: %s). Nothing was written.' % (type(e).__name__, str(e)[:200])) from e
    # The whole bundle is assembled first, in memory: the draft, its checklist, the receipt, this plan and, when there is
    # anything to review, the notice that says so. It is then written into a hidden staging folder and moved into place in
    # one step. A failure at any point leaves no folder a reader could take for a finished draft, and a retry is safe.
    bundle = dict(files)
    notice = _review_notice(again)
    if notice is not None:
        bundle['REVIEW-FIRST.txt'] = notice.encode()
    receipt = dict(receipt, path=str(target))
    record = dict(again, written=dict(folder=str(target), files=sorted(list(bundle) + ['receipt.json', 'regeneration-plan.json']),
                                      workbook_sha256=receipt['workbook_sha256'], checklist_sha256=receipt['checklist_sha256'],
                                      photos_embedded=sorted(blobs)))
    bundle['regeneration-plan.json'] = json.dumps(record, indent=2, sort_keys=True, ensure_ascii=False).encode()
    bundle['receipt.json'] = sf.canonical(receipt)   # last: the receipt is what marks a draft as ready
    _publish(out, target, bundle)
    return record


def _review_notice(plan_result):
    if not plan_result['holds']:
        return None
    lines = ['REVIEW FIRST. This proposed draft is held. A person decides; nothing here was approved or sent.', '']
    lines += [h['text'] for h in plan_result['holds']]
    for c in plan_result['corrections']:
        if c['outcome'] != 'source_agrees':
            now = c['source_value_now']
            lines += ['', f'Record {c["record_id"]}, {c["field"]}: an earlier report ({c["report_file_hash"][:12]}) says "{c["report_value"]}" '
                          f'(documented by {c["documented_by"]}: {c["reason"]}).',
                      '  The source now ' + ('has no such record.' if now is None else 'is blank.' if sf.missing(now) else f'says "{now.get("value")}".'),
                      '  This draft shows the source value. The earlier report is unchanged. Evidence: ' +
                      ('exact report cell and source item named' if c['evidence_status'] != 'not_established' else 'NOT established (' + '; '.join(c['evidence_gaps']) + ')') + '.',
                      ('  Reviewed by ' + c['acknowledged']['reviewed_by'] + ' (name as stated; not authenticated here): ' + c['acknowledged']['note']) if c['acknowledged'] else '  Not reviewed.']
    return '\n'.join(lines)


def _publish(out, target, bundle):
    """Write every file of the bundle into a hidden staging folder, then move the folder into place in one step."""
    import os
    import shutil
    import tempfile
    out.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix='.staging-' + target.name + '-', dir=str(out)))
    try:
        for name, body in bundle.items():
            (staging / name).write_bytes(body)
        if sorted(p.name for p in staging.iterdir()) != sorted(bundle):
            raise PlanError('The staged bundle is not complete. Nothing was published.')
        if target.exists():
            raise PlanError('A proposal for exactly these inputs was already written there. Nothing is overwritten.')
        os.rename(staging, target)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise

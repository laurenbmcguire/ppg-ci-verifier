"""A reviewer's decisions on findings: stable identity, distinct actions, an audit log, undo.

Identity
    Every finding gets an ID built from what it is about, never from its position in a list:
        source | code | sheet | cell (or row / SharePoint item + field) [ #n when the same thing is flagged twice ]
    e.g. ``check|missing_cell|Inspection Detail|L81`` and ``check|missing_cell|Inspection Detail|M81`` are two
    findings, so a decision on one can never settle the other. Filtering, grouping, sorting or re-reading the same
    file gives the same IDs. A decision is stored on one report version (its file hash) together with a fingerprint
    of what the finding said (its message and the values it quotes); it is only applied while the finding on screen
    still has that fingerprint. A newer version, a recheck that changes the message, or a different value shows the
    earlier decision as "no longer matches" instead of silently settling the new finding.

Actions
    reject   the finding is not a problem; the cell stays exactly as recorded (a blank stays blank: unknown, not zero)
    defer    an open question for someone else (e.g. the SharePoint owner); the finding stays open and the report
             stays on hold until it is answered
    confirm  the finding is a real problem that needs a correction; it stays open until the correction is applied
    reopen   back on the list for a person, whatever Claude recommended or an older record said (those are kept as
             they were, with their reasons; this is a new decision on top)
    undo     puts back the state those findings had before one earlier decision (logged; originals never change)

Every request carries a request ID: sending the same request twice records it once. Every request from the screen also
carries the fingerprint of each finding as it was shown, and the check run of that screen; a finding that changed in
between (a new SharePoint value, a re-check) refuses the request instead of attaching the decision to what it says now. Every request names the
decision revision it was made against: a stale tab gets a conflict, never a silent overwrite. A batch is all or
nothing: one unknown or stale finding refuses the whole batch.
"""
import hashlib
import json
import uuid
from datetime import datetime, timezone

ACTIONS = ('reject', 'defer', 'confirm', 'reopen', 'undo')
REQUIRE_REASON = ('reject', 'defer')
MAX_LOG = 2000
MAX_REQUESTS = 500


class DecisionError(ValueError):
    """Bad request: the reviewer has to change something."""


class DecisionConflict(DecisionError):
    """The request no longer fits the report (another tab, a newer check, an unknown finding)."""
    def __init__(self, message, **detail):
        super().__init__(message)
        self.detail = detail


def _now():
    return datetime.now(timezone.utc).isoformat()


def record_index(result):
    """(sheet, row) -> the record that row holds: its SharePoint item when it links to one, else identifier + part +
    day + shift. A finding on that row is about that record."""
    out = {}
    for r in result.get('records') or []:
        src = r.get('source') or {}
        if src.get('item'):
            key = f'item:{src.get("site") or ""}/{src.get("list") or ""}/{src["item"]}'
        elif r.get('identifier') or r.get('part'):
            key = 'row:' + '|'.join(str(r.get(k) or '') for k in ('identifier', 'part', 'day', 'shift'))
        else:
            continue
        out[(r.get('sheet'), str(r.get('row')))] = key
    return out


def _col(issue):
    return ''.join(ch for ch in str(issue.get('cell') or '') if ch.isalpha()).upper()


def _base_id(issue):
    src = {'Checks': 'check', 'SharePoint': 'rec', 'SharePoint (day totals)': 'sp', 'Usual day': 'usual', 'AI review': 'ai'}.get(issue.get('source'), 'other')
    code = issue.get('code') or ''
    rec = issue.get('record_key')
    if src == 'rec':
        where = f'item:{issue.get("item")}|{issue.get("field") or ""}' if issue.get('item') else f'{rec or issue.get("sheet") or ""}|row:{issue.get("row")}|{issue.get("field") or ""}'
        return f'rec|{code}|{where}'
    if rec:   # tied to the record, not to where it happens to sit: sorting or inserting rows cannot move a decision
        where = f'{issue.get("sheet") or ""}|{rec}|col:{_col(issue) or "row"}'
    elif issue.get('cell'):
        where = f'{issue.get("sheet") or ""}|{str(issue["cell"]).upper()}'
    elif issue.get('row') is not None:
        where = f'{issue.get("sheet") or ""}|row:{issue.get("row")}'
    else:   # about the whole report: the message is what identifies it
        where = 'text:' + hashlib.sha256(str(issue.get('text') or '').encode()).hexdigest()[:12]
    if src == 'ai':   # Claude can raise two different things about one cell
        where += '|' + hashlib.sha256(str(issue.get('text') or '').encode()).hexdigest()[:8]
    return f'{src}|{code}|{where}'


def assign_ids(issues, records=None):
    """Give every issue a stable ``key``; the old position-based key is kept as ``legacy_key`` for verdicts made on
    the same check run. ``records`` (from record_index) ties row findings to their record. Two findings about exactly
    the same thing get #2, #3 in their stable reading order."""
    records = records or {}
    seen = {}
    for x in issues:
        if x.get('row') is not None and x.get('sheet'):
            x['record_key'] = records.get((x.get('sheet'), str(x.get('row'))))
        base = _base_id(x)
        seen[base] = seen.get(base, 0) + 1
        x['legacy_key'] = x.get('key')
        x['key'] = base if seen[base] == 1 else f'{base}#{seen[base]}'
        x['fp'] = fingerprint(x)
    return issues


def fingerprint(issue):
    """What the finding says and which record it is about, so a decision is never carried onto a finding that now
    says something else or is about another record."""
    body = [issue.get('observed'), issue.get('code'), issue.get('text'), issue.get('report_value'), issue.get('source_value'), issue.get('sheet'),
            issue.get('record_key') or [str(issue.get('cell') or '').upper() or None, issue.get('row')], _col(issue) or None]
    return hashlib.sha256(json.dumps(body, default=str, sort_keys=True).encode()).hexdigest()[:16]


def _clean(text, n):
    return ' '.join(str(text or '').split())[:n]


def apply(issues, result):
    """Split issues by the decisions on this version. Returns (open, rejected, stale_decisions, counts).
    open keeps deferred and confirmed findings (with their decision attached): they are unresolved."""
    decisions = result.get('finding_decisions') or {}
    open_, rejected, stale = [], [], []
    for x in issues:
        d = decisions.get(x['key'])
        why = None
        if d and d.get('fp') != x.get('fp'):
            why = 'This decision was made on a different reading of this finding, so it is not applied. Decide again.'
        elif d and d.get('carried_from') and (not x.get('record_key') or d.get('record_key') != x.get('record_key')):
            # across versions a decision only follows a verified record (its SharePoint item), never a sheet coordinate
            why = 'This decision was made on an earlier version and this finding is not tied to the same verified record, so it is not applied. Decide again.'
        if why:
            stale.append(dict(key=x['key'], decision=d, now=x.get('text'), why=why))
            x['previous_decision'] = dict(d, note=why)
            d = None
        if d and d.get('action') == 'reject':
            rejected.append(dict(x, decision=d, verdict='not_an_issue', verdict_reason=f'Kept as recorded: {d.get("reason")}'.strip()))
            continue
        if d:
            x['decision'] = d
        open_.append(x)
    counts = dict(rejected=len(rejected), reopened=sum(1 for x in open_ if (x.get('decision') or {}).get('action') == 'reopen'),
                  deferred=sum(1 for x in open_ if (x.get('decision') or {}).get('action') == 'defer'),
                  confirmed=sum(1 for x in open_ if (x.get('decision') or {}).get('action') == 'confirm'),
                  undecided=sum(1 for x in open_ if not x.get('decision')), stale=len(stale))
    return open_, rejected, stale, counts


def decide(result, current, *, ids, action, reason='', by='', owner='', evidence=(), request_id='', expected_rev=None, undo_of=None, expected_fps=None, expected_run=None):
    """Record one decision on ``result`` (the version's saved check) in place and return the log entry.
    ``current`` maps every finding ID of this version (open or already decided) to its issue."""
    request_id = _clean(request_id, 80)
    if not request_id:
        raise DecisionError('Missing request ID.')
    normalized = dict(action=action, ids=sorted(set(_clean(k, 300) for k in ids or [] if _clean(k, 300))),
                      reason=_clean(reason, 1000), by=_clean(by, 80), owner=_clean(owner, 120),
                      evidence=[_clean(e, 500) for e in (evidence or []) if _clean(e, 500)][:20],
                      expected_rev=expected_rev, expected_run=expected_run, expected_fps=expected_fps, undo_of=undo_of)
    request_hash = hashlib.sha256(json.dumps(normalized, sort_keys=True, default=str).encode()).hexdigest()
    requests = result.get('finding_requests') or {}
    if request_id in requests:   # the same click arriving twice (double click, retry after a lost response)
        done = next((e for e in reversed(result.get('finding_decision_log') or []) if e.get('decision_id') == requests[request_id]), None)
        if not done or done.get('request_hash') != request_hash:
            raise DecisionConflict('This request ID was already used for a different or unbound decision. Reload before deciding again.')
        return dict(done, replayed=True)
    rev = int(result.get('finding_decisions_rev') or 0)
    if type(expected_rev) is not int or expected_rev != rev:
        raise DecisionConflict('Someone (or another tab) changed the decisions on this report. Reload to see them, then decide again.',
                               current_rev=rev)
    if action not in ACTIONS:
        raise DecisionError('Unknown decision.')
    reason, by, owner = _clean(reason, 1000), _clean(by, 80), _clean(owner, 120)
    evidence = [_clean(e, 500) for e in (evidence or []) if _clean(e, 500)][:20]
    decisions = dict(result.get('finding_decisions') or {})
    log = list(result.get('finding_decision_log') or [])
    if action == 'undo':
        target = next((e for e in log if e.get('decision_id') == undo_of), None)
        if not target:
            raise DecisionConflict('That decision is not on this version.', missing=[undo_of])
        if any(e.get('undo_of') == undo_of for e in log):
            raise DecisionConflict('That decision was already undone.')
        later = [e for e in log[log.index(target) + 1:] if set(e.get('ids') or []) & set(target.get('ids') or [])]
        if later:
            raise DecisionConflict('A later decision changed some of these findings. Undo that one first.', blocking=[e['decision_id'] for e in later])
        ids = list(target.get('ids') or [])
        prev = target.get('prev') or {}
        for k in ids:
            if prev.get(k):
                decisions[k] = prev[k]
            else:
                decisions.pop(k, None)
        entry = dict(decision_id='d-' + uuid.uuid4().hex[:12], request_id=request_id, action='undo', undo_of=undo_of, ids=ids,
                     cells=target.get('cells'), reason=reason or None, by=by or None, at=_now(), prev={k: decisions.get(k) for k in ids})
    else:
        ids = list(dict.fromkeys(_clean(k, 300) for k in ids or [] if _clean(k, 300)))
        if not ids:
            raise DecisionError('Choose at least one finding.')
        if len(ids) > 1000:
            raise DecisionError('Decide on at most 1000 findings at once.')
        missing = [k for k in ids if k not in current]
        if missing:
            raise DecisionConflict(f'{len(missing)} of these findings are not on this version any more (the report was checked again '
                                   'or changed). Nothing was saved. Reload and decide again.', missing=missing)
        from hosting.review_summary import check_run
        if not isinstance(expected_run, dict) or expected_run != check_run(result):
            raise DecisionConflict('The check run changed. Reload and decide again.')
        if not isinstance(expected_fps, dict):
            raise DecisionConflict('The displayed finding fingerprints are required. Reload and decide again.')
        stale = [k for k in ids if not expected_fps.get(k) or expected_fps[k] != current[k].get('fp')]
        if stale:
            raise DecisionConflict('Some findings changed. Nothing was saved. Reload and decide again.', stale=stale)
        if action in REQUIRE_REASON and not reason:
            raise DecisionError('Say why (the reason is kept with the decision).' if action == 'reject' else 'Say what is being waited for and why.')
        if action == 'reject':
            high = [k for k in ids if current[k].get('severity') == 'high']
            if high and not evidence:
                raise DecisionError(f'{len(high)} of these {"is a blocker" if len(high) == 1 else "are blockers"}. A blocker can only be kept as '
                                    'recorded with evidence (what you checked, e.g. the SharePoint item or photo). Or defer it to the source owner.')
        at, did = _now(), 'd-' + uuid.uuid4().hex[:12]
        prev = {k: decisions.get(k) for k in ids}
        cells = {k: ' '.join(p for p in (current[k].get('sheet'), str(current[k].get('cell') or (f'row {current[k]["row"]}' if current[k].get('row') else '')) or None) if p) or 'Whole report' for k in ids}
        for k in ids:
            x = current[k]
            decisions[k] = dict(action=action, reason=reason, owner=owner or None, evidence=evidence, by=by or None, at=at, decision_id=did,
                                fp=x.get('fp'), record_key=x.get('record_key'), file_hash=result.get('file_hash'), sheet=x.get('sheet'), cell=x.get('cell'), row=x.get('row'),
                                value=x.get('report_value'), source_value=x.get('source_value'))
        entry = dict(decision_id=did, request_id=request_id, action=action, ids=ids, cells=cells, reason=reason, owner=owner or None,
                     evidence=evidence, by=by or None, at=at, prev=prev, file_hash=result.get('file_hash'))
    entry['request_hash'] = request_hash
    log.append(entry)
    result['finding_decisions'] = decisions
    result['finding_decision_log'] = log[-MAX_LOG:]
    result['finding_decisions_rev'] = rev + 1
    requests = dict(requests); requests[request_id] = entry['decision_id']
    result['finding_requests'] = dict(list(requests.items())[-MAX_REQUESTS:])
    return entry


def legacy_fine_matches(accepted, issues):
    """Older "fine as is" records saved only code, sheet, row and message, not the cell. On a row with two findings
    that say the same thing (a blank rework cell and a blank NOK cell are both "Missing cell.") such a record cannot
    say which one the reviewer meant, so it settles neither: both stay open with a note. Returns {issue key: record}
    for unambiguous matches and {issue key: note} for ambiguous ones."""
    hits, ambiguous = {}, {}
    for a in accepted:
        if not a.get('by_person'):
            continue
        cand = [x for x in issues if x.get('code') == a.get('code') and x.get('text') == a.get('text')
                and (a.get('row') is None or (x.get('sheet') == a.get('sheet') and str(x.get('row')) == str(a.get('row'))))]
        if a.get('cell'):
            cand = [x for x in cand if str(x.get('cell') or '').upper() == str(a['cell']).upper()]
        elif a.get('key'):
            cand = [x for x in cand if x['key'] == a['key']] or cand
        if not a.get('fp'):
            for x in cand:
                ambiguous.setdefault(x['key'], 'An earlier "fine as is" did not bind the exact finding values and check run, so it is not applied. Decide again.')
            continue
        cand = [x for x in cand if x.get('fp') == a['fp']]
        if len(cand) == 1 or (a.get('row') is None and cand and not a.get('cell')):
            for x in cand:
                hits.setdefault(x['key'], a)
        elif len(cand) > 1:
            for x in cand:
                ambiguous.setdefault(x['key'], 'An earlier "fine as is" on this row did not record which cell it meant '
                                     f'({", ".join(str(c.get("cell") or "?") for c in cand)} all match), so it is not applied. Decide again.')
    return hits, ambiguous

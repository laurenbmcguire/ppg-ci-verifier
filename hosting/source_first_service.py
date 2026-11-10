"""Durable source-review operations using the app's existing auth/inbox/store.

Every returned success is read back through a new authoritative store session.
Pending capture receipts are never silently retried after an uncertain response.
"""
from copy import deepcopy
from contextlib import contextmanager
import json
import re
from hosting import source_first as sf
from hosting.files import files_for
from hosting.spreadsheet_inbox import SpreadsheetInbox

PREFIX = 'source_first|'


def request_id(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_-]{16,100}', value):
        raise sf.ReviewError('A stable request ID is required. Reload its receipt before retrying an uncertain operation.')
    return value


def _get(db, key):
    row = db.execute('SELECT value_json FROM settings WHERE key=?', (PREFIX + key,)).fetchone()
    return json.loads(row[0]) if row else None


def _put(db, key, value):
    db.execute('INSERT INTO settings(key,value_json) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json',
               (PREFIX + key, sf.canonical(value).decode()))


def _principal(h, inbox):
    h.refresh_session_principal(inbox)
    return sf.actor(h.current_user())


@contextmanager
def outer_commit_only(inbox):
    """Keep native inbox savepoints inside the enclosing store transaction.

    The retained PgSession normally commits each depth-zero inbox.connect().
    Source-first success instead belongs to PostgresWorkspace.session's final
    commit, including private Files writes and its durable operation receipt.
    This request-local depth guard makes inbox contexts nested savepoints; the
    unchanged outer store alone commits or rolls back. No global PG behavior
    or connection settings are changed.
    """
    pg = inbox.pg
    previous = pg.depth if pg is not None else None
    if pg is not None:
        pg.depth = previous + 1
    try:
        yield
    finally:
        if pg is not None:
            pg.depth = previous


@contextmanager
def transaction(h, key):
    with h.store().session() as directory:
        inbox = SpreadsheetInbox(directory)
        with outer_commit_only(inbox):
            actor = _principal(h, inbox)
            with inbox.connect() as db:
                inbox._begin(db, 'source-first:' + key)
                yield directory, inbox, db, actor


def load_capture(directory, state):
    raw = files_for(directory).get(state['snapshot_path'])
    if sf.digest(raw) != state['snapshot_file_hash']:
        raise sf.ReviewError('Stored snapshot bytes differ from the sealed capture.')
    capture = json.loads(raw)
    sf.verify_seal(capture)
    return capture


def save_capture(directory, capture, blobs):
    sf.verify_seal(capture)
    raw = sf.canonical(capture)
    sha = sf.digest(raw)
    path = 'uploads/source-first-' + sha + '.json'
    files_for(directory).put(path, raw)
    for photo_hash, blob in blobs.items():
        if sf.digest(blob) != photo_hash:
            raise sf.ReviewError('Photo bytes do not match their provenance hash.')
        files_for(directory).put('uploads/source-first-photo-' + photo_hash + '.bin', blob)
    return dict(snapshot_path=path, snapshot_file_hash=sha, snapshot_hash=capture['snapshot_hash'])


def _hold_key(scope):
    return 'source_first_hold|' + sf.digest({k: scope[k] for k in ('site_id','list_id')})


def _check_hold(db, scope):
    from datetime import datetime, timezone
    row = db.execute('SELECT value_json FROM settings WHERE key=?', (_hold_key(scope),)).fetchone()
    if not row: return
    hold = json.loads(row[0])
    due = hold.get('not_before')
    if not due or datetime.now(timezone.utc) < datetime.fromisoformat(due):
        raise sf.ReviewError('This exact source has a provider cooldown hold. ' + ('Do not read it before ' + due + '.' if due else 'A verified provider recovery is required; no retry deadline was supplied.'))


def _failure(directory, db, state, exc):
    evidence = getattr(exc, 'evidence', None)
    if evidence is None: return
    raw = sf.canonical(evidence); sha = sf.digest(raw)
    path = 'uploads/source-first-failed-' + sha + '.json'
    files_for(directory).put(path, raw)
    state['failure_receipt'] = dict(sha256=sha, status=evidence.get('status'),
                                    remaining_cursor_hash=sf.digest(evidence.get('remaining_cursor')),
                                    cooldown=evidence.get('cooldown'), private_evidence_path=path)
    hold = evidence.get('cooldown')
    if hold:
        old = db.execute('SELECT value_json FROM settings WHERE key=?', (_hold_key(state['scope']),)).fetchone()
        previous = json.loads(old[0]) if old else None
        if previous and (not previous.get('not_before') or previous.get('not_before') > (hold.get('not_before') or '')):
            hold = previous
        db.execute('INSERT INTO settings(key,value_json) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json',
                   (_hold_key(state['scope']),sf.canonical(hold).decode()))


def registered_assignment(inbox, scope):
    from hosting import registry
    matches = []
    for entry in source_choices(inbox):
        if entry.get('list_id') != scope['list_id'] or not registry._same_job(entry.get('job'),scope['job']): continue
        if entry['site_id'] == scope['site_id']:
            matches.append({k:entry.get(k) for k in ('client_key','client','site','source')})
    unique = {sf.digest(m):m for m in matches}
    if len(unique) != 1 or not next(iter(unique.values())).get('client_key'):
        raise sf.ReviewError('The exact source does not have one verified client assignment.')
    return dict(next(iter(unique.values())), **scope)


def source_choices(inbox):
    """Already verified registry identities only. No discovery or provider read."""
    from hosting import registry
    choices = {}
    cached = inbox.setting(registry.SNAPSHOT_KEY, []) or []
    for entry in registry.entries(inbox.setting(registry.REGISTRY_KEY, [])):
        entry = dict(entry)
        if not entry.get('site_id') and entry.get('verified'):
            ids = {j['site_id'] for c in cached if c.get('key') == entry.get('client_key')
                   for j in c.get('jobs') or [] if j.get('site') == entry.get('site') and
                   j.get('list_id') == entry.get('list_id') and j.get('site_id')}
            if len(ids) == 1:
                entry['site_id'] = ids.pop()
        if not all(entry.get(k) for k in ('verified', 'client_key', 'job', 'site_id', 'list_id')):
            continue
        choice = {k: entry.get(k) for k in ('client_key', 'client', 'job', 'site', 'site_id', 'list_id', 'source')}
        choices[sf.digest(choice)] = choice
    return sorted(choices.values(), key=lambda c: (str(c['client']), str(c['job']), c['site_id'], c['list_id']))


def _template():
    from hosting import source_first_workbook as wb
    return wb, wb.contract()


def review(directory, state):
    capture = load_capture(directory, state)
    wb, contract = _template()
    if state.get('mapping') is None:
        return capture, [], [dict(code='mapping_required', text='Review and save the exact source field mapping.', blocking=True)], None
    rows, findings = sf.reviewed_rows(capture, state['mapping'], state['overlays'])
    findings.extend(wb.scope_findings(rows, state.get('header') or {}, capture, contract))
    bind = sf.binding(capture, state['mapping'], state['overlays'], state.get('header') or {},
                      contract['clean_sha256'], sf.digest(contract), wb.generator_hash())
    bind['assignment_hash'] = sf.digest(state.get('assignment'))
    return capture, rows, findings, bind


def view(h, key=None):
    with h.store().session() as directory:
        inbox = SpreadsheetInbox(directory)
        _principal(h, inbox)
        if key is None:
            with inbox.connect() as db:
                saved = db.execute('SELECT value_json FROM settings WHERE key LIKE ?', (PREFIX + '%',)).fetchall()
            return dict(sessions=[{k: s.get(k) for k in ('id', 'scope', 'status', 'created_at', 'updated_at')}
                                  for row in saved if (s := json.loads(row[0]))], sources=source_choices(inbox), release='held')
        with inbox.connect() as db:
            state = _get(db, key)
        if not state:
            raise sf.ReviewError('Source review not found.')
        return bounded_view(detail(directory, state))


def require_visible_review(directory, state):
    if len(json.dumps(detail(directory, state), allow_nan=False).encode()) > sf.MAX_RESPONSE_BYTES:
        raise sf.ReviewError('The complete review exceeds the private pilot display limit. Approval and generation remain blocked; source records were not truncated.')


def bounded_view(out):
    if len(json.dumps(out, allow_nan=False).encode()) <= sf.MAX_RESPONSE_BYTES:
        return out
    # A successful outer save remains successful even if its complete review is
    # too large to display. Return a small authoritative held receipt, not rows.
    return dict(id=out['id'], scope=out['scope'], status=out['status'],
                created_at=out['created_at'], updated_at=out['updated_at'],
                view_limited=True, release='held', approved=False,
                current_sharepoint=False, snapshot_hash=out.get('snapshot_hash'),
                error='The saved review exceeds the private pilot display limit. Source records are retained unchanged. Approval and generation remain blocked until the complete review can be displayed.',
                receipt=dict(last_operation=(out.get('operations') or [None])[-1],
                             report_hash=(out.get('report') or {}).get('file_hash')))


def detail(directory, state):
    """Complete review representation, reused by display and approval gates."""
    out = {k: deepcopy(state.get(k)) for k in ('id', 'scope', 'status', 'created_at', 'updated_at', 'mapping', 'header', 'approval', 'report', 'operations', 'changes', 'error', 'assignment')}
    if out.get('report'):
        out['report'].pop('artifact_path', None)
    out['release'] = 'held'
    out['current_sharepoint'] = False
    if state.get('failure_receipt'):
        out['failure_receipt']={k:v for k,v in state['failure_receipt'].items() if k!='private_evidence_path'}
    if state.get('snapshot_path'):
        capture, rows, findings, bind = review(directory, state)
        column_values = {}
        for name in capture['columns']:
            values = {sf.canonical(sf.typed(row['raw'], name)): sf.typed(row['raw'], name) for row in capture['records']}
            column_values[name] = list(values.values())[:50]
        out.update(snapshot_hash=capture['snapshot_hash'], captured_at=capture['captured_at'],
                   snapshot_envelope_hash=capture['envelope_hash'], columns=capture['columns'], rows=rows,
                   findings=findings, binding=bind, binding_hash=sf.digest(bind) if bind else None,
                   overlays=state['overlays'], coverage=capture['coverage'],
                   photos=capture['photos'], column_values=column_values, approved=bool(bind and state.get('approval', {}).get('binding_hash') == sf.digest(bind)))
        out['current_sharepoint'] = sf.fresh(state.get('freshness'), capture['snapshot_hash'])
        if out.get('report'):
            out['report']['review_binding_current'] = out['report']['binding_hash'] == (sf.digest(bind) if bind else None)
            from datetime import datetime
            try:
                final_comparison = (datetime.fromisoformat(state['freshness']['started_at']) >= datetime.fromisoformat(out['report']['generated_at']) and
                                    state['freshness'].get('report_hash_at_start') == out['report']['file_hash'])
            except (KeyError, TypeError, ValueError):
                final_comparison = False
            out['report']['current_sharepoint'] = bool(out['current_sharepoint'] and final_comparison and out['approved'] and out['report']['review_binding_current'])
        out['freshness'] = state.get('freshness')
        out['verification_state'] = ('matches_current_source_with_reviewed_overlays' if state['overlays'] else 'matches_current_sharepoint') if (out.get('report') or {}).get('current_sharepoint') else 'matches_reviewed_snapshot' if state.get('report') and state['report'].get('binding_hash') == (sf.digest(bind) if bind else None) else 'source_review_pending'
        # Raw all-source records are deliberately never returned by this view.
    return out



def capture(h, payload, *, capture_fn=None):
    from hosting import source_first_capture, photo_embed
    scope = sf.scope(payload.get('scope'))
    req = request_id(payload.get('request_id'))
    # The request ID itself has one scope; another job/date cannot reuse it.
    key = sf.digest({'capture_request_id': req})
    prepared = False
    with transaction(h, key) as (directory, inbox, db, actor):
        old = _get(db, key)
        if old:
            if old['scope'] != scope or old['created_by']['id'] != actor['id']:
                raise sf.ReviewError('This request ID already belongs to another capture.')
        else:
            _check_hold(db, scope)
            # Persisted verified identities only. No provider/name resolution may
            # happen before the pending receipt commits in the outer store.
            assignment = registered_assignment(inbox, scope)
            photo_embed.use_stored_certificate(inbox.setting(photo_embed.CERT_KEY, None))
            state = dict(id=key, scope=scope, status='capture_pending', created_by=actor, created_at=sf.now(), assignment=assignment,
                         updated_at=sf.now(), mapping=None, header={}, overlays=[], operations=[], approval={}, report=None)
            _put(db, key, state)
            prepared = True
    if not prepared:
        return view(h, key)
    try:
        snapshot, blobs = (capture_fn or source_first_capture.capture)(scope)
        with transaction(h, key) as (directory, inbox, db, actor):
            state = _get(db, key)
            if state['status'] != 'capture_pending':
                raise sf.ReviewError('Capture receipt was already settled.')
            state.update(save_capture(directory, snapshot, blobs), status='source_review', updated_at=sf.now(),
                         freshness=dict(snapshot_hash=snapshot['snapshot_hash'], compared_at=snapshot['captured_at'], match=True))
            _put(db, key, state)
    except sf.ReviewError as exc:
        with transaction(h, key) as (directory, _, db, _):
            state = _get(db, key)
            _failure(directory, db, state, exc)
            if state['status'] == 'capture_pending':
                state.update(status='capture_failed', error=str(exc), updated_at=sf.now())
                _put(db, key, state)
    return view(h, key)


def mutate(h, key, action, payload):
    req = request_id(payload.get('request_id'))
    body_hash = sf.digest(dict(action=action, payload=payload))
    with transaction(h, key) as (directory, inbox, db, actor):
        state = _get(db, key)
        if not state or not state.get('snapshot_path'):
            raise sf.ReviewError('A sealed source capture is required.')
        prior = next((o for o in state['operations'] if o['request_id'] == req), None)
        if prior:
            if prior['body_hash'] != body_hash:
                raise sf.ReviewError('Request ID was reused for a different decision.')
        else:
            cap, rows, findings, bind = review(directory, state)
            if payload.get('snapshot_hash') != cap['snapshot_hash']:
                raise sf.ReviewError('The displayed source snapshot changed. Reload and review it again.')
            if payload.get('binding_hash') != (sf.digest(bind) if bind else None):
                raise sf.ReviewError('The displayed mapping or corrections changed. Reload before saving.')
            if len(state['operations']) >= 1000:
                raise sf.ReviewError('This review reached its bounded operation limit. Start a fresh capture; receipts were not removed.')
            state.setdefault('decision_history', []).append(dict(mapping=state['mapping'], header=state['header'], overlays=deepcopy(state['overlays']), approval=state['approval'], actor=actor, at=sf.now()))
            if action == 'mapping':
                state['mapping'] = sf.mapping(payload.get('mapping'), cap['columns'])
                state['overlays'] = []
                state['approval'] = {}
            elif action == 'header':
                wb, contract = _template()
                state['header'] = wb.header(payload.get('header'), cap)
                state['header'].update(reviewed_by=actor, reviewed_at=sf.now())
                state['approval'] = {}
            elif action == 'correct':
                if not state.get('mapping'):
                    raise sf.ReviewError('Save the field mapping before correcting values.')
                record_id, field = payload.get('record_id'), payload.get('field')
                row = next((r for r in cap['records'] if r['id'] == record_id), None)
                overlay = sf.proposal(cap, record_id, field, payload.get('replacement'), payload.get('evidence'), h.current_user())
                overlay['original'] = sf.typed(row['raw'], state['mapping']['fields'][field])
                overlay['mapping_hash'] = sf.digest(state['mapping'])
                state['overlays'].append(overlay)
                state['approval'] = {}
            elif action == 'approve':
                require_visible_review(directory, state)
                if any(f['blocking'] for f in findings):
                    raise sf.ReviewError('Unresolved source or template blockers remain. Acknowledgment does not clear unknown values.')
                if payload.get('reviewed') is not True:
                    raise sf.ReviewError('Confirm review of this exact source, corrections, mapping, and template.')
                state['approval'] = dict(binding_hash=sf.digest(bind), binding=bind, actor=actor, at=sf.now(),
                                         meaning='Content approval for the sealed snapshot only; release held.')
            else:
                raise sf.ReviewError('Unknown source-review decision.')
            state['operations'].append(dict(request_id=req, body_hash=body_hash, action=action, status='saved', actor=actor, at=sf.now()))
            state['updated_at'] = sf.now()
            if action == 'approve':
                require_visible_review(directory, state)
            _put(db, key, state)
    return view(h, key)


def refresh(h, key, payload, *, capture_fn=None):
    from hosting import source_first_capture, photo_embed
    req = request_id(payload.get('request_id'))
    request_hash = sf.digest(payload)
    started = False
    with transaction(h, key) as (directory, inbox, db, actor):
        state = _get(db, key)
        if not state or not state.get('snapshot_path'):
            raise sf.ReviewError('A sealed snapshot is required.')
        prior = next((o for o in state['operations'] if o['request_id'] == req), None)
        if prior:
            if prior['body_hash'] != request_hash:
                raise sf.ReviewError('Refresh request ID already has another meaning.')
        else:
            if payload.get('snapshot_hash') != state['snapshot_hash']:
                raise sf.ReviewError('Displayed source snapshot changed.')
            _check_hold(db, state['scope'])
            started_at = sf.now()
            report_hash_at_start = (state.get('report') or {}).get('file_hash')
            state['operations'].append(dict(request_id=req, body_hash=request_hash, action='refresh', status='pending', actor=actor, at=started_at,
                                            started_at=started_at, report_hash_at_start=report_hash_at_start))
            state['freshness'] = None
            _put(db, key, state)
            photo_embed.use_stored_certificate(inbox.setting(photo_embed.CERT_KEY, None))
            selected = state['scope']
            started = True
    if not started:
        return view(h, key)
    try:
        new, blobs = (capture_fn or source_first_capture.capture)(selected)
        with transaction(h, key) as (directory, inbox, db, actor):
            state = _get(db, key)
            old = load_capture(directory, state)
            if old['snapshot_hash'] != payload['snapshot_hash']:
                raise sf.ReviewError('Another source comparison changed this review. Reload it.')
            if new['snapshot_hash'] != old['snapshot_hash']:
                state['changes'] = sf.changes(old, new)
                state.setdefault('history', []).append(dict(snapshot_path=state['snapshot_path'], approval=state['approval'], overlays=state['overlays'], header=state['header'], report=state['report']))
                state.update(save_capture(directory, new, blobs), status='source_review', overlays=[], approval={}, header={})
                state['report'] = None
            comparison = save_capture(directory, new, blobs)
            # Same content renews freshness without erasing approval/overlays.
            state['freshness'] = dict(snapshot_hash=new['snapshot_hash'], compared_at=new['captured_at'], match=True,
                                      started_at=started_at, report_hash_at_start=report_hash_at_start,
                                      capture_envelope_hash=new['envelope_hash'], comparison_path=comparison['snapshot_path'])
            op = next(o for o in state['operations'] if o['request_id'] == req)
            op.update(status='saved', at=sf.now())
            state['updated_at'] = sf.now()
            _put(db, key, state)
    except sf.ReviewError as exc:
        with transaction(h, key) as (directory, _, db, _):
            state = _get(db, key)
            _failure(directory, db, state, exc)
            op = next(o for o in state['operations'] if o['request_id'] == req)
            op.update(status='failed', error=str(exc))
            _put(db, key, state)
    return view(h, key)


def generate(h, key, payload):
    req = request_id(payload.get('request_id'))
    request_hash = sf.digest(payload)
    with transaction(h, key) as (directory, inbox, db, actor):
        state = _get(db, key)
        if not state:
            raise sf.ReviewError('Source review not found.')
        prior = next((o for o in state['operations'] if o['request_id'] == req), None)
        if prior:
            if prior['body_hash'] != request_hash:
                raise sf.ReviewError('Generation request ID was reused.')
        else:
            require_visible_review(directory, state)
            cap, rows, findings, bind = review(directory, state)
            if payload.get('binding_hash') != sf.digest(bind) or state.get('approval', {}).get('binding_hash') != sf.digest(bind):
                raise sf.ReviewError('Approve the exact reviewed source and template before generating.')
            if any(f['blocking'] for f in findings):
                raise sf.ReviewError('Source or template blockers remain.')
            if registered_assignment(inbox, cap['scope']) != state['assignment']:
                raise sf.ReviewError('Verified client/source assignment changed. Create and review a new source capture.')
            wb, contract = _template()
            blobs = {p['sha256']: files_for(directory).get('uploads/source-first-photo-' + p['sha256'] + '.bin')
                     for p in cap['photos'] if p.get('status') == 'supported'}
            raw, verification = wb.build(rows, state['header'], cap, bind, blobs)
            if len(raw) > sf.MAX_RESPONSE_BYTES:
                raise sf.ReviewError('Generated report exceeds the private pilot download limit. No new report artifact was saved. Source records were not truncated.')
            if not verification.get('matches_reviewed_snapshot'):
                raise sf.ReviewError('Saved workbook did not pass exact-snapshot verification.')
            filename = re.sub(r'[^A-Za-z0-9_. -]', '_', cap['scope']['job']) + ' ' + cap['scope']['business_date'] + ' DAILY HELD.xlsx'
            file_hash = sf.digest(raw)
            path = 'uploads/source-first-report-' + file_hash + '.xlsx'
            # Private held artifact, not an ordinary inbox upload. Assignment is
            # provenance, never permission to share with client/member accounts.
            state['report'] = dict(file_hash=file_hash, filename=filename, verification=verification,
                                   binding_hash=sf.digest(bind), generated_at=sf.now(), release='held',
                                   artifact_path=path, assignment=deepcopy(state['assignment']))
            state['status'] = 'report_held'
            state['operations'].append(dict(request_id=req, body_hash=request_hash, action='generate', status='saved', actor=actor,
                                            file_hash=file_hash, at=sf.now()))
            state['updated_at'] = sf.now()
            require_visible_review(directory, state)
            files_for(directory).put(path, raw)
            _put(db, key, state)
    # Outer store commit must finish first, even if inner inbox work was accepted.
    result = view(h, key)
    if result.get('report'):
        with h.store().session() as directory:
            inbox = SpreadsheetInbox(directory)
            _principal(h, inbox)
            with inbox.connect() as db:
                persisted = _get(db, key)['report']
            blob = files_for(directory).get(persisted['artifact_path'])
            if sf.digest(blob) != result['report']['file_hash']:
                raise sf.ReviewError('Authoritative report readback did not match the saved receipt.')
    return result

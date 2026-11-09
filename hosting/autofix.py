"""Fix everything that can be fixed safely, then leave only what a person must decide.

1. Every source-backed or mechanical correction (SharePoint value for the same item, spacing, number stored as text,
   a detail the app already knows) is applied.
2. Claude then resolves the rest: it changes cells, takes out or moves rows, or confirms a value is right.
   A must-fix issue is never "confirmed fine" by Claude, and a value that SharePoint has is only ever changed to
   SharePoint's value; anything else is left for a person.
3. Everything goes into a new version (the version before is kept unchanged) with one summary of what was done.
   Each change can be reverted on its own (that makes the next version), and so can each "confirmed fine".

Nothing is approved or sent.
"""
from datetime import datetime, timezone


class AutoFixError(ValueError):
    pass


def _now():
    return datetime.now(timezone.utc).isoformat()


def eligible(item):
    """Why this version must not be auto-fixed, or None."""
    if item.get('approval'):
        return 'It is approved; approved versions are left exactly as they are.'
    if any(x.get('sent') for x in item.get('sent') or []):
        return 'It was sent to the client.'
    if item.get('auto_fix_tried') or item.get('auto_fixed'):
        return 'It has already been fixed automatically.'
    if (item.get('fixes_applied') or []):
        return 'A newer version of it exists.'
    if any(str(c.get('source') or '').startswith('Reverted') for c in (item.get('fixed_from') or {}).get('changes') or []):
        return 'A person reverted Claude\'s fixes on this version.'
    return None


KEEP = ('id', 'sheet', 'cell', 'before', 'after', 'reason', 'source', 'kind', 'snapshot_id', 'captured_at', 'item', 'field', 'change_set', 'change_set_size')


def _held_cells(item):
    from hosting.spreadsheet_inbox import SpreadsheetInbox
    return SpreadsheetInbox.held_cells(item)


BATCH = 20        # issues per Claude call: each request stays well inside the function time limit
MAX_ROUNDS = 15


def run(h, file_hash):
    """The whole pass in one go (tests, scripts). The app calls step() phase by phase so no request runs long."""
    out = step(h, file_hash, 'sure')
    while out['next'] == 'ai':
        out = step(h, out['item']['file_hash'], 'ai')
    out = step(h, out['item']['file_hash'], 'finish')
    return out['item'], out['summary']


def _remaining(inbox, file_hash, summary):
    _, _, issues = inbox.ai_fix_inputs(file_hash)
    skip = set(summary.get('left_keys') or [])
    return [i for i in issues if i['key'] not in skip]


def step(h, file_hash, phase):
    """One short request of the automatic pass.
    sure   - apply every source-backed / mechanical fix (one new version); start the summary
    ai     - Claude resolves the next batch of what is left (one new version when it changes something)
    finish - check the final version against SharePoint again and close the summary"""
    raise AutoFixError('Automatic workbook changes are disabled pending atomic review safeguards. Review and apply cell corrections instead.')
    from api.index import ai_review_configured
    from hosting import ai_fix
    from hosting.public_api import _after_change
    from hosting.spreadsheet_inbox import SpreadsheetInbox
    if phase == 'sure':
        with h.store().session() as directory:
            inbox = SpreadsheetInbox(directory)
            item = inbox.get(file_hash)
            why_not = eligible(item)
            if why_not:
                raise AutoFixError(why_not)
            props = inbox.fix_proposals(file_hash)['proposals']
            held = _held_cells(item)
            sure = [p for p in props if p.get('sure') and (p.get('sheet'), str(p.get('cell') or '').upper()) not in held]
            summary = dict(made_at=_now(), from_hash=file_hash, status='running', rounds=0, changed=[], removed=[], moved=[], fine=[], left=[], left_keys=[], notes=[])
            current = file_hash
            if sure:
                current = inbox.apply_fixes(file_hash, [dict(p) for p in sure], expected_check_hash=item.get('check_hash'), automatic=True)['file_hash']
                summary['changed'] = [dict(sheet=p['sheet'], cell=p['cell'], before=p['before'], after=p['after'], reason=p.get('only') or p.get('reason'),
                                           source=p.get('source'), by='app') for p in sure]
            if not ai_review_configured():
                summary['notes'].append('Claude is not connected here, so only the source-backed and mechanical fixes were made.')
            inbox._update(file_hash, lambda r: r.update(auto_fix_tried=dict(at=_now(), to=current)))
            inbox._update(current, lambda r: r.update(auto_fixed=summary, auto_fix_tried=dict(at=_now(), to=current)))
            more = False  # AI write plans are disabled until they can be scoped and committed atomically.
            return dict(item=inbox.get(current), summary=summary, next='ai' if more else 'finish')
    with h.store().session() as directory:
        inbox = SpreadsheetInbox(directory)
        item = inbox.get(file_hash)
        summary = dict(item.get('auto_fixed') or {})
        if summary.get('status') != 'running':
            raise AutoFixError('No automatic pass is running on this version.')
        todo = _remaining(inbox, file_hash, summary) if phase == 'ai' else []
    if phase == 'finish':
        _after_change(h, file_hash)   # one SharePoint check and baseline on the final version
        summary['status'] = 'done'; summary['finished_at'] = _now()
        with h.store().session() as directory:
            inbox = SpreadsheetInbox(directory)
            inbox._update(file_hash, lambda r: r.update(auto_fixed=summary))
            return dict(item=inbox.get(file_hash), summary=summary, next=None)
    if phase != 'ai':
        raise AutoFixError('Unknown step.')
    if not todo or summary.get('rounds', 0) >= MAX_ROUNDS:
        return dict(item=item, summary=summary, next='finish')
    batch = todo[:BATCH]
    try:
        fixed, ai = ai_fix.run(h, file_hash, [i['key'] for i in batch], auto=True, after=False, merge=False)
    except ai_fix.AIFixError as exc:
        summary['notes'].append(f'Claude could not finish one batch: {exc}')
        summary['left_keys'] = (summary.get('left_keys') or []) + [i['key'] for i in batch]
        summary['left'] = (summary.get('left') or []) + [dict(text=i.get('text'), code=i.get('code'), sheet=i.get('sheet'), row=i.get('row'), cell=i.get('cell'), why='Claude could not finish this batch') for i in batch]
        fixed, ai = item, {}
    current = fixed['file_hash']
    summary['rounds'] = summary.get('rounds', 0) + 1
    summary['changed'] = (summary.get('changed') or []) + [dict(c, source='Claude', by='Claude') for c in ai.get('changed') or []]
    for k in ('removed', 'moved', 'fine'):
        summary[k] = (summary.get(k) or []) + (ai.get(k) or [])
    left_now = ai.get('left') or []
    left_texts = {(x.get('text'), str(x.get('row'))) for x in left_now}
    summary['left'] = (summary.get('left') or []) + left_now
    summary['left_keys'] = (summary.get('left_keys') or []) + [i['key'] for i in batch if (i.get('text'), str(i.get('row'))) in left_texts]
    progress = bool(ai.get('changed') or ai.get('removed') or ai.get('moved') or ai.get('fine'))
    with h.store().session() as directory:
        inbox = SpreadsheetInbox(directory)
        inbox._update(current, lambda r: r.update(auto_fixed=summary, auto_fix_tried=dict(at=_now(), to=current)))
        more = progress and bool(_remaining(inbox, current, summary)) and summary['rounds'] < MAX_ROUNDS
        if not progress and _remaining(inbox, current, summary):
            # nothing moved this round: record the batch as left so the next round does not ask about it again
            summary['left_keys'] = list(dict.fromkeys(summary['left_keys'] + [i['key'] for i in batch]))
            inbox._update(current, lambda r: r.update(auto_fixed=summary))
            more = bool(_remaining(inbox, current, summary)) and summary['rounds'] < MAX_ROUNDS
        return dict(item=inbox.get(current), summary=summary, next='ai' if more else 'finish')


def revert(h, file_hash, cells=(), fine=(), by=''):
    """Undo chosen automatic changes (as a new version) and chosen "confirmed fine" decisions (on this version)."""
    raise AutoFixError('Reverting automatic changes is disabled pending scoped, atomic review safeguards.')
    from hosting.public_api import _after_change
    from hosting.spreadsheet_inbox import SpreadsheetInbox
    with h.store().session() as directory:
        inbox = SpreadsheetInbox(directory)
        item = inbox.get(file_hash)
        summary = dict(item.get('auto_fixed') or {})
        if not summary:
            raise AutoFixError('Nothing on this version was fixed automatically.')
        if item.get('approval') or any(x.get('sent') for x in item.get('sent') or []):
            raise AutoFixError('This version is approved or sent; open the version before it instead.')
        wanted = {(c.get('sheet'), str(c.get('cell') or '').upper()) for c in cells}
        changes, changed = [], []
        for c in summary.get('changed') or []:
            if (c['sheet'], c['cell'].upper()) in wanted and not c.get('reverted'):
                changes.append(dict(sheet=c['sheet'], cell=c['cell'], before=c['after'], after=c['before'],
                                    reason=f'Reverted the automatic fix ({c.get("source")})', source='Reverted (person)', kind='text'))
                changed.append(dict(c, reverted=True, reverted_by=by or None, reverted_at=_now()))
            else:
                changed.append(c)
        if wanted and not changes:
            raise AutoFixError('Those cells were not changed automatically on this version, or are already reverted.')
        fine_texts = {str(t) for t in fine}
        new_fine = [dict(f, reverted=True, reverted_by=by or None) if f.get('text') in fine_texts and not f.get('reverted') else f for f in summary.get('fine') or []]
        summary.update(changed=changed, fine=new_fine)
        target = file_hash
        if changes:
            new = inbox.apply_fixes(file_hash, changes)
            target = new['file_hash']
        def mark(r):
            r['auto_fixed'] = summary
            r['auto_fix_tried'] = dict(at=_now(), to=target, reverted=True)
            if fine_texts:   # the issue comes back for a person
                r['accepted_issues'] = [a for a in r.get('accepted_issues') or [] if a.get('by_person') or a.get('text') not in fine_texts]
        inbox._update(target, mark)
    if target != file_hash:
        _after_change(h, target)
    with h.store().session() as directory:
        return SpreadsheetInbox(directory).get(target)

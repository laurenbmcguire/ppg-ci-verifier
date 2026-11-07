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


def run(h, file_hash):
    from api.index import ai_review_configured
    from hosting import ai_fix
    from hosting.public_api import _after_change
    from hosting.spreadsheet_inbox import SpreadsheetInbox
    with h.store().session() as directory:
        inbox = SpreadsheetInbox(directory)
        item = inbox.get(file_hash)
        why_not = eligible(item)
        if why_not:
            raise AutoFixError(why_not)
        props = inbox.fix_proposals(file_hash)['proposals']
    sure = [p for p in props if p.get('sure')]
    summary = dict(made_at=_now(), from_hash=file_hash, changed=[], removed=[], moved=[], fine=[], left=[], notes=[])
    current = file_hash
    if sure:
        with h.store().session() as directory:
            new = SpreadsheetInbox(directory).apply_fixes(current, [
                {k: p.get(k) for k in ('id', 'sheet', 'cell', 'before', 'after', 'reason', 'source', 'kind')} for p in sure])
        current = new['file_hash']
        summary['changed'] += [dict(sheet=p['sheet'], cell=p['cell'], before=p['before'], after=p['after'], reason=p.get('only') or p.get('reason'),
                                    source=p.get('source'), by='app') for p in sure]
        _after_change(h, current)
    if ai_review_configured():
        try:
            fixed, ai = ai_fix.run(h, current, None, auto=True)
            current = fixed['file_hash']
            summary['changed'] += [dict(c, source='Claude', by='Claude') for c in ai.get('changed') or []]
            summary['removed'] = ai.get('removed') or []
            summary['moved'] = ai.get('moved') or []
            summary['fine'] = ai.get('fine') or []
            summary['left'] = ai.get('left') or []
            summary['model'] = ai.get('model')
        except ai_fix.AIFixError as exc:
            if 'at least one issue' not in str(exc):
                summary['notes'].append(f'Claude could not finish: {exc}')
    else:
        summary['notes'].append('Claude is not connected here, so only the source-backed and mechanical fixes were made.')
    with h.store().session() as directory:
        inbox = SpreadsheetInbox(directory)
        inbox._update(file_hash, lambda r: r.update(auto_fix_tried=dict(at=_now(), to=current)))
        if current != file_hash:
            inbox._update(current, lambda r: r.update(auto_fixed=summary, auto_fix_tried=dict(at=_now(), to=current)))
        else:
            summary['notes'].append('Nothing needed changing.')
            inbox._update(current, lambda r: r.update(auto_fixed=summary))
        return inbox.get(current), summary


def revert(h, file_hash, cells=(), fine=(), by=''):
    """Undo chosen automatic changes (as a new version) and chosen "confirmed fine" decisions (on this version)."""
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

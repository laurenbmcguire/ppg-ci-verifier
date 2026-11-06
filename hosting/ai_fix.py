"""Fix with AI: the reviewer picks any issues (or all) and Claude resolves every one of them.

Each issue is resolved by changing cells, taking out rows, moving rows to the right shift tab, or confirming the
value is right (with the reason). Changes go into a new version; the version before stays as it was. What Claude did,
and anything it marked for a person to double-check, is kept on the report.
"""
from datetime import datetime, timezone


class AIFixError(ValueError):
    pass


def run(h, file_hash, keys=None, opener=None):
    from hosting import ai_review
    from hosting.spreadsheet_inbox import SpreadsheetInbox, validate_changes
    from hosting.fixes import relocate_rows, FixError
    from hosting.public_api import _after_change
    with h.store().session() as directory:
        item, raw, issues = SpreadsheetInbox(directory).ai_fix_inputs(file_hash)
    if keys is not None:
        wanted = set(keys)
        issues = [i for i in issues if i['key'] in wanted]
    if not issues:
        raise AIFixError('Pick at least one issue for Claude to fix.')
    for i in issues:
        i['ref'] = i['key']
    try:
        plan = ai_review.resolve(item, raw, issues, opener=opener)
    except ai_review.AIReviewNotConfigured as exc:
        raise AIFixError(str(exc)) from None
    except ai_review.AIReviewError as exc:
        raise AIFixError(str(exc)) from None
    by_ref = {i['key']: i for i in issues}
    cells, dropped = validate_changes(item, raw, plan['cells'])
    sheets = {s.get('name') for s in item.get('sheets') or []}
    rows, seen_rows, bad_rows = [], set(), []
    for r in plan['removes'] + plan['moves']:
        target = r.get('to_sheet') if 'to_sheet' in r else None
        ok = r['sheet'] in sheets and (not target or target in sheets) and (r['sheet'], r['row']) not in seen_rows and target != r['sheet']
        (rows if ok else bad_rows).append(r)
        seen_rows.add((r['sheet'], r['row']))
    current = file_hash
    try:
        with h.store().session() as directory:
            inbox = SpreadsheetInbox(directory)
            if cells:
                current = inbox.apply_fixes(current, [dict(c, source='AI fix (asked)') for c in cells])['file_hash']
            if rows:
                now = inbox.get(current)
                _, data = inbox.original_bytes(current)
                changes = relocate_rows(now['filename'], data, [(r['sheet'], r['row'], (r.get('to_sheet') or None)) for r in rows])
                for c in changes:
                    c['reason'], c['source'] = 'Rows taken out or moved by Claude', 'Row taken out or moved (AI fix)'
                current = inbox.apply_fixes(current, changes)['file_hash']
    except FixError as exc:
        raise AIFixError(f'Claude\'s changes could not be applied: {exc}') from None
    fine = [dict(code=by_ref[f['ref']].get('code'), sheet=by_ref[f['ref']].get('sheet'), row=by_ref[f['ref']].get('row'),
                 text=by_ref[f['ref']].get('text'), reason=f['reason']) for f in plan['fine'] if f['ref'] in by_ref]
    handled = {c['ref'] for c in cells} | {r['ref'] for r in rows} | {f['ref'] for f in plan['fine'] if f['ref'] in by_ref}
    left = [dict(text=i.get('text'), why='Claude\'s change did not fit the file' if i['key'] in {d.get('ref') for d in dropped} | {r['ref'] for r in bad_rows} else 'Claude did not resolve it')
            for i in issues if i['key'] not in handled]
    summary = dict(made_at=datetime.now(timezone.utc).isoformat(), model=plan.get('model'), asked=len(issues), from_hash=file_hash,
                   changed=[dict(sheet=c['sheet'], cell=c['cell'], before=c['before'], after=c['after'], reason=c['reason'], check=c['confidence'] == 'check') for c in cells],
                   removed=[dict(sheet=r['sheet'], row=r['row'], reason=r['reason'], check=r['confidence'] == 'check') for r in rows if not r.get('to_sheet')],
                   moved=[dict(sheet=r['sheet'], row=r['row'], to_sheet=r['to_sheet'], reason=r['reason'], check=r['confidence'] == 'check') for r in rows if r.get('to_sheet')],
                   fine=[dict(text=f['text'], reason=f['reason']) for f in fine], left=left)
    def mark(r):
        r['accepted_issues'] = (r.get('accepted_issues') or []) + fine
        r['ai_fixed'] = summary
    with h.store().session() as directory:
        SpreadsheetInbox(directory)._update(current, mark)
    if current != file_hash:
        _after_change(h, current)
    with h.store().session() as directory:
        return SpreadsheetInbox(directory).get(current), summary

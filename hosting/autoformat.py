"""Automatic cleanup after an upload: every change the app is sure about is made, and the report says what was done.

Sure changes: how values are written (capital letters, extra spaces, hidden characters, numbers stored as text), values
SharePoint shows for certain, and exact duplicate rows (every field identical) taken out. Anything the app is not sure
about (a likely typo, an AI suggestion) still waits for a person. The cleaned copy is a new version; the original upload
is kept as it was.
"""
import re
from collections import Counter

HIDDEN = re.compile('[\x00-\x1f\x7f-\x9f\xa0​-‏  ﻿]')
NUMBER = re.compile(r"^\s*'?-?[\d,]+(\.\d+)?\s*$")


def kind(p):
    """'capital letters' | 'extra spaces' | 'hidden characters' | 'spaces and capitals' | 'number stored as text' | None"""
    a, b = str(p.get('before') if p.get('before') is not None else ''), str(p.get('after') if p.get('after') is not None else '')
    if a == b:
        return None
    if a.upper() == b.upper():
        return 'capital letters'
    vis = lambda x: HIDDEN.sub('', x)
    if vis(a) != a and vis(a) == vis(b):
        return 'hidden characters'
    squash = lambda x: re.sub(r'\s+', ' ', x).strip()
    if squash(a) == squash(b):
        return 'extra spaces'
    if squash(vis(a)).upper() == squash(vis(b)).upper():
        return 'spaces and capitals'
    if NUMBER.match(a):
        try:
            if float(a.replace("'", '').replace(',', '').strip()) == float(b):
                return 'number stored as text'
        except ValueError:
            pass
    return None


def formatting_only(proposals):
    return [p for p in proposals if p.get('sure') and p.get('source') != 'AI fix' and kind(p)]


def label(p):
    """Plain words for any sure change."""
    k = kind(p)
    if k:
        return k
    src = str(p.get('source') or '')
    if src.startswith('SharePoint'):
        return 'matched to SharePoint'
    if src.startswith('Spelling'):
        return 'spelled like the other rows'
    return 'sure correction'


def sure_changes(proposals):
    return [p for p in proposals if p.get('sure') and not str(p.get('source') or '').startswith('AI')]


def _dup_rows(item):
    from hosting.review_summary import summarize
    return [(i['sheet'], int(i['row']), i['text']) for i in summarize(item)['issues']
            if i.get('code') == 'duplicate_row' and i.get('row') and 'every field identical' in (i.get('text') or '')]


def run(h, file_hash):
    """Clean an upload. Returns the cleaned item, or None when there was nothing the app was sure about."""
    import os
    if os.environ.get('PPG_AUTOFORMAT', '1') == '0':   # off switch (also used by tests of the manual fix steps)
        return None
    from hosting.spreadsheet_inbox import SpreadsheetInbox
    from hosting.public_api import _after_change
    from hosting.fixes import relocate_rows
    with h.store().session() as directory:
        inbox = SpreadsheetInbox(directory)
        props = inbox.fix_proposals(file_hash)['proposals']
        cells = sure_changes(props)
        current, steps = file_hash, [file_hash]
        if cells:
            current = inbox.apply_fixes(file_hash, [dict(p, source=f"{p.get('source') or 'Fix'} (automatic cleanup)") for p in cells])['file_hash']
        item = inbox.get(current)
        dups = _dup_rows(item)
        removed = []
        if dups:
            _, data = inbox.original_bytes(current)
            steps.append(current)
            moves = relocate_rows(item['filename'], data, [(s_, r, None) for s_, r, _t in dups])
            for c in moves:
                c['reason'], c['source'] = 'Extra copy of a duplicate row taken out', 'Row taken out (automatic cleanup)'
            current = inbox.apply_fixes(current, moves)['file_hash']
            removed = [dict(sheet=s_, row=r, text=t) for s_, r, t in dups]
        if current == file_hash:
            return None
    kinds = Counter(label(p) for p in cells)
    if removed:
        kinds['duplicate row taken out'] = len(removed)
    examples, seen = [], set()
    for p in cells:   # up to 3 examples, a different kind each when possible
        if len(examples) < 3 and label(p) not in seen:
            seen.add(label(p)); examples.append(p)
    for p in cells:
        if len(examples) < 3 and p not in examples:
            examples.append(p)
    note = dict(count=len(cells) + len(removed), cells=len(cells), formatting=sum(1 for p in cells if kind(p)), kinds=dict(kinds), from_hash=file_hash, hidden=list(dict.fromkeys(steps)),
                examples=[dict(sheet=p.get('sheet'), cell=p.get('cell'), before=p.get('before'), after=p.get('after'), kind=label(p)) for p in examples],
                removed_rows=removed[:50], changes=[dict(sheet=p.get('sheet'), cell=p.get('cell'), before=p.get('before'), after=p.get('after'), kind=label(p)) for p in cells[:500]])
    with h.store().session() as directory:
        inbox = SpreadsheetInbox(directory)
        inbox._update(current, lambda r: r.update(auto_formatting=note))
        for x in dict.fromkeys(steps):   # a re-upload of the same file opens the cleaned copy
            inbox._update(x, lambda r: r.update(auto_formatted_to=current))
    _after_change(h, current)
    with h.store().session() as directory:
        return SpreadsheetInbox(directory).get(current)

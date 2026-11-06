"""Formatting-only cleanup, done automatically after an upload.

Only changes that alter how a value is written are made: different capital letters, extra spaces, hidden characters,
and numbers stored as text. Anything that changes a value (a typo, a SharePoint mismatch, a duplicate row) still waits
for a person. The cleaned copy is a new version; the original upload is kept as it was.
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


def run(h, file_hash):
    """Clean the formatting of an upload. Returns the cleaned item, or None when there was nothing to clean."""
    import os
    if os.environ.get('PPG_AUTOFORMAT', '1') == '0':   # off switch (also used by tests of the manual fix steps)
        return None
    from hosting.spreadsheet_inbox import SpreadsheetInbox
    from hosting.public_api import _after_change
    with h.store().session() as directory:
        inbox = SpreadsheetInbox(directory)
        props = inbox.fix_proposals(file_hash)['proposals']
        fmt = formatting_only(props)
        if not fmt:
            return None
        fixed = inbox.apply_fixes(file_hash, [dict(p, source=f"{p.get('source') or 'Fix'} (automatic formatting)") for p in fmt])
    kinds = Counter(kind(p) for p in fmt)
    examples, seen = [], set()
    for p in fmt:   # up to 3 examples, a different kind each when possible
        if len(examples) < 3 and kind(p) not in seen:
            seen.add(kind(p)); examples.append(p)
    for p in fmt:
        if len(examples) < 3 and p not in examples:
            examples.append(p)
    note = dict(count=len(fmt), kinds=dict(kinds), from_hash=file_hash,
                examples=[dict(sheet=p.get('sheet'), cell=p.get('cell'), before=p.get('before'), after=p.get('after'), kind=kind(p)) for p in examples])
    with h.store().session() as directory:
        inbox = SpreadsheetInbox(directory)
        inbox._update(fixed['file_hash'], lambda r: r.update(auto_formatting=note))
        inbox._update(file_hash, lambda r: r.update(auto_formatted_to=fixed['file_hash']))   # a re-upload of the same file opens the cleaned copy
    _after_change(h, fixed['file_hash'])
    with h.store().session() as directory:
        return SpreadsheetInbox(directory).get(fixed['file_hash'])

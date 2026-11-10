"""Exact, read-only mechanical evidence for explicitly selected findings.

This is not bulk recommendation eligibility and cannot clear source holds.
"""
import re
from collections import Counter
from hosting.fixes import proposals, FixError
from hosting.review_summary import summarize

SOURCES = frozenset(('Clean-up', 'Number as text', 'Spelling'))
FIELDS = ('id', 'sheet', 'cell', 'before', 'after', 'kind', 'source',
          'snapshot_id', 'item', 'field', 'change_set', 'change_set_size')


def source_owned(issue):
    return (issue.get('source_value') is not None
            or str(issue.get('source') or '').startswith('SharePoint')
            or bool(re.search(r'^(source_|sharepoint_)|timezone.*unconfirmed|date.*unconfirmed', str(issue.get('code') or ''), re.I)))


def candidates(item, raw):
    """Recompute from unchanged bytes; never elevate suggested or neighboring values."""
    review = summarize(item)
    issues = review['issues']
    all_issues = issues + review.get('dismissed', [])
    fixes = proposals(item, item['filename'], raw)
    cells = Counter((p.get('sheet'), p.get('cell')) for p in fixes)
    out = {}
    for issue in issues:
        key, cell, sheet = issue['key'], issue.get('cell'), issue.get('sheet')
        if (not issue.get('fp') or not isinstance(cell, str)
                or not re.fullmatch(r'[A-Z]{1,3}[1-9][0-9]{0,6}', cell)
                or issue.get('decision') or issue.get('previous_decision')
                or key in (item.get('review_decisions') or {}) or source_owned(issue)):
            continue
        related = [i for i in all_issues if i.get('sheet') == sheet and i.get('cell') == cell]
        if any(source_owned(i) or i.get('decision') or i.get('previous_decision') for i in related):
            continue
        for fix in fixes:
            if (fix.get('sheet') != sheet or fix.get('cell') != cell or cells[(sheet, cell)] != 1
                    or fix.get('source') not in SOURCES or fix.get('sure') is not True
                    or fix.get('kind') not in ('text', 'number')
                    or any(fix.get(k) is not None for k in ('snapshot_id', 'item', 'field', 'change_set', 'change_set_size'))):
                continue
            out[key] = dict(fix, finding_id=key, selected_finding_id=key,
                            selected_finding_fp=issue['fp'], recommendation_eligible=False,
                            recommendation_reason='Exact mechanical correction for this selected finding; checked again when saved.')
    return out


def validate(item, raw, changes):
    marked = [c for c in changes if 'selected_finding_id' in c or 'selected_finding_fp' in c]
    if not marked:
        return
    current = candidates(item, raw)
    for change in marked:
        exact = current.get(change.get('selected_finding_id'))
        if (not exact or change.get('selected_finding_fp') != exact['selected_finding_fp']
                or any(change.get(k) != exact.get(k) for k in FIELDS)):
            raise FixError('The selected mechanical correction changed or lacks exact current evidence. Reopen this finding. Nothing was applied.')

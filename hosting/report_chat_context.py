"""Minimized report-question context. Never serializes a workbook or calls a provider."""
import re
from hosting.fixes import _sources, _addr, col_index, col_letter
from hosting import reconcile

MAX_CELLS = 12
MAX_FINDINGS = 12


def _short(value, limit=400):
    return None if value is None else str(value)[:limit]


def build(item, raw, review, holds, question):
    sources, cached = _sources(item['filename'], raw)
    sheets = dict(sources)
    references = []
    def add(sheet, cell):
        key = (sheet, str(cell).replace('$', '').upper())
        try:
            letters, row = _addr(key[1])
            if col_index(letters) >= 16384 or row > 1048576: return
        except ValueError:
            return
        if key not in references: references.append(key)
    # Only known sheets in this exact report can be named; no files/URLs are resolved.
    for sheet in sheets:
        pattern = r"(?:'" + re.escape(sheet) + r"'|(?<![\w])" + re.escape(sheet) + r")!\s*(\$?[A-Za-z]{1,3}\$?[1-9][0-9]{0,6})\b"
        for match in re.finditer(pattern, question, re.I): add(sheet, match[1])
    if len(sheets) == 1:
        sheet = next(iter(sheets))
        for match in re.finditer(r'(?<![\w!])\$?[A-Za-z]{1,3}\$?[1-9][0-9]{0,6}\b', question):
            if not re.search(r'!\s*$', question[:match.start()]): add(sheet, match[0])
    explicit = references[:MAX_CELLS]
    all_issues = [f for f in review.get('issues') or [] if isinstance(f, dict)]
    positions = {(s, int(re.sub(r'\D', '', c))) for s, c in explicit}
    terms = set(re.findall(r'[a-z]{3,}', question.lower())) - {'the','this','that','why','what','about','report','please','does','could','would','with','from','have','are','and','for'}
    def match_issue(f):
        if explicit:
            return 1 if (f.get('sheet'), f.get('row')) in positions else 0
        words = set(re.findall(r'[a-z]{3,}', str(f.get('text') or '').lower()))
        return len(words & terms)
    matches = [f for f in all_issues if match_issue(f)]
    selected = sorted(matches, key=match_issue, reverse=True)[:MAX_FINDINGS] if matches else ([] if explicit else all_issues[:8])
    for finding in selected:
        if finding.get('sheet') in sheets and finding.get('cell'): add(finding['sheet'], finding['cell'])
    references = references[:MAX_CELLS]
    cells = []
    seen = set()
    def cell_value(sheet, address, why):
        if (sheet, address) in seen: return
        seen.add((sheet, address))
        letters, row = _addr(address); column = col_index(letters)
        records = sheets[sheet]
        value = records[row-1][column] if row <= len(records) and column < len(records[row-1]) else None
        entry = dict(sheet=sheet, cell=address, value=_short(value, 160), blank=value in (None, ''), purpose=why)
        if isinstance(value, str) and value.startswith('='):
            entry['formula'] = True
            saved = cached.get(sheet) or []
            entry['saved_result'] = _short(saved[row-1][column] if row <= len(saved) and column < len(saved[row-1]) else None, 160)
        cells.append(entry)
    for sheet, address in references:
        cell_value(sheet, address, 'referenced or relevant finding cell')
        letters, row = _addr(address)
        metadata = next((s for s in item.get('sheets') or [] if s.get('name') == sheet), {})
        headers = [t.get('header_row') for t in metadata.get('tables') or [] if isinstance(t.get('header_row'), int) and 0 < t['header_row'] < row]
        header = max(headers) if headers else (1 if row > 1 else None)
        if header: cell_value(sheet, letters + str(header), 'column header')
    positions = {(s, int(re.sub(r'\D', '', c))) for s, c in references}
    current = reconcile.current(item)
    source_binding_held = any(h.get('code') in ('source_mapping_changed','source_assignment_changed','source_assignment_unavailable') for h in holds)
    if current and source_binding_held:
        source = {'status':'unverified','scope':'The stored source comparison is historical evidence only: the current source assignment or column mapping is not confirmed. Resolve the source hold and compare again; no source differences are supplied here.'}
    elif current:
        source = {k: current.get(k) for k in ('status','snapshot_id','captured_at','report_rows','fields_checked','fields_not_checked')}
        source['scope'] = 'Current stored source comparison; chat does not verify or release this report.'
        source['relevant_differences'] = [{k:_short(d.get(k),160) for k in ('sheet','row','field','report','sharepoint','status')}
            for d in current.get('rows') or [] if (d.get('sheet'),d.get('row')) in positions][:MAX_CELLS]
    elif item.get('source_reconciliation'):
        source = {'status':'stale','scope':'Saved comparison is stale and cannot verify the current report.'}
    else:
        source = {'status':(item.get('sharepoint') or {}).get('status') or 'not_compared',
                  'scope':'No current exact-source comparison is supplied; older day-level matches are not verification.'}
    identity = item.get('report_identity') or {}
    quantities = {}
    for field in ('inspected','certified','rejected'):
        q = (review.get('quantities') or {}).get(field)
        if isinstance(q, dict):
            quantities[field] = dict(value=_short(q.get('value'),120), unit=_short(q.get('unit'),60),
                partial=q.get('partial') if type(q.get('partial')) is bool else None,
                complete=q.get('complete') if type(q.get('complete')) is bool else None)
        else:
            quantities[field] = dict(value=_short(review.get(field),120),
                unit=_short(review.get('rejected_unit'),60) if field=='rejected' else None,
                partial=True if field=='rejected' and review.get('rejected_partial') else None, complete=None)
    summary = dict(status=review.get('status'), scans=review.get('scans'), quantities=quantities,
        quantity_scope='Only complete=true is a full total. Partial or unknown completeness is not an exact total; unknown units must not be assumed.')
    return dict(excerpt_only=True,
        report=dict(file_hash=item['file_hash'],check_hash=item['check_hash'],filename=item['filename'],file_version=item.get('version'),revision=item.get('revision'),
            identity={k:_short(identity.get(k),120) for k in ('project_number','report_date','client')},
            summary=summary,
            internal_check_status=item.get('internal_check_status'),release_status=item.get('release_status')),
        evidence_scope='Only the question-referenced cells, relevant finding cells and their column headers are included. Other cells and sheets were not sent. Ask for an exact sheet and cell when evidence is missing.',
        findings_total=len(all_issues), findings_shown=len(selected),
        findings=[{k:_short(f.get(k),600 if k=='text' else 120) for k in ('source','severity','sheet','row','cell','text')} for f in selected],
        holds=[{k:_short(h.get(k),400) for k in ('code','text')} for h in holds[:12]],
        source_comparison=source, relevant_cells=cells,
        selection_limited=len(explicit)>=MAX_CELLS or len(all_issues)>len(selected))

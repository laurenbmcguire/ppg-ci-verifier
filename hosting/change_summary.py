"""Report change summary: how a report compared with SharePoint and every change made to it, as a PDF kept on record.

Made automatically when a report that differed from SharePoint (or was changed) is approved, and on request. Each PDF
is stored with the report and listed in the records, so there is always a copy of what was different and what was done.
"""
import hashlib
import io
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

RECORDS = 'change_summaries'
CT = ZoneInfo('America/Chicago')


def _chain(inbox, file_hash):
    """[original, ..., final] versions, following each corrected copy back to the first upload."""
    out, seen, h = [], set(), file_hash
    while h and h not in seen and len(out) < 30:
        seen.add(h)
        item = inbox.get(h)
        out.append(item)
        h = (item.get('fixed_from') or {}).get('file_hash')
    return out[::-1]


def collect(inbox, file_hash, catalog=None):
    from hosting.review_summary import summarize
    from hosting import sharepoint_jobs
    versions = _chain(inbox, file_hash)
    first, final = versions[0], versions[-1]
    rv_first, rv_final = summarize(first), summarize(final)
    job = rv_final.get('job') or rv_first.get('job')
    sp = first.get('sharepoint') or {}
    sp_final = final.get('sharepoint') or {}
    changes, rows = [], []
    is_row_move = lambda src: src.startswith('Row ') or src in ('Shift times', 'Duplicate rows') or src.startswith('Duplicate rows')
    for v in versions[1:]:
        moved_cells = 0
        for c in (v.get('fixed_from') or {}).get('changes') or []:
            src = str(c.get('source') or '')
            how = 'Automatic cleanup' if 'automatic' in src else 'Fixed with AI' if 'AI' in src else 'By a person'
            if is_row_move(src):   # rows taken out or moved: one line per row below, not every shifted cell
                moved_cells += 1
                continue
            changes.append(dict(c, how=how, version=v.get('version'), filename=v.get('filename')))
        a, x = v.get('auto_formatting') or {}, v.get('ai_fixed') or {}
        listed = [dict(what='Taken out', where=f"{r['sheet']} row {r['row']}", why=r.get('text') or 'Exact duplicate row', how='Automatic cleanup') for r in a.get('removed_rows') or []]
        listed += [dict(what='Taken out', where=f"{r['sheet']} row {r['row']}", why=r.get('reason'), how='Fixed with AI') for r in x.get('removed') or []]
        listed += [dict(what=f"Moved to {r['to_sheet']}", where=f"{r['sheet']} row {r['row']}", why=r.get('reason'), how='Fixed with AI') for r in x.get('moved') or []]
        if moved_cells and not listed:
            listed = [dict(what='Rows taken out or moved', where=f'{moved_cells} cells shifted', why=next((c.get('reason') for c in (v.get('fixed_from') or {}).get('changes') or [] if is_row_move(str(c.get('source') or ''))), ''), how='By a person')]
        rows += listed
    fine = [dict(text=a.get('text'), reason=a.get('reason')) for a in (final.get('accepted_issues') or [])]
    open_issues = [i for i in rv_final['issues'] if i['severity'] != 'info']
    job_check = sharepoint_jobs.lookup(job, catalog)
    differences = [f.get('message') for f in (sp.get('findings') or []) if f.get('message')]
    return dict(job=job, day=rv_final.get('day') or rv_first.get('day'), file_hash=final['file_hash'], first_hash=first['file_hash'],
                first_filename=first.get('filename'), final_filename=final.get('filename'), versions=len(versions),
                job_check=job_check, sharepoint_first=dict(status=sp.get('status'), message=sp.get('message'), matched=sp.get('matched_rows'),
                                                         report_rows=sp.get('report_rows'), sharepoint_rows=sp.get('sharepoint_rows')),
                sharepoint_final=dict(status=sp_final.get('status'), message=sp_final.get('message'), findings=len(sp_final.get('findings') or [])),
                differences=differences, changes=changes, rows=rows, fine=fine, open=[dict(severity=i['severity'], where=i.get('where') or (f"{i.get('sheet')} row {i.get('row')}" if i.get('row') else i.get('sheet') or ''), text=i.get('text')) for i in open_issues],
                approval=final.get('approval'), sent=[s for s in (final.get('sent') or []) if s.get('sent')],
                inspected=rv_final.get('inspected'), rejected=rv_final.get('rejected'), source_document=(final.get('source_document') or {}).get('filename'))


def differs(data):
    """True when the report did not simply match SharePoint as uploaded."""
    return bool(data['differences'] or data['changes'] or data.get('rows') or data['open'] or data['job_check'].get('status') in ('differs', 'not_found', 'ambiguous'))


def render(data, made_by=None):
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import inch
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle, KeepTogether
    from xml.sax.saxutils import escape
    st = getSampleStyleSheet()
    green = colors.HexColor('#20594d')
    H1 = ParagraphStyle('h1', parent=st['Title'], alignment=0, fontSize=18, textColor=green, spaceAfter=4)
    H2 = ParagraphStyle('h2', parent=st['Heading2'], fontSize=12, textColor=green, spaceBefore=12, spaceAfter=4)
    P = ParagraphStyle('p', parent=st['Normal'], fontSize=9.5, leading=13)
    S = ParagraphStyle('s', parent=P, fontSize=8.5, leading=11, textColor=colors.HexColor('#555555'))
    C = ParagraphStyle('c', parent=P, fontSize=8.5, leading=11)
    e = lambda v: escape('' if v is None else str(v))
    now = datetime.now(timezone.utc).astimezone(CT)
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=letter, leftMargin=0.7 * inch, rightMargin=0.7 * inch, topMargin=0.6 * inch, bottomMargin=0.6 * inch,
                            title=f"Report change summary {data['job'] or ''} {data['day'] or ''}", author='PPG Verifier')
    def table(head, rows, widths):
        t = Table([[Paragraph(f'<b>{e(x)}</b>', C) for x in head]] + [[Paragraph(e(x), C) for x in r] for r in rows], colWidths=widths, repeatRows=1)
        t.setStyle(TableStyle([('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#e8f2ee')), ('GRID', (0, 0), (-1, -1), 0.4, colors.HexColor('#c9d6d1')),
                               ('VALIGN', (0, 0), (-1, -1), 'TOP'), ('TOPPADDING', (0, 0), (-1, -1), 3), ('BOTTOMPADDING', (0, 0), (-1, -1), 3)]))
        return t
    jc = data['job_check']
    out = [Paragraph('Report change summary', H1),
           Paragraph(f"<b>{e(data['job'] or 'Unknown job')}</b> &nbsp;·&nbsp; report day {e(data['day'] or 'unknown')} &nbsp;·&nbsp; made {now.strftime('%b %d, %Y %I:%M %p')} CT" + (f" by {e(made_by)}" if made_by else ''), P),
           Spacer(1, 4),
           Paragraph(f"First upload: {e(data['first_filename'])}" + (f" (from {e(data['source_document'])})" if data.get('source_document') else '') +
                     f"<br/>Final version: {e(data['final_filename'])} ({data['versions']} version{'s' if data['versions'] != 1 else ''})", S)]
    # the outcome in one paragraph
    n_changes, n_open, n_rows = len(data['changes']), len(data['open']), len(data.get('rows') or [])
    why = {'differs': 'the job number is written differently from SharePoint', 'not_found': 'the job is not in the SharePoint job list',
           'ambiguous': 'the job is on more than one SharePoint site'}.get(jc.get('status'))
    diff = [f"{len(data['differences'])} difference{'s' if len(data['differences']) != 1 else ''} from the SharePoint scans" if data['differences'] else None, why]
    did = [f"{n_changes} cell{'s' if n_changes != 1 else ''} changed" if n_changes else None, f"{n_rows} row{'s' if n_rows != 1 else ''} taken out or moved" if n_rows else None,
           f"{len(data['fine'])} flagged item{'s' if len(data['fine']) != 1 else ''} checked and found fine" if data['fine'] else None]
    diff, did = [x for x in diff if x], [x for x in did if x]
    verdict = ('Matched SharePoint and needed no changes.' if not differs(data) else
               (f"Did not match SharePoint: {'; '.join(diff)}. " if diff else '') + (f"{'; '.join(did).capitalize()}. " if did else 'Nothing was changed. ')
               + (f"{n_open} item{'s' if n_open != 1 else ''} still open at the end." if n_open else 'Nothing left open at the end.'))
    appr = data.get('approval')
    out += [Paragraph('Summary', H2), Paragraph(e(verdict), P)]
    if appr:
        out.append(Paragraph(f"Approved as final by {e(appr.get('by'))} on {e(str(appr.get('at') or '')[:10])}." + (' Sent to the client.' if data['sent'] else ' Not sent yet.'), P))
    # SharePoint
    sp = data['sharepoint_first']
    out += [Paragraph('SharePoint check', H2), Paragraph('<b>Job:</b> ' + e(jc.get('message')), P)]
    if sp.get('status'):
        rows_line = f"{sp.get('matched')} of {sp.get('report_rows')} report rows matched SharePoint scans ({sp.get('sharepoint_rows')} scans on SharePoint)." if sp.get('report_rows') is not None else ''
        out.append(Paragraph('<b>Rows, as uploaded:</b> ' + e(sp.get('message') or rows_line or sp.get('status')), P))
    else:
        out.append(Paragraph('<b>Rows:</b> not compared with SharePoint scans (SharePoint was not reachable for this report).', P))
    if data['sharepoint_final'].get('status'):
        out.append(Paragraph('<b>Rows, final version:</b> ' + e(data['sharepoint_final'].get('message') or data['sharepoint_final'].get('status')), P))
    if data['differences']:
        out += [Spacer(1, 4), table(['Different from SharePoint (as uploaded)'], [[d] for d in data['differences'][:80]], [7.1 * inch])]
        if len(data['differences']) > 80:
            out.append(Paragraph(f"…and {len(data['differences']) - 80} more.", S))
    # changes
    out.append(Paragraph('Changes made', H2))
    if data['changes']:
        out.append(table(['Where', 'Before', 'After', 'Why', 'Done by'],
                         [[f"{c.get('sheet')} {c.get('cell')}", c.get('before'), c.get('after'), c.get('reason'), c.get('how')] for c in data['changes'][:400]],
                         [1.0 * inch, 1.15 * inch, 1.15 * inch, 2.6 * inch, 1.2 * inch]))
        if len(data['changes']) > 400:
            out.append(Paragraph(f"…and {len(data['changes']) - 400} more.", S))
    else:
        out.append(Paragraph('No cells were changed.', P))
    if data.get('rows'):
        out += [Paragraph('Rows taken out or moved', H2), table(['Row', 'What', 'Why', 'Done by'], [[r['where'], r['what'], r['why'], r['how']] for r in data['rows'][:200]],
                                                                [1.3 * inch, 1.2 * inch, 3.4 * inch, 1.2 * inch]), Paragraph('Rows below each one moved up; totals and formulas were not touched.', S)]
    if data['fine']:
        out += [Paragraph('Checked and fine as is', H2), table(['Flagged', 'Why it is fine'], [[f['text'], f['reason']] for f in data['fine'][:100]], [3.4 * inch, 3.7 * inch])]
    out.append(Paragraph('Still open at the end', H2))
    out.append(table(['Severity', 'Where', 'What'], [[{'high': 'Must fix', 'medium': 'Check', 'low': 'Minor'}.get(o['severity'], o['severity']), o['where'], o['text']] for o in data['open'][:150]], [0.8 * inch, 1.3 * inch, 5.0 * inch])
               if data['open'] else Paragraph('Nothing.', P))
    out += [Spacer(1, 10), Paragraph('Made by the PPG Verifier. Internal record: not for the client.', S)]
    doc.build(out)
    return buf.getvalue()


def make(h, file_hash, made_by=None, reason='requested', only_if_differs=False):
    """Build the PDF, store it with the report and add it to the records. Returns the record."""
    from hosting.spreadsheet_inbox import SpreadsheetInbox, files_for
    from hosting import sharepoint_jobs
    with h.store().session() as directory:
        inbox = SpreadsheetInbox(directory)
        catalog = inbox.setting(sharepoint_jobs.KEY)
        data = collect(inbox, file_hash, catalog)
    if only_if_differs and not differs(data):
        return None
    pdf = render(data, made_by)
    made = datetime.now(timezone.utc).isoformat()
    rid = hashlib.sha256((data['file_hash'] + made).encode()).hexdigest()[:20]
    path = f'uploads/summary-{rid}.pdf'
    record = dict(id=rid, file_hash=data['file_hash'], first_hash=data['first_hash'], job=data['job'], day=data['day'], made_at=made, by=made_by, reason=reason,
                  differed=differs(data), differences=len(data['differences']), changes=len(data['changes']), rows=len(data.get('rows') or []), open=len(data['open']),
                  job_check=data['job_check'].get('status'), path=path, size=len(pdf),
                  filename=f"Change summary {data['job'] or 'report'} {data['day'] or ''}.pdf".replace('  ', ' '))
    with h.store().session() as directory:
        inbox = SpreadsheetInbox(directory)
        files_for(inbox.root).put(path, pdf)
        recs = inbox.setting(RECORDS, []) or []
        inbox.set_setting(RECORDS, ([record] + recs)[:3000])
        inbox._update(data['file_hash'], lambda r: r.update(change_summaries=([dict(id=rid, made_at=made, by=made_by, reason=reason)] + (r.get('change_summaries') or []))[:50]))
    return record


def pdf_for(h, rid):
    from hosting.spreadsheet_inbox import SpreadsheetInbox, files_for
    with h.store().session() as directory:
        inbox = SpreadsheetInbox(directory)
        rec = next((r for r in inbox.setting(RECORDS, []) or [] if r.get('id') == rid), None)
        if not rec:
            raise ValueError('Change summary not found.')
        return rec, files_for(inbox.root).get(rec['path'])

"""Report change summary: how a report compared with SharePoint and every change made to it, as a PDF kept on record.

Made automatically when a report that differed from SharePoint (or was changed) is approved, and on request. Each PDF
is stored with the report and listed in the records, so there is always a copy of what was different and what was done.
"""
import hashlib
import io
import json
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


def source_receipt(item, day, assignment=None, mapping_state=None):
    """Fail closed: a successful check must identify this exact workbook and checker."""
    rec = item.get('source_reconciliation') or {}
    bound = rec.get('bound_to') or {}
    reasons = []
    from hosting.spreadsheets import CHECKER_VERSION
    if item.get('checker_version') != CHECKER_VERSION:
        reasons.append('The saved checker is outdated; recheck before source verification.')
    if not rec: reasons.append('No item-by-item source check is recorded.')
    if rec.get('stale'): reasons.append('The source check is stale.')
    from hosting.source_date_contracts import receipt_contract_current
    if rec.get('date_contract') is not None and not receipt_contract_current(rec['date_contract']):
        reasons.append('Source-date metadata expired or changed. Recheck SharePoint to refresh its timezone and Date-field evidence before generating a verified summary.')
    from hosting.reconcile import current
    if rec and current(item) is None:
        reasons.append('The saved source receipt does not match the current authoritative report scope or binding. Review the layout and compare the source again.')
    for key in ('file_hash', 'checker_version'):
        if not item.get(key) or bound.get(key) != item.get(key):
            reasons.append(f'Missing or mismatched {key} binding.')
    if not day or rec.get('day') != day: reasons.append('Business date is not bound.')
    for key in ('site_id', 'list_id', 'snapshot_id', 'captured_at'):
        if not rec.get(key): reasons.append(f'Missing {key}.')
    from hosting.registry import RESOLVED
    assignment = assignment or {}
    for key in ('site_id', 'list_id'):
        if not assignment.get(key) or assignment.get(key) != rec.get(key):
            reasons.append(f'Current assignment {key} is missing or differs from the checked source.')
    if assignment.get('needs_person') or assignment.get('status') not in RESOLVED:
        reasons.append('Current client/job assignment is unresolved.')
    mapping = rec.get('mapping_binding') or {}
    if not mapping or mapping.get('legacy_requires_confirmation') or any(not mapping.get(k) or mapping.get(k) != rec.get(k) for k in ('site_id','list_id')):
        reasons.append('Exact-source column mapping is missing or requires reconfirmation.')
    if mapping_state is not None and mapping != mapping_state:
        reasons.append('Source columns changed after this comparison; compare again.')
    cov = rec.get('coverage') or {}
    if cov.get('complete') is not True or any(cov.get(k) for k in ('denied','throttled','failed_pages','truncated','unknown_schema','undated','date_fallbacks')):
        reasons.append('Source coverage is incomplete or unavailable.')
    from hosting.reconcile import counts_verified
    if not counts_verified(rec):
        reasons.append('Inspection and rejection count/quantity evidence is incomplete or differs.')
    if not isinstance(rec.get('report_rows'), int) or rec.get('report_rows') != rec.get('sharepoint_items_for_day'):
        reasons.append('Source/report row counts are missing or unequal.')
    if not {'inspected', 'rejected'} <= set(rec.get('fields_checked') or []):
        reasons.append('Both inspected and rejected fields must have been checked.')
    passed = not reasons and rec.get('status') == 'verified' and rec.get('verified') is True and not rec.get('rows') and not rec.get('rows_truncated') and not rec.get('not_verified_because')
    check_hash = hashlib.sha256(json.dumps(rec, sort_keys=True, default=str).encode()).hexdigest() if rec else None
    mapping_sha256 = hashlib.sha256(json.dumps(mapping_state if mapping_state is not None else mapping, sort_keys=True, default=str).encode()).hexdigest()
    return dict(mapping_sha256=mapping_sha256, status='verified' if passed else 'held', reasons=reasons, check_sha256=check_hash,
                workbook_sha256=item.get('file_hash'), checker_version=item.get('checker_version'),
                source_status=rec.get('status'), snapshot_id=rec.get('snapshot_id'), site_id=rec.get('site_id'),
                list_id=rec.get('list_id'), captured_at=rec.get('captured_at'), day=rec.get('day'),
                report_rows=rec.get('report_rows'), source_rows=rec.get('sharepoint_items_for_day'),
                fields_checked=rec.get('fields_checked') or [], differences=rec.get('rows') or [],
                corrections_applied=rec.get('corrections_applied'))


def collect(inbox, file_hash, catalog=None):
    from hosting.review_summary import summarize
    from hosting import sharepoint_jobs
    versions = _chain(inbox, file_hash)
    first, final = versions[0], versions[-1]
    rv_first, rv_final = summarize(first), summarize(final)
    job = rv_final.get('job') or rv_first.get('job')
    day = rv_final.get('day') or rv_first.get('day')
    assignment = inbox.assignment_for(final, rv_final)
    rec = final.get('source_reconciliation') or {}
    try:
        mapping_state = inbox.source_mapping(rec.get('site_id'), rec.get('list_id'))
    except ValueError:
        mapping_state = {}
    receipt = source_receipt(final, day, assignment, mapping_state)
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
    # what a person decided on the final version (never an earlier version's decisions, never Claude's): the summary
    # is made from the exact version it describes, and says which check run and which SharePoint read that was
    where = lambda i: i.get('where') or (f"{i.get('sheet')} row {i.get('row')}" if i.get('row') else i.get('sheet') or '')
    fine = [dict(text=f"{where(i)}: {i.get('text')}", reason=f"{(i.get('decision') or {}).get('reason')} ({(i.get('decision') or {}).get('by') or 'a reviewer'})")
            for i in rv_final['dismissed'] if i.get('dismissed_by') == 'person']
    earlier = [dict(text=f"{where(i)}: {i.get('text')}", reason=i.get('verdict_reason')) for i in rv_final['dismissed'] if i.get('dismissed_by') == 'person_legacy']
    claude = [dict(text=f"{where(i)}: {i.get('text')}", reason=i.get('verdict_reason')) for i in rv_final['dismissed'] if str(i.get('dismissed_by')).startswith('claude')]
    deferred = [dict(text=f"{where(i)}: {i.get('text')}", reason=f"Waiting for {(i.get('decision') or {}).get('owner') or 'the source owner'}: {(i.get('decision') or {}).get('reason')}")
                for i in rv_final['issues'] if (i.get('decision') or {}).get('action') == 'defer']
    open_issues = [i for i in rv_final['issues'] if i['severity'] != 'info' and (i.get('decision') or {}).get('action') != 'defer']
    job_check = sharepoint_jobs.lookup(job, catalog)
    differences = [f.get('message') for f in (sp.get('findings') or []) if f.get('message')]
    action_snapshot = dict(versions=versions, assignment=assignment, source_receipt=receipt)
    action_hash = hashlib.sha256(json.dumps(action_snapshot, sort_keys=True, default=str).encode()).hexdigest()
    return dict(source_receipt=receipt, action_snapshot_sha256=action_hash, job=job, day=rv_final.get('day') or rv_first.get('day'), file_hash=final['file_hash'], first_hash=first['file_hash'],
                first_filename=first.get('filename'), final_filename=final.get('filename'), versions=len(versions),
                job_check=job_check, sharepoint_first=dict(status=sp.get('status'), message=sp.get('message'), matched=sp.get('matched_rows'),
                                                         report_rows=sp.get('report_rows'), sharepoint_rows=sp.get('sharepoint_rows')),
                sharepoint_final=dict(status=(rv_final.get('sharepoint') or {}).get('status') or sp_final.get('status'),
                                      message=(rv_final.get('sharepoint') or {}).get('message') or sp_final.get('message'),
                                      captured_at=(rv_final.get('sharepoint') or {}).get('captured_at'), findings=len(sp_final.get('findings') or [])),
                run=rv_final.get('run'), revision=final.get('revision') or 1, reconcile=rv_final.get('reconcile'),
                earlier=earlier, claude=claude, deferred=deferred,
                differences=differences, changes=changes, rows=rows, fine=fine, open=[dict(severity=i['severity'], where=i.get('where') or (f"{i.get('sheet')} row {i.get('row')}" if i.get('row') else i.get('sheet') or ''), text=i.get('text')) for i in open_issues],
                approval=final.get('approval'), sent=[s for s in (final.get('sent') or []) if s.get('sent')],
                inspected=rv_final.get('inspected'), rejected=rv_final.get('rejected'), source_document=(final.get('source_document') or {}).get('filename'))


def differs(data):
    """True when the report did not simply match SharePoint as uploaded."""
    return bool(data.get('source_receipt', {}).get('status') != 'verified' or data['changes'] or data.get('rows') or data['open'])


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
    diff = []  # Legacy upload/catalog differences cannot override the current source receipt.
    did = [f"{n_changes} cell{'s' if n_changes != 1 else ''} changed" if n_changes else None, f"{n_rows} row{'s' if n_rows != 1 else ''} taken out or moved" if n_rows else None,
           f"{len(data['fine'])} prior accepted finding decision(s) recorded" if data['fine'] else None]
    diff, did = [x for x in diff if x], [x for x in did if x]
    verdict = ('Matched SharePoint and needed no changes.' if not differs(data) else
               (f"Did not match SharePoint: {'; '.join(diff)}. " if diff else '') + (f"{'; '.join(did).capitalize()}. " if did else 'Nothing was changed. ')
               + (f"{n_open} item{'s' if n_open != 1 else ''} still open at the end." if n_open else 'Nothing left open at the end.'))
    receipt = data.get('source_receipt') or {}
    if receipt.get('status') != 'verified':
        verdict = 'HOLD: current exact-version source verification is unavailable or has unresolved concerns. ' + verdict
    appr = data.get('approval')
    out += [Paragraph('Summary', H2), Paragraph(e(verdict), P)]
    if appr:
        out.append(Paragraph(f"Recorded approval by {e(appr.get('by'))} on {e(str(appr.get('at') or '')[:10])}." + (' Sent to the client.' if data['sent'] else ' Not sent yet.'), P))
    # Current source receipt takes precedence over legacy upload-time checks and catalogs.
    out += [Paragraph('Current SharePoint check', H2),
            Paragraph('<b>Status:</b> ' + e(receipt.get('status') or 'unavailable'), P)]
    for label, key in [('Workbook SHA-256','workbook_sha256'), ('Checker version','checker_version'),
                       ('Check SHA-256','check_sha256'), ('Snapshot ID','snapshot_id'),
                       ('Site ID','site_id'), ('List ID','list_id'), ('Captured at','captured_at'), ('Business date','day')]:
        out.append(Paragraph(f'<b>{label}:</b> ' + e(receipt.get(key) or 'unavailable'), S))
    out.append(Paragraph('<b>Rows:</b> ' + e(f"{receipt.get('report_rows')} report / {receipt.get('source_rows')} source; fields checked: {', '.join(receipt.get('fields_checked') or []) or 'unavailable'}"), P))
    for reason in receipt.get('reasons') or []:
        out.append(Paragraph(e(reason), P))
    if receipt.get('differences'):
        out.append(Paragraph(f"{len(receipt['differences'])} current source discrepancies recorded. Report remains on hold.", P))
    out.append(Paragraph('<b>Report/check/action snapshot SHA-256:</b> ' + e(data.get('action_snapshot_sha256') or 'unavailable'), S))
    out.append(Paragraph('Prior accepted findings and approvals below are historical decisions, not proof of current source completeness or permission to release.', S))
    if data.get('differences'):
        out.append(Paragraph('Historical upload-time differences (not the current verdict)', H2))
        out.append(table(['Previously recorded difference'], [[v] for v in data['differences'][:80]], [7.1 * inch]))
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
        out += [Paragraph('Kept as recorded by a reviewer (this version)', H2), table(['Flagged', 'Reason (who)'], [[f['text'], f['reason']] for f in data['fine'][:100]], [3.4 * inch, 3.7 * inch])]
    if data.get('deferred'):
        out += [Paragraph('Waiting on the source owner (report on hold)', H2), table(['Flagged', 'Waiting for'], [[f['text'], f['reason']] for f in data['deferred'][:100]], [3.4 * inch, 3.7 * inch])]
    if data.get('earlier'):
        out += [Paragraph('Kept by an earlier record', H2), Paragraph(f"{len(data['earlier'])} finding(s) settled by an older “fine as is” record.", P)]
    if data.get('claude'):
        out += [Paragraph('Set aside by Claude (recommendations, not decisions)', H2), Paragraph(f"{len(data['claude'])} finding(s). Each is listed with Claude's reason in the app.", P)]
    rc = data.get('reconcile') or {}
    if rc:
        out.append(Paragraph(f"All {rc.get('total')} findings on v{data.get('revision')}: {rc.get('open')} open, {rc.get('deferred')} waiting, {rc.get('confirmed')} confirmed, "
                             f"{rc.get('kept_by_person')} kept by a reviewer, {rc.get('kept_earlier_record')} kept by earlier records, {rc.get('set_aside_by_claude')} set aside by Claude, "
                             f"{rc.get('corrected')} corrected later, {rc.get('informational')} for information. Check run: {e(str((data.get('run') or {}).get('file_hash') or '')[:12])}… "
                             f"checker {e(str((data.get('run') or {}).get('checker_version') or ''))}, SharePoint read {e(str((data.get('run') or {}).get('snapshot_id') or 'none'))}.", S))
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
    record = dict(source_receipt=data.get('source_receipt'), action_snapshot_sha256=data.get('action_snapshot_sha256'), pdf_sha256=hashlib.sha256(pdf).hexdigest(), id=rid, file_hash=data['file_hash'], first_hash=data['first_hash'], job=data['job'], day=data['day'], made_at=made, by=made_by, reason=reason,
                  differed=differs(data), differences=len(data['differences']), changes=len(data['changes']), rows=len(data.get('rows') or []), open=len(data['open']),
                  job_check=data['job_check'].get('status'), path=path, size=len(pdf),
                  filename=f"Change summary {data['job'] or 'report'} {data['day'] or ''}.pdf".replace('  ', ' '))
    with h.store().session() as directory:
        inbox = SpreadsheetInbox(directory)
        # Lock the immutable lineage in the same deterministic order as correction
        # writers. Settings include assignment/source mappings and the shared PDF
        # index, so serialize those writers too before the final snapshot read.
        hashes = sorted({v['file_hash'] for v in _chain(inbox, file_hash)})
        with inbox.connect() as db:
            if inbox.pg is not None:
                for version_hash in hashes:
                    inbox._begin(db, version_hash)
                db.execute('LOCK TABLE settings IN SHARE ROW EXCLUSIVE MODE')
            else:
                inbox._begin(db, file_hash)
            from contextlib import nullcontext
            from copy import copy
            locked = copy(inbox)
            locked.connect = lambda: nullcontext(db)
            latest = collect(locked, file_hash, locked.setting(sharepoint_jobs.KEY))
            if latest['action_snapshot_sha256'] != data['action_snapshot_sha256']:
                raise ValueError('Report or review changed while the summary was prepared; generate a fresh summary.')
            files_for(inbox.root).put(path, pdf)
            recs = locked.setting(RECORDS, []) or []
            locked.set_setting(RECORDS, ([record] + recs)[:3000])
            # Artifact bookkeeping is not report state. Never invalidate a valid
            # approval merely by generating an internal PDF of that exact state.
    return record


def pdf_for(h, rid):
    from hosting.spreadsheet_inbox import SpreadsheetInbox, files_for
    with h.store().session() as directory:
        inbox = SpreadsheetInbox(directory)
        rec = next((r for r in inbox.setting(RECORDS, []) or [] if r.get('id') == rid), None)
        if not rec:
            raise ValueError('Change summary not found.')
        return rec, files_for(inbox.root).get(rec['path'])

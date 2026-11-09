"""Recommendations: the next steps for a report, a week or month, or the whole operation.

Claude writes them from the facts gathered here when AI review is on; otherwise the rules below write them,
so the panel is never empty. Each recommendation can carry an app action (do) that the screen turns into a button.
"""
import json

ACTIONS = ('fix_issues', 'review_fixes', 'fix_shifts', 'recheck_sharepoint', 'send_for_review', 'approve_send', 'open_report', 'open_week')
TIPS = {
    'Duplicate VINs or rows': 'Compare the underlying rows, identifiers, timestamps and source records. A repeated identifier alone does not prove a duplicate inspection; verify what each row represents before proposing a correction.',
    'Scans on the wrong shift': 'The shift is whatever the inspector picks at login. Ask inspectors to pick the shift carefully and to log out at shift change; the verifier re-sorts by scan time either way.',
    'Typos in VINs or part numbers': 'Compare the recorded identifier or part number with the original source and confirm the expected format with the site. An unusual value does not establish a typo or how it was entered.',
    'Did not match SharePoint': 'When the report and SharePoint disagree, fix both: clients are sometimes given SharePoint access and it must match what was emailed.',
    'Totals that did not add up': 'Keep the template\'s total formulas; typed totals drift from the rows. Use yesterday\'s file as the template and change the date once in the header.',
    'Blank or missing values': 'Ask the site to verify missing values against the original source and clarify whether each field is required. Keep unknown values unknown; a blank does not establish its cause or mean zero.',
    'Numbers stored as text or bad values': 'Paste values into the template with Paste Special > Values so quantities stay numbers and add into the totals.',
}


def _n(n, word, plural=None):
    return f'{n:,} {word if n == 1 else plural or word + "s"}'


def _rec(title, why, priority='medium', kind='check', who='you', do=None, target=None):
    return dict(title=title, why=why, priority=priority, kind=kind, who=who, do=do, target=target)


# ---------------------------------------------------------------- facts (what Claude sees) and rules (no AI)
def _review_detail(rv):
    """Finding storage/classification/loading only; never a source-verification verdict."""
    cov, rc, page = (rv.get(k) or {} for k in ('coverage', 'reconcile', 'page'))
    count = lambda x: isinstance(x, int) and not isinstance(x, bool) and x >= 0
    total, listed = cov.get('findings_total'), cov.get('findings_listed')
    storage = (count(total) and count(listed) and total == listed
               and cov.get('not_listed') == 0 and cov.get('truncated') is False)
    parts = [rc.get(k) for k in ('open', 'informational', 'kept_by_person', 'kept_earlier_record',
                               'set_aside_by_claude', 'deferred', 'confirmed', 'corrected')]
    classified = (rc.get('adds_up') is True and count(rc.get('total'))
                  and all(count(n) for n in parts) and sum(parts) == rc['total']
                  and cov.get('all_findings') == rc['total']
                  and count(cov.get('shown')) and count(cov.get('dismissed'))
                  and cov['shown'] + cov['dismissed'] == rc['total'])
    severity_counts = [(rv.get('counts') or {}).get(k) for k in ('high', 'medium', 'low', 'info')]
    classified = classified and all(count(n) for n in severity_counts) and sum(severity_counts) == cov.get('shown')
    loaded = all(isinstance(rv.get(name), list) and count(cov.get(key))
                 and len(rv[name]) == cov[key] for name, key in (('issues', 'shown'), ('dismissed', 'dismissed')))
    if page:
        loaded = loaded and page.get('complete') is not False and all(
            count(page.get(name + '_total')) and len(rv.get(name) or []) == page[name + '_total']
            for name in ('issues', 'dismissed'))
    return dict(version=1, workbook_storage_complete=storage,
                classification_complete=classified, finding_details_complete=loaded)


def report_facts(item, rv, fixes_waiting):
    sp = item.get('sharepoint') or {}
    one = rv.get('sharepoint') or {}   # the one source verdict the header shows (item-by-item reconciliation when there is one)
    bl = item.get('baseline') or {}
    tl = sp.get('timeline') or {}
    return dict(job=rv.get('job'), day=rv.get('day'), status=rv['status'], counts=rv['counts'],
                review_detail=_review_detail(rv),
                numbers={k: rv.get(k) for k in ('scans', 'inspected', 'rejected', 'rejected_unit')},
                issues=[{k: i.get(k) for k in ('severity', 'source', 'where', 'text', 'code', 'verdict') if i.get(k)} for i in rv.get('issues', [])[:40]],
                sharepoint=dict(status=one.get('status') or sp.get('status'), message=one.get('message') or sp.get('message'), captured_at=one.get('captured_at'),
                                matched=one.get('matched') if one.get('matched') is not None else sp.get('matched_rows'), report_rows=sp.get('report_rows'),
                                sharepoint_rows=sp.get('sharepoint_rows'), wrong_shift=sp.get('wrong_shift'), missing_from_report=sp.get('missing_from_report'),
                                not_on_sharepoint=sp.get('not_on_sharepoint')),
                timeline_flags=tl.get('flags') or [], usual_day=dict(status=bl.get('status'), message=bl.get('message')),
                fixes_waiting=fixes_waiting, corrected_from=(item.get('fixed_from') or {}).get('filename'),
                changes_in_this_copy=len((item.get('fixed_from') or {}).get('changes') or []),
                corrected_copy_exists=bool(item.get('fixes_applied')), approved=bool(item.get('approval')),
                sent=any(x.get('sent') for x in item.get('sent') or []), review=(item.get('review_request') or {}).get('status'))


def report_rules(f):
    out = []
    must = f['counts'].get('high', 0)
    if f['sent']:
        out.append(_rec('Already sent to the client', 'Do not change a sent report unless Chris decides to send a correction.', 'low', 'send', 'manager'))
    if f['corrected_copy_exists'] and not f['sent']:
        out.append(_rec('Work on the corrected copy, not this one', 'A corrected copy of this report exists; approve and send that version.', 'high', 'check', 'you', 'open_report'))
    if f['sharepoint'].get('wrong_shift'):
        n = f['sharepoint']['wrong_shift']
        out.append(_rec(f'Put {n} row{"s" if n != 1 else ""} on the right shift', 'Their scan times fall in a different shift than the tab they are on.', 'high', 'fix', 'you', 'fix_shifts'))
    if must:
        out.append(_rec(f'Fix the {must} must-fix issue{"s" if must != 1 else ""} before sending', 'The client must never find an error PPG did not find first.', 'high', 'fix', 'you', 'fix_issues'))
    if f['fixes_waiting']:
        n = f['fixes_waiting']
        out.append(_rec(f'Accept or reject {n} suggested fix{"es" if n != 1 else ""}', 'They are ready to go into a corrected copy in one click.', 'high' if must else 'medium', 'fix', 'you', 'review_fixes'))
    if f['changes_in_this_copy'] and not f['sent']:
        n = f['changes_in_this_copy']
        out.append(_rec(f'Make the same {n} change{"s" if n != 1 else ""} in the SharePoint list', 'Clients are sometimes given SharePoint access, so it must match what was emailed. Ask before editing live data.', 'medium', 'follow_up', 'you'))
    sp = f['sharepoint']
    if sp.get('status') in (None, 'not_checked', 'not_found', 'error', 'stale'):
        out.append(_rec('Check SharePoint again', sp.get('message') or 'The report has not been compared with the SharePoint scans yet.', 'medium', 'check', 'you', 'recheck_sharepoint'))
    elif sp.get('missing_from_report'):
        out.append(_rec(f'Find {_n(sp["missing_from_report"], "scan")} missing from the report', 'They are on SharePoint for this day and shift but not in the report: the list may not have fully loaded, or scans came after the export.', 'high', 'check', 'you'))
    if f.get('vins_on_other_days_count'):
        n, first = f['vins_on_other_days_count'], f['vins_on_other_days'][0]
        out.append(_rec(f'Review {n}{"+" if n >= 20 else ""} normalized identifier/day match{"es" if n != 1 else ""} across reports',
                        f'{first["vin"]} matches an identifier in the {first["day"]} report after normalization. '
                        'A normalized identifier match is not proof of a duplicate inspection. Review the underlying records; do not delete rows based on this match alone.',
                        'high', 'check', 'you'))
    for flag in f['timeline_flags'][:2]:
        out.append(_rec('Ask the site lead about the scanning pattern', flag, 'low', 'follow_up', 'site lead'))
    if f['usual_day'].get('status') == 'unusual':
        out.append(_rec('Confirm the unusual day with the site lead', f['usual_day'].get('message') or 'The volume is far from the job\'s usual day.', 'medium', 'follow_up', 'site lead'))
    if not f['sent'] and not f['corrected_copy_exists']:
        if f['approved']:
            out.append(_rec('Send it to the client', 'It is approved. Reply All on the job\'s last report email from data@ppgquality.com.', 'medium', 'send', 'you', 'approve_send'))
        elif f['review'] == 'waiting':
            out.append(_rec('Waiting for the reviewer', 'Copy the link to the reviewer if they have not seen it.', 'low', 'send', 'manager'))
        elif not must and not f['fixes_waiting']:
            detail = f.get('review_detail') or {}
            complete = all(detail.get(k) is True for k in ('workbook_storage_complete', 'classification_complete', 'finding_details_complete'))
            why = ('No high-severity findings or suggested cell changes are listed in this complete finding review. '
                   'This does not establish source completeness or release readiness.' if complete else
                   'Finding storage, classification or loaded details are incomplete or unknown. '
                   'Review the coverage before concluding that no corrections are required.')
            out.append(_rec('Send it for review', why + ' The report still needs reviewer assessment.',
                            'medium', 'send', 'you', 'send_for_review'))
    return out[:8]


def period_rules(p):
    out = []
    t = p['totals']
    missing = [f for f in p['findings'] if 'no daily report was uploaded' in f['text']]
    dups = [f for f in p['findings'] if 'billed twice' in f['text']]
    diffs = [f for f in p['findings'] if 'SharePoint shows' in f['text']]
    holds = [f for f in p['findings'] if 'must-fix' in f['text']]
    if dups:
        out.append(_rec(f'Check {_n(len(dups), "VIN")} on more than one day before billing', dups[0]['text'], 'high', 'check', 'you', 'open_week'))
    if missing:
        out.append(_rec(f'Upload {_n(len(missing), "missing daily report")}', ', '.join(f['day'] for f in missing[:5]) + ' had SharePoint scans but no report.', 'high', 'fix', 'you'))
    if holds:
        out.append(_rec(f'Clear {_n(len(holds), "daily report")} still on Hold', ', '.join(f['day'] for f in holds[:5]), 'high', 'fix', 'you'))
    if diffs:
        out.append(_rec(f'Reconcile {_n(len(diffs), "day")} with SharePoint', diffs[0]['text'], 'medium', 'check', 'you'))
    unsent = t['reports'] - t['sent']
    if unsent > 0:
        out.append(_rec(f'{_n(unsent, "daily report")} not sent from the verifier', 'Approve and send them, or confirm they went out another way.', 'medium', 'send', 'you'))
    if not out and not t['reports']:
        out.append(_rec(f'Nothing to review this {p["kind"]}', f'No daily reports were uploaded for this {p["kind"]}. Pick another {p["kind"]} with the arrows, or upload the reports.', 'low', 'fix', 'you'))
    elif not out:
        out.append(_rec(f'The {p["kind"]} is clean', 'Every day has a clean, sent report that matches SharePoint. Create the slide show for the client.', 'low', 'send', 'you'))
    return out[:8]


def overview_rules(o):
    out = []
    t = o['totals']
    for c in o['categories'][:2]:
        tip = TIPS.get(c['reason'])
        if tip:
            out.append(_rec(f'Common listed finding: {c["reason"]} ({c["count"]})', tip, 'medium', 'process', 'site lead'))
    holds = sorted([j for j in o['jobs'] if j['open']], key=lambda j: -j['open'])
    if holds:
        out.append(_rec(f'{_n(sum(j["open"] for j in holds), "daily report")} still on Hold', ', '.join(f'{j["job"]} ({j["open"]})' for j in holds[:4]), 'high', 'fix', 'you'))
    if t.get('rejected_missing_reports') or t.get('inspected_missing_reports'):
        out.append(_rec('Overall reject rate is unknown', 'Some selected reports have missing, partial, or incompatible quantities. Review the coverage before using a percentage.', 'high', 'fix', 'you'))
    if t['on_time_pct'] is not None and t['on_time_pct'] < 90:
        timed = t['sent'] - t.get('on_time_unknown', 0)
        out.append(_rec(f'{t["on_time_pct"]:.1f}% of recorded sends with known timing were on time',
                        f'{t["on_time"]} of {timed} recorded sends with known timing were sent by the day after the report date. External sends are not included.',
                        'medium', 'process', 'manager'))
    rated = [j for j in o['jobs'] if j['rate'] is not None and j['days'] >= 2]
    if len(rated) >= 2:
        worst = max(rated, key=lambda j: j['rate'])
        avg = sum(j['rejected'] for j in rated) / max(1, sum(j['inspected'] for j in rated)) * 100
        if worst['rate'] > avg * 1.5:
            out.append(_rec(f'{worst["job"]} has a higher reported reject rate', f'{worst["rate"]:.2f}% against {avg:.2f}% across jobs with complete quantities. Review the underlying records before drawing conclusions.', 'medium', 'follow_up', 'manager', 'open_week', worst['job']))
    unsent = t['reports'] - t['sent']
    if unsent:
        out.append(_rec(f'{_n(unsent, "daily report")} without a recorded verifier send', 'External sending and what a client saw are unknown. Do not interpret a missing send record as proof the client never received it.', 'low', 'process', 'you'))
    if not out:
        out.append(_rec('Review the saved evidence', 'These statistics describe saved reports. They do not establish complete source accuracy or what clients saw.', 'low', 'process', 'you'))
    return out[:8]


# ---------------------------------------------------------------- Claude
SYSTEM = """You advise PPG Quality (Preferred Precision Group), which inspects parts and vehicles at automotive and aerospace plants and sends each client an end-of-shift Excel report. The owner's standard: the report leaves the client no room to question anything; the client must never find an error PPG did not find first.

From <facts> (a {scope}), write up to 7 recommendations: the most useful next steps, most important first. Rules:
- Ground every recommendation in <facts>. Never invent numbers, VINs, names or events. Quote numbers exactly.
- Concrete and doable: say exactly what to do and why it matters. No generic advice.
- Never suggest changing a report that was already sent, or editing live SharePoint data, without saying it needs Chris's yes.
- Never suggest putting scan times, inspector names or internal check results in anything the client receives.
- who: you (the report preparer), site lead, inspector, manager (Chris), or client.
- do: when the app can do it in one click, one of: """ + ', '.join(ACTIONS) + """; otherwise leave it out. target: a job number for open_week, a cell for fix_issues, if useful."""

TOOL = {'name': 'give_recommendations', 'description': 'Return the recommendations.',
        'input_schema': {'type': 'object', 'required': ['recommendations'], 'properties': {'recommendations': {'type': 'array', 'maxItems': 7, 'items': {
            'type': 'object', 'required': ['title', 'why', 'priority'], 'properties': {
                'title': {'type': 'string', 'description': 'Imperative, under 90 characters.'},
                'why': {'type': 'string', 'description': 'One or two sentences, with the numbers from the facts.'},
                'priority': {'type': 'string', 'enum': ['high', 'medium', 'low']},
                'kind': {'type': 'string', 'enum': ['fix', 'check', 'follow_up', 'process', 'send']},
                'who': {'type': 'string'}, 'do': {'type': 'string', 'enum': list(ACTIONS)}, 'target': {'type': 'string'}}}}}}}


def ai(facts, scope, opener=None):
    from hosting import ai_review
    import os
    model = os.environ.get('AI_REVIEW_MODEL') or ai_review.DEFAULT_MODEL
    out = ai_review._post(dict(model=model, max_tokens=3000, system=SYSTEM.replace('{scope}', scope), tools=[TOOL],
                               tool_choice={'type': 'tool', 'name': 'give_recommendations'},
                               messages=[{'role': 'user', 'content': '<facts>\n' + json.dumps(facts, default=str) + '\n</facts>\n\nCall give_recommendations.'}]), opener)
    recs = []
    for r in (ai_review._tool_input(out, 'recommendations').get('recommendations') or [])[:7]:
        if not isinstance(r, dict) or not r.get('title'):
            continue
        recs.append(_rec(' '.join(str(r['title']).split())[:120], ' '.join(str(r.get('why') or '').split())[:400],
                         r.get('priority') if r.get('priority') in ('high', 'medium', 'low') else 'medium',
                         r.get('kind') if r.get('kind') in ('fix', 'check', 'follow_up', 'process', 'send') else 'check',
                         ' '.join(str(r.get('who') or 'you').split())[:30], r.get('do') if r.get('do') in ACTIONS else None,
                         ' '.join(str(r.get('target') or '').split())[:60] or None))
    return recs


def make(facts, scope, rules, use_ai):
    """(items, source). Falls back to the rules when AI is off or fails."""
    from datetime import datetime, timezone
    if use_ai:
        try:
            items = ai(facts, scope)
            if items:
                return dict(items=items, source='ai', made_at=datetime.now(timezone.utc).isoformat())
        except Exception as exc:  # AI trouble never leaves the panel empty
            return dict(items=rules(facts), source='rules', note=f'Dex could not be reached ({str(exc)[:120]}); these come from the checks.',
                        made_at=datetime.now(timezone.utc).isoformat())
    return dict(items=rules(facts), source='rules', made_at=datetime.now(timezone.utc).isoformat())


# ---------------------------------------------------------------- facts for a period and the overview; cached answers
def period_facts(p):
    days = [dict(day=x['day'], report=bool(x['report']), status=(x['report'] or {}).get('status'), sent=bool((x['report'] or {}).get('sent_at')),
                 scans=(x['report'] or {}).get('scans'), sharepoint_scans=(x['sharepoint'] or {}).get('scans')) for x in p['days']]
    return dict(job=p['job'], kind=p['kind'], label=p['label'], totals=p['totals'], findings=p['findings'][:40], days=days)


def overview_facts(o):
    return dict(label=o['label'], totals=o['totals'], jobs=o['jobs'][:40], categories=o['categories'], statistics=o.get('statistics'),
                evidence_limit='Client exposure is unknown. Missing quantities and rates are unknown, not zero. Listed findings are not confirmed unique errors. Corrections are saved records, not proof of prevention.')


def fingerprint(facts):
    import hashlib
    return hashlib.sha256(json.dumps(facts, sort_keys=True, default=str).encode()).hexdigest()[:16]


def answer(cached, facts, rules, scope, ai_wanted, ai_on):
    """(result, to_cache). Claude's last answer is reused while the facts are unchanged; otherwise the rules answer.
    With ai_wanted this calls Claude, so run it outside any DB transaction and save to_cache afterwards."""
    fp = fingerprint(facts)
    if ai_wanted and ai_on:
        out = make(facts, scope, rules, True)
        return dict(out, ai_on=ai_on), (dict(fp=fp, recs=out) if out['source'] == 'ai' else None)
    if cached and cached.get('fp') == fp:
        return dict(cached['recs'], ai_on=ai_on), None
    out = make(facts, scope, rules, False)
    if cached:
        out['note'] = "The data changed since Dex's last look. Ask Dex again for fresh recommendations."
    return dict(out, ai_on=ai_on), None

"""Recommendations: the next steps for a report, a week or month, or the whole operation.

Claude writes them from the facts gathered here when AI review is on; otherwise the rules below write them,
so the panel is never empty. Each recommendation can carry an app action (do) that the screen turns into a button.
"""
import json

ACTIONS = ('fix_issues', 'review_fixes', 'fix_shifts', 'recheck_sharepoint', 'send_for_review', 'approve_send', 'open_report', 'open_week')
TIPS = {
    'Duplicate VINs or rows': 'Most duplicates are the same car scanned on two logins a few minutes apart. Remind inspectors to log out at hand-over and to check the scan went through before scanning again.',
    'Scans on the wrong shift': 'The shift is whatever the inspector picks at login. Ask inspectors to pick the shift carefully and to log out at shift change; the verifier re-sorts by scan time either way.',
    'Typos in VINs or part numbers': 'Typos come from typing instead of scanning. Ask the site to scan every VIN and part label, and to re-scan unreadable stickers rather than type them.',
    'Did not match SharePoint': 'When the report and SharePoint disagree, fix both: clients are sometimes given SharePoint access and it must match what was emailed.',
    'Totals that did not add up': 'Keep the template\'s total formulas; typed totals drift from the rows. Use yesterday\'s file as the template and change the date once in the header.',
    'Blank or missing values': 'Blank VINs or quantities usually mean an unreadable sticker or a skipped field on the tablet. Ask the site to fill the gap the same shift.',
    'Numbers stored as text or bad values': 'Paste values into the template with Paste Special > Values so quantities stay numbers and add into the totals.',
}


def _n(n, word, plural=None):
    return f'{n:,} {word if n == 1 else plural or word + "s"}'


def _rec(title, why, priority='medium', kind='check', who='you', do=None, target=None):
    return dict(title=title, why=why, priority=priority, kind=kind, who=who, do=do, target=target)


# ---------------------------------------------------------------- facts (what Claude sees) and rules (no AI)
def report_facts(item, rv, fixes_waiting):
    sp = item.get('sharepoint') or {}
    bl = item.get('baseline') or {}
    tl = sp.get('timeline') or {}
    return dict(job=rv.get('job'), day=rv.get('day'), status=rv['status'], counts=rv['counts'],
                numbers={k: rv.get(k) for k in ('scans', 'inspected', 'rejected', 'rejected_unit')},
                issues=[{k: i.get(k) for k in ('severity', 'source', 'where', 'text', 'code', 'verdict') if i.get(k)} for i in rv.get('issues', [])[:40]],
                sharepoint=dict(status=sp.get('status'), message=sp.get('message'), matched=sp.get('matched_rows'), report_rows=sp.get('report_rows'),
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
    if sp.get('status') in (None, 'not_checked', 'not_found', 'error'):
        out.append(_rec('Check SharePoint again', sp.get('message') or 'The report has not been compared with the SharePoint scans yet.', 'medium', 'check', 'you', 'recheck_sharepoint'))
    elif sp.get('missing_from_report'):
        out.append(_rec(f'Find {_n(sp["missing_from_report"], "scan")} missing from the report', 'They are on SharePoint for this day and shift but not in the report: the list may not have fully loaded, or scans came after the export.', 'high', 'check', 'you'))
    if f.get('vins_on_other_days_count'):
        n, first = f['vins_on_other_days_count'], f['vins_on_other_days'][0]
        out.append(_rec(f'Check {n}{"+" if n >= 20 else ""} {"VIN" if n == 1 else "VINs"} already reported on another day',
                        f'{first["vin"]} is also in the {first["day"]} report. An old file re-sent, or a car counted (and billed) twice?', 'high', 'check', 'you'))
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
            out.append(_rec('Send it for review', 'Nothing must be fixed. Until you are signed off, the report goes to Chris before the client.', 'medium', 'send', 'you', 'send_for_review'))
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
    if not out:
        out.append(_rec(f'The {p["kind"]} is clean', 'Every day has a clean, sent report that matches SharePoint. Create the slide show for the client.', 'low', 'send', 'you'))
    return out[:8]


def overview_rules(o):
    out = []
    t = o['totals']
    for c in o['categories'][:2]:
        tip = TIPS.get(c['reason'])
        if tip:
            out.append(_rec(f'Most common catch: {c["reason"]} ({c["count"]})', tip, 'medium', 'process', 'site lead'))
    holds = sorted([j for j in o['jobs'] if j['open']], key=lambda j: -j['open'])
    if holds:
        out.append(_rec(f'{_n(sum(j["open"] for j in holds), "daily report")} still on Hold', ', '.join(f'{j["job"]} ({j["open"]})' for j in holds[:4]), 'high', 'fix', 'you'))
    if t['on_time_pct'] is not None and t['on_time_pct'] < 90:
        out.append(_rec(f'Only {t["on_time_pct"]:.0f}% of reports went out on time', 'On time means sent by the day after the shift. Look at the jobs with the lowest on-time count.', 'medium', 'process', 'manager'))
    rated = [j for j in o['jobs'] if j['rate'] is not None and j['days'] >= 2]
    if len(rated) >= 2:
        worst = max(rated, key=lambda j: j['rate'])
        avg = sum(j['rejected'] for j in rated) / max(1, sum(j['inspected'] for j in rated)) * 100
        if worst['rate'] > avg * 1.5:
            out.append(_rec(f'{worst["job"]} rejects far more than the other jobs', f'{worst["rate"]:.1f}% against {avg:.1f}% across jobs. Worth a look at the defect reasons with the client.', 'medium', 'follow_up', 'manager', 'open_week', worst['job']))
    unsent = t['reports'] - t['sent']
    if unsent:
        out.append(_rec(f'{_n(unsent, "daily report")} not sent from the verifier', 'Sending from the verifier records who approved and sent what, which these numbers rely on.', 'low', 'process', 'you'))
    if not out:
        out.append(_rec('Nothing stands out', 'Keep going: every report was checked and sent.', 'low', 'process', 'you'))
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
            return dict(items=rules(facts), source='rules', note=f'Claude could not be reached ({str(exc)[:120]}); these come from the checks.',
                        made_at=datetime.now(timezone.utc).isoformat())
    return dict(items=rules(facts), source='rules', made_at=datetime.now(timezone.utc).isoformat())


# ---------------------------------------------------------------- facts for a period and the overview; cached answers
def period_facts(p):
    days = [dict(day=x['day'], report=bool(x['report']), status=(x['report'] or {}).get('status'), sent=bool((x['report'] or {}).get('sent_at')),
                 scans=(x['report'] or {}).get('scans'), sharepoint_scans=(x['sharepoint'] or {}).get('scans')) for x in p['days']]
    return dict(job=p['job'], kind=p['kind'], label=p['label'], totals=p['totals'], findings=p['findings'][:40], days=days)


def overview_facts(o):
    return dict(label=o['label'], totals=o['totals'], jobs=o['jobs'][:40], categories=o['categories'])


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
        out['note'] = "The data changed since Claude's last look. Ask Claude again for fresh recommendations."
    return dict(out, ai_on=ai_on), None

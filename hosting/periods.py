"""Weekly and monthly view of a job: the daily reports saved in the verifier and the job's SharePoint scans,
day by day, with checks for the period, a check of an uploaded weekly/monthly report file, and the numbers
used to build the period's dashboard and slide show."""
import calendar
import re
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from statistics import median

KINDS = ('week', 'month')


def bounds(kind, day):
    """(start, end, label) for the week (Monday to Sunday) or month containing day."""
    d = date.fromisoformat(str(day)[:10])
    if kind == 'month':
        start = d.replace(day=1)
        end = d.replace(day=calendar.monthrange(d.year, d.month)[1])
        return start, end, start.strftime('%B %Y')
    start = d - timedelta(days=d.weekday())
    end = start + timedelta(days=6)
    same_month = start.month == end.month
    label = f'Week of {start.strftime("%B")} {start.day}' + (f' to {end.day}' if same_month else f' to {end.strftime("%B")} {end.day}') + f', {end.year}'
    return start, end, label


def key(job, kind, start, what):
    return f'period|{job.upper()}|{kind}|{start.isoformat()}|{what}'


def parse_key(k):
    m = re.fullmatch(r'period\|([A-Z0-9 _.-]{1,60})\|(week|month)\|(\d{4}-\d{2}-\d{2})\|(dashboard|slides)', k or '')
    return (m.group(1), m.group(2), date.fromisoformat(m.group(3)), m.group(4)) if m else None


def _num(v):
    try:
        return float(str(v).replace(',', '')) if v not in (None, '', '—') else None
    except ValueError:
        return None


def pick_daily(rows):
    """Of several uploads for one job and day, the one that counts: sent, else approved, else a corrected copy, else newest."""
    def rank(r):
        rv = r.get('review') or {}
        return (bool(rv.get('sent_at')), bool(rv.get('approved')), bool(rv.get('fixed_from')), r.get('received_at') or '')
    return max(rows, key=rank)


def sharepoint_days(job, start, end, schedule=None):
    """{day: {scans, inspected, rejected}} from the job's SharePoint list, by report day (shift times aware)."""
    from hosting import sharepoint
    from hosting.source_compare import parse_export, SourceError
    from hosting.baseline import stats_from_rows
    if not sharepoint.configured():
        return {}, 'SharePoint is not connected.'
    try:
        data = sharepoint._list_items(job)
    except sharepoint.SharePointError as exc:
        return {}, str(exc)
    if not data:
        return {}, f'No "Data #{job}" list was found on SharePoint.'
    _, lst, names, items = data
    by_day = defaultdict(list)
    for it in items:
        d = sharepoint._item_day(it, schedule)
        if d and start.isoformat() <= d <= end.isoformat():
            by_day[d].append(it)
    out = {}
    for d, rows in by_day.items():
        try:
            stats = stats_from_rows(parse_export(f'{job} {d}.csv', sharepoint._items_csv(names, rows)))
        except SourceError:
            continue
        out[d] = {k: (float(v) if v is not None else None) for k, v in stats.items()}
    return out, f'SharePoint list "{lst.get("displayName")}"'


def assemble(job, kind, start, end, label, reports, sp_days, sp_note):
    """The period, day by day, with its checks."""
    days, findings = [], []
    sev = lambda s, d, t: findings.append(dict(severity=s, day=d, text=t))
    last = min(end, datetime.now(timezone.utc).date())
    for i in range((end - start).days + 1):
        d = (start + timedelta(days=i)).isoformat()
        rep, sp = reports.get(d), sp_days.get(d)
        days.append(dict(day=d, report=rep, sharepoint=sp))
        if d > last.isoformat():
            continue
        if sp and sp.get('scans') and not rep:
            sev('high', d, f'SharePoint has {int(sp["scans"])} scans but no daily report was uploaded for this day.')
        if rep:
            if rep['status'] == 'hold':
                sev('high', d, f'The daily report still has {rep["counts"].get("high", 0)} must-fix issue(s).')
            elif rep['status'] == 'check':
                sev('low', d, f'The daily report has {rep["counts"].get("medium", 0) + rep["counts"].get("low", 0)} item(s) to look at.')
            if not rep.get('sent_at'):
                sev('medium', d, 'Approved but not sent to the client from the verifier.' if rep.get('approved') else 'Not approved or sent from the verifier yet.')
            if sp:
                for field, name in (('scans', 'scans'), ('inspected', 'parts inspected'), ('rejected', 'rejects')):
                    a, b = rep.get(field), sp.get(field)
                    if a is not None and b is not None and abs(a - b) > 0.0001:
                        sev('high', d, f'The daily report shows {a:,.0f} {name}; SharePoint shows {b:,.0f}.')
    def tot(field):
        vals = [x['report'][field] if x['report'] and x['report'].get(field) is not None else (x['sharepoint'] or {}).get(field)
                for x in days]
        vals = [v for v in vals if v is not None]
        return sum(vals) if vals else None
    worked = [x for x in days if (x['report'] and x['report'].get('scans')) or (x['sharepoint'] and x['sharepoint'].get('scans'))]
    scans_by_day = [(x['day'], (x['report'] or {}).get('scans') or (x['sharepoint'] or {}).get('scans') or 0) for x in worked]
    if len(scans_by_day) >= 3:
        mid = median(v for _, v in scans_by_day)
        for d, v in scans_by_day:
            if mid and v < mid * 0.5:
                sev('medium', d, f'Only {v:,.0f} scans, under half of this period\'s usual {mid:,.0f}. A short shift, or missing scans?')
    if not worked:
        sev('medium', None, f'No daily reports and no SharePoint scans for {job} in this {kind}.')
    inspected, rejected = tot('inspected'), tot('rejected')
    findings.sort(key=lambda f: ({'high': 0, 'medium': 1, 'low': 2}[f['severity']], f['day'] or ''))
    return dict(job=job, kind=kind, start=start.isoformat(), end=end.isoformat(), label=label, sharepoint_source=sp_note,
                days=days, findings=findings,
                totals=dict(days_worked=len(worked), reports=sum(1 for x in days if x['report']), scans=tot('scans'),
                            inspected=inspected, rejected=rejected,
                            rate=(rejected / inspected * 100) if inspected and rejected is not None else None,
                            sent=sum(1 for x in days if x['report'] and x['report'].get('sent_at'))))


def check_file(filename, raw, period):
    """Check an uploaded weekly or monthly report against the period's dailies and SharePoint, day by day."""
    from hosting.spreadsheets import report_records, inspect_spreadsheet
    from hosting.review_summary import summarize
    recs = [r for r in report_records(filename, raw) if r.get('table_kind') == 'detail']
    own = summarize(dict(inspect_spreadsheet(filename, raw), filename=filename), with_issues=True)
    findings = []
    add = lambda s, t, d=None: findings.append(dict(severity=s, day=d, text=t))
    per_day = defaultdict(lambda: dict(rows=0, inspected=0.0, rejected=0.0))
    dated = 0
    for r in recs:
        d = r.get('day').isoformat() if r.get('day') else None
        if d:
            dated += 1
        k = per_day[d]
        k['rows'] += 1
        k['inspected'] += _num((r.get('values') or {}).get('inspected')) or 0
        k['rejected'] += _num((r.get('values') or {}).get('rejected')) or 0
    expected = {}
    for x in period['days']:
        src = x['report'] or x['sharepoint']
        if src:
            expected[x['day']] = src
    if not recs:
        add('high', 'No inspection rows were found in this file.')
    elif dated:
        for d, got in sorted((d, v) for d, v in per_day.items() if d):
            if not (period['start'] <= d <= period['end']):
                add('high', f'{got["rows"]} row(s) are dated {d}, outside {period["label"]}.', d)
                continue
            want = expected.get(d)
            if not want:
                add('high', f'The file has {got["rows"]} row(s) for this day, but there is no daily report or SharePoint data for it.', d)
                continue
            for field, name in (('inspected', 'parts inspected'), ('rejected', 'rejects')):
                if want.get(field) is not None and abs(got[field] - want[field]) > 0.0001:
                    add('high', f'The file shows {got[field]:,.0f} {name}; the day\'s report / SharePoint shows {want[field]:,.0f}.', d)
        for d, want in sorted(expected.items()):
            if d not in per_day and (want.get('scans') or want.get('inspected')):
                add('high', f'This day is missing from the file (the daily data shows {want.get("inspected") or 0:,.0f} parts inspected).', d)
    total_in = sum(v['inspected'] for v in per_day.values())
    total_rej = sum(v['rejected'] for v in per_day.values())
    t = period['totals']
    if t.get('inspected') is not None and recs and abs(total_in - t['inspected']) > 0.0001:
        add('high', f'Total parts inspected in the file: {total_in:,.0f}; the {period["kind"]}\'s daily data adds up to {t["inspected"]:,.0f}.')
    if t.get('rejected') is not None and recs and abs(total_rej - t['rejected']) > 0.0001:
        add('high', f'Total rejects in the file: {total_rej:,.0f}; the {period["kind"]}\'s daily data adds up to {t["rejected"]:,.0f}.')
    for i in [i for i in own.get('issues') or [] if i['severity'] in ('high', 'medium')][:40]:
        add(i['severity'], ('File check: ' + (f'{i["where"]}: ' if i.get('where') else '') + i['text']))
    return dict(filename=filename, checked_at=datetime.now(timezone.utc).isoformat(), rows=len(recs), dated_rows=dated,
                inspected=total_in, rejected=total_rej, findings=findings,
                status='hold' if any(f['severity'] == 'high' for f in findings) else 'check' if findings else 'clear')


def data_for(period, dailies):
    """Numbers for the period's dashboard and slide show. dailies: hosting.dashboard.data_for() of each day's report."""
    shifts = defaultdict(lambda: dict(rows=0, inspected=0.0, rejected=0.0))
    reasons, parts = Counter(), defaultdict(lambda: dict(rows=0, inspected=0.0, rejected=0.0))
    pass_fail = any(d.get('pass_fail') for d in dailies)
    for d in dailies:
        for s in d.get('shifts') or []:
            k = shifts[s['shift']]
            for f in ('rows', 'inspected', 'rejected'):
                k[f] += s.get(f) or 0
        for r in d.get('reasons') or []:
            reasons[r['reason']] += r['count']
        for p in d.get('parts') or []:
            k = parts[p['part']]
            for f in ('rows', 'inspected', 'rejected'):
                k[f] += p.get(f) or 0
    days = []
    for x in period['days']:
        src = x['report'] or x['sharepoint'] or {}
        days.append(dict(day=x['day'], scans=src.get('scans'), inspected=src.get('inspected'), rejected=src.get('rejected'),
                         source='daily report' if x['report'] else ('SharePoint' if x['sharepoint'] else None)))
    t = period['totals']
    return dict(job=period['job'], kind=period['kind'], label=period['label'], start=period['start'], end=period['end'], pass_fail=pass_fail,
                totals=dict(days_worked=t['days_worked'], scans=t['scans'], inspected=t['inspected'], rejected=t['rejected'], rate=t['rate']),
                days=days, shifts=[dict(shift=k, **v) for k, v in sorted(shifts.items())],
                reasons=[dict(reason=k, count=v) for k, v in reasons.most_common(12)],
                parts=[dict(part=k, **v) for k, v in sorted(parts.items(), key=lambda kv: -kv[1]['inspected'])[:20]],
                generated=datetime.now(timezone.utc).strftime('%Y-%m-%d'))


def cross_day(vins_by_day):
    """Findings for VINs that are in more than one day's report: possibly inspected or billed twice."""
    days_of = defaultdict(list)
    for d, vins in sorted(vins_by_day.items()):
        for v in vins:
            days_of[v].append(d)
    dup = {v: ds for v, ds in days_of.items() if len(ds) > 1}
    out = []
    for v, ds in sorted(dup.items(), key=lambda kv: kv[1])[:15]:
        names = ' and '.join(date.fromisoformat(d).strftime('%a %b ') + str(date.fromisoformat(d).day) for d in ds)
        out.append(dict(severity='high', day=ds[-1], text=f'{v} is in the daily reports for {names}. Inspected twice, or billed twice?'))
    if len(dup) > 15:
        out.append(dict(severity='high', day=None, text=f'{len(dup) - 15} more VIN(s) appear in more than one day\'s report.'))
    return out


def day_scans(job, day, schedule=None):
    """SharePoint activity for one job: scans for the day and the day before, last scan time, scans per shift."""
    from hosting import sharepoint
    from hosting.shifts import local_time, assign
    if not sharepoint.configured():
        return None
    data = sharepoint._list_items(job)
    if not data:
        return dict(found=False)
    d0 = date.fromisoformat(day)
    prev = (d0 - timedelta(days=1)).isoformat()
    out = dict(found=True, list=data[1].get('displayName'), today=0, yesterday=0, last=None, shifts={})
    last = None
    for it in data[3]:
        d = sharepoint._item_day(it, schedule)
        if d == day:
            out['today'] += 1
            local = local_time(sharepoint._stamp(it))
            if local:
                last = max(last, local) if last else local
                sh = assign(local, schedule)[0] if schedule else None
                if sh:
                    out['shifts'][sh['name']] = out['shifts'].get(sh['name'], 0) + 1
        elif d == prev:
            out['yesterday'] += 1
    out['last'] = last.isoformat() if last else None
    return out

"""The SharePoint job list (the "PPG SharePoint — Clients & Jobs" workbook): which client and site holds each job, and
whether it is active. Every report's job is checked against it: found, written differently, or not in SharePoint."""
import io
import re
from datetime import date, datetime

KEY = 'sharepoint_jobs'


class JobListError(ValueError):
    pass


def _s(v):
    return '' if v is None else str(v).strip()


def _d(v):
    if isinstance(v, (datetime, date)):
        return v.date().isoformat() if isinstance(v, datetime) else v.isoformat()
    return _s(v) or None


def parse(raw):
    """Jobs from the workbook's 'All Jobs' tab (header row: Client, Location, SharePoint Site, Job # ...)."""
    from openpyxl import load_workbook
    try:
        wb = load_workbook(io.BytesIO(raw), read_only=True, data_only=True)
    except Exception as exc:
        raise JobListError('This file could not be opened. Upload the "PPG SharePoint — Clients & Jobs" Excel file.') from exc
    ws = next((w for w in wb.worksheets if w.title.strip().lower() == 'all jobs'), None) or wb.worksheets[0]
    rows = list(ws.iter_rows(values_only=True))
    head_at = next((n for n, r in enumerate(rows[:30]) if r and any(_s(c).lower().startswith('job #') for c in r)), None)
    if head_at is None:
        raise JobListError('No "Job #" column was found. Upload the "PPG SharePoint — Clients & Jobs" Excel file (its All Jobs tab).')
    head = [_s(c).lower() for c in rows[head_at]]
    col = lambda *names: next((i for i, h in enumerate(head) if any(h.startswith(n) for n in names)), None)
    c = dict(client=col('client'), location=col('location'), site=col('sharepoint site'), job=col('job #'), type=col('type'), status=col('status'),
             entries=col('entries'), first=col('first entry'), last=col('last entry'), notes=col('notes'))
    as_of = next((_d(r[1]) for r in rows[:head_at] if r and _s(r[0]).lower().startswith('as-of') and len(r) > 1), None)
    jobs = []
    for r in rows[head_at + 1:]:
        get = lambda k: r[c[k]] if c[k] is not None and c[k] < len(r) else None
        job = _s(get('job'))
        if not job:
            continue
        notes = _s(get('notes'))
        jobs.append(dict(client=_s(get('client')), location=_s(get('location')), site=_s(get('site')), job=job, type=_s(get('type')) or 'Job',
                         status=_s(get('status')), entries=get('entries') if isinstance(get('entries'), (int, float)) else None,
                         first=_d(get('first')), last=_d(get('last')), notes=notes,
                         aliases=sorted(set(re.findall(r'says (\d{4})', notes)))))
    if not jobs:
        raise JobListError('The job list is empty.')
    return dict(as_of=as_of, jobs=jobs)


def _numbers(text):
    return set(re.findall(r'(?<!\d)(\d{4})(?!\d)', text or ''))


def lookup(job, catalog):
    """{status: found | differs | ambiguous | not_found | no_list | no_job, match, matches, message}"""
    if not catalog or not catalog.get('jobs'):
        return dict(status='no_list', message='No SharePoint job list loaded yet (Checks & shifts).')
    if not job:
        return dict(status='no_job', message='The report has no job number, so it could not be matched to SharePoint.')
    real = [j for j in catalog['jobs'] if (j.get('type') or 'Job') == 'Job']
    up = job.strip().upper()
    exact = [j for j in real if j['job'].upper() == up]
    def say(j):
        state = (j.get('status') or '').lower()
        tail = f" Last tablet entry {j['last']}." if j.get('last') else ''
        return f"{j['client']}{', ' + j['location'] if j.get('location') and j['location'] != '—' else ''} (site {j['site']}), status {state or 'unknown'}.{tail}"
    if len(exact) == 1:
        j = exact[0]
        warn = '' if (j.get('status') or '').lower() == 'active' else ' It is not active in SharePoint, so check the report is for the right job.'
        return dict(status='found', match=j, inactive=bool(warn), message=f"Job {j['job']} is in SharePoint: {say(j)}{warn}")
    if len(exact) > 1:
        return dict(status='ambiguous', matches=exact, message=f"Job {job} is on {len(exact)} SharePoint sites: " + '; '.join(f"{j['site']} ({j.get('entries') or 0} rows)" for j in exact) + '. Check which one the report belongs to.')
    nums = _numbers(up)
    near = [j for j in real if nums & (_numbers(j['job'].upper()) | set(j.get('aliases') or []))]
    if len(near) > 1:   # the one sharing the most name parts wins (2232-SUNWAY-GRAMMS -> 2222-SUNWAY-GRAMMS)
        parts = lambda t: {x for x in re.split(r'[^A-Z0-9]+', t.upper()) if x}
        score = lambda j: len(parts(up) & (parts(j['job']) | set(j.get('aliases') or [])))
        best = max(score(j) for j in near)
        top = [j for j in near if score(j) == best]
        if len(top) == 1 and best >= 2:
            near = top
    if len(near) == 1:
        j = near[0]
        return dict(status='differs', match=j, message=f"The report says {job} but the SharePoint list is named {j['job']}: {say(j)} Use the SharePoint name on the report.")
    if near:
        return dict(status='ambiguous', matches=near[:6], message=f"Job {job} is not in SharePoint as written. Close matches: " + ', '.join(j['job'] for j in near[:6]) + '.')
    return dict(status='not_found', message=f"Job {job} is not in the SharePoint job list ({catalog.get('as_of') or 'latest'}). Check the job number.")

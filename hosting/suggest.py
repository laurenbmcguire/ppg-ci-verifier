"""Suggestions for blank details: the most likely value from what is around it.

A blank "Paying client" is most likely the client the job belongs to; a blank worksite is that client's location; a
blank date is the report day; a blank shift is the shift tab it sits on; a blank area or condition is what the rows
around it say. Suggestions are never made automatically: a person (or Fix with AI) accepts them.
"""
import re
from collections import Counter

DETAIL_LABELS = [   # label text (lower case, no punctuation) -> what goes in the blank next to it
    (re.compile(r'^(paying )?(client|customer)( name)?$|^bill to$|^billed to$'), 'client'),
    (re.compile(r'^(worksite|work site)( location)?$|^site location$|^location$|^plant$'), 'worksite'),
    (re.compile(r'^(project|job)( number| no| #)?$|^project id$'), 'job'),
    (re.compile(r'^(report )?date$|^inspection date$|^date of report$'), 'day'),
    (re.compile(r'^prepared by( ppg)?$|^inspection company$|^vendor$'), 'prepared_by'),
    (re.compile(r'^(job|service|inspection|work) type$|^type of (job|service|work|inspection)$'), 'job_type'),
    (re.compile(r'^(job |project |work )?description$|^scope( of work)?$'), 'description'),
]


def context(job, day, catalog=None, client_name=None, assignment=None, known=None):
    """What is known about the report: job, day, client and where the work is. assignment = the settled client/job
    (registry, source links or a person); known = values found elsewhere in the report (job type, description)."""
    from hosting import sharepoint_jobs
    found = sharepoint_jobs.lookup(job, catalog) if job else {}
    m = found.get('match') or {}
    a = assignment or {}
    settled = a.get('status') in ('manual', 'verified', 'matched', 'source')
    client = client_name or (a.get('client') if settled else None) or m.get('client') or None
    loc = m.get('location') if m.get('location') not in (None, '', '—') else None
    known = known or {}
    return dict(job=(a.get('job') if settled else None) or (m.get('job') if found.get('status') in ('found', 'differs') else None) or job,
                day=day, client=client, worksite=', '.join(x for x in (client, loc) if x) or m.get('site') or None, prepared_by='PPG Quality',
                job_type=known.get('job_type') or m.get('type') if (m.get('type') or 'Job') != 'Job' else known.get('job_type'),
                description=known.get('description') or m.get('notes') or None, verified=settled)


def header(ctx):
    """Report header fields for a job, filled from what is known."""
    return {k: v for k, v in dict(customer=ctx.get('client'), worksite=ctx.get('worksite'), prepared_by=ctx.get('prepared_by')).items() if v}


def _norm(v):
    return re.sub(r'[^a-z0-9# ]', '', str(v or '').lower()).strip()


def _is_value(v, what):
    """Could v be the value of a "what" field? Labels, headers and formulas never are; a date field needs a date."""
    if isinstance(v, str):
        t = v.strip()
        if not t or t.startswith('='):
            return False
        n = _norm(t)
        if any(rx.search(n) for rx, _ in DETAIL_LABELS) or n in HEADER_WORDS:
            return False
        if what == 'day':
            return bool(re.search(r'\d{4}-\d{2}-\d{2}|\d{1,2}/\d{1,2}/\d{2,4}|(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]* \d{1,2}', t.lower()))
        return True
    if what == 'day':
        import datetime as _dt
        return isinstance(v, (_dt.date, _dt.datetime)) or (isinstance(v, (int, float)) and 20000 < v < 80000)
    return v is not None


HEADER_WORDS = {'shift', 'area', 'auditor', 'identifier', 'part number', 'part name', 'quantity', 'quantity inspected', 'ok', 'nok', 'reworked',
                'comments', 'condition', 'supplier', 'date', 'time', 'inspected', 'rejected', 'defect', 'recorded defect', 'project number'}


def blank_details(sources, ctx, add, col_letter):
    """Label/value blocks ("Paying Client" | blank): suggest from the client, job and day, or from a filled twin label."""
    empty = lambda v: v in (None, '') or (isinstance(v, str) and not v.strip())
    found = {}
    for sheet, rows in sources:
        spots, offsets = [], Counter()
        for r, row in enumerate(rows[:80]):   # details sit at the top of a sheet
            texts = [v for v in row if isinstance(v, str) and v.strip()]
            if len(texts) >= 4 and sum(1 for v in texts if len(v) <= 30 and not any(ch.isdigit() for ch in v)) >= 4:
                continue   # a table's header row ("Project number | Date | Shift | ..."): labels, never label/value pairs
            for c, val in enumerate(row[:12]):
                label = _norm(val)
                if not label or len(label) > 40 or not isinstance(val, str):
                    continue
                what = next((w for rx, w in DETAIL_LABELS if rx.search(label)), None)
                if not what:
                    continue
                k = next((k for k in (1, 2, 3) if c + k < len(row) and not empty(row[c + k])), None)
                if k and not _is_value(row[c + k], what):
                    k = None   # the next cell is another label, a header or a formula: not this field's value
                if k:
                    offsets[k] += 1
                spots.append(dict(sheet=sheet, row=r + 1, c=c, label=val.strip(), what=what, filled=bool(k), value=row[c + k] if k else None))
        step = offsets.most_common(1)[0][0] if offsets else 1   # where values sit on this tab (next cell, or two over)
        for sp in spots:
            sp['col'] = sp['c'] + step + 1
            found.setdefault(sp['what'], []).append(sp)
    for what, spots in found.items():
        twin = next((s['value'] for s in spots if s['filled'] and isinstance(s['value'], str) and s['value'].strip() and _is_value(s['value'], what)), None)   # "Client" filled, "Paying Client" blank
        guess = twin or (ctx.get('day') if what == 'day' else ctx.get(what))
        if not guess:
            continue
        why = {'client': f"The client this job belongs to{' (the other client field says so)' if twin else ''}.",
               'worksite': 'Where the client is.', 'job': 'The job this report is for.', 'day': 'The report day.',
               'prepared_by': 'Reports are prepared by PPG Quality.', 'job_type': 'The service recorded on this report\'s rows.',
               'description': 'What this job inspects, from the report\'s rows.'}[what]
        known = bool(twin) or what in ('day', 'prepared_by') or (ctx.get('verified') and what in ('client', 'job', 'worksite')) or what in ('job_type', 'description')
        for s in spots:
            if not s['filled']:
                # data the app already has (the settled client and job, the report's own rows) is filled in, not just suggested
                add(s['sheet'], f"{col_letter(s['col'] - 1)}{s['row']}", guess, f'"{s["label"]}" is blank. Filled in: {guess}. {why}',
                    'Known detail' if known else 'Suggested', 'high' if known else 'medium', sure=True if known else None)


def blank_columns(detail, ctx, add, text_cell):
    """A blank in a column that is filled on nearly every other row: the value on the rows around it."""
    for kind, label in (('shift', 'Shift'), ('area', 'Area'), ('auditor', 'Auditor'), ('condition', 'Condition')):
        rows = [r for r in detail if kind in r['labels']]
        filled = [r for r in rows if r['labels'].get(kind)]
        if len(rows) < 4 or len(filled) < 0.75 * len(rows) or len(filled) == len(rows):
            continue
        for r in rows:
            if r['labels'].get(kind):
                continue
            same = [x for x in filled if x['sheet'] == r['sheet']]
            near = sorted(same, key=lambda x: abs(x['row'] - r['row']))[:2]
            guess, why = None, ''
            if near and len({x['labels'][kind] for x in near}) == 1:
                guess, why = near[0]['labels'][kind], 'the rows around it say the same'
            elif same:
                guess, n = Counter(x['labels'][kind] for x in same).most_common(1)[0]
                why = f'used on {n} of {len(same)} rows on this tab'
            if kind == 'shift':
                m = re.match(r'\s*(\d)', str(r['sheet']))
                if m:
                    guess, why = m.group(1), f'it is on the {r["sheet"]} tab'
            if guess:
                add(r['sheet'], text_cell(r, kind), guess, f'{label} is blank. Suggested: {guess} ({why}).', 'Suggested', 'medium')

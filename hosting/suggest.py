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
]


def context(job, day, catalog=None, client_name=None):
    """What is known about the report: job, day, client and where the work is."""
    from hosting import sharepoint_jobs
    found = sharepoint_jobs.lookup(job, catalog) if job else {}
    m = found.get('match') or {}
    client = client_name or m.get('client') or None
    loc = m.get('location') if m.get('location') not in (None, '', '—') else None
    return dict(job=(m.get('job') if found.get('status') in ('found', 'differs') else None) or job, day=day, client=client,
                worksite=', '.join(x for x in (client, loc) if x) or m.get('site') or None, prepared_by='PPG Quality')


def header(ctx):
    """Report header fields for a job, filled from what is known."""
    return {k: v for k, v in dict(customer=ctx.get('client'), worksite=ctx.get('worksite'), prepared_by=ctx.get('prepared_by')).items() if v}


def _norm(v):
    return re.sub(r'[^a-z0-9# ]', '', str(v or '').lower()).strip()


def blank_details(sources, ctx, add, col_letter):
    """Label/value blocks ("Paying Client" | blank): suggest from the client, job and day, or from a filled twin label."""
    empty = lambda v: v in (None, '') or (isinstance(v, str) and not v.strip())
    found = {}
    for sheet, rows in sources:
        spots, offsets = [], Counter()
        for r, row in enumerate(rows[:80]):   # details sit at the top of a sheet
            for c, val in enumerate(row[:12]):
                label = _norm(val)
                if not label or len(label) > 40 or not isinstance(val, str):
                    continue
                what = next((w for rx, w in DETAIL_LABELS if rx.search(label)), None)
                if not what:
                    continue
                k = next((k for k in (1, 2, 3) if c + k < len(row) and not empty(row[c + k])), None)
                if k:
                    offsets[k] += 1
                spots.append(dict(sheet=sheet, row=r + 1, c=c, label=val.strip(), what=what, filled=bool(k), value=row[c + k] if k else None))
        step = offsets.most_common(1)[0][0] if offsets else 1   # where values sit on this tab (next cell, or two over)
        for sp in spots:
            sp['col'] = sp['c'] + step + 1
            found.setdefault(sp['what'], []).append(sp)
    for what, spots in found.items():
        twin = next((s['value'] for s in spots if s['filled'] and isinstance(s['value'], str) and s['value'].strip()), None)   # "Client" filled, "Paying Client" blank
        guess = twin or (ctx.get('day') if what == 'day' else ctx.get(what))
        if not guess:
            continue
        why = {'client': f"Most likely the client this job belongs to{' (the other client field says so)' if twin else ''}.",
               'worksite': 'Most likely where the client is.', 'job': 'The job this report is for.', 'day': 'The report day.',
               'prepared_by': 'Reports are prepared by PPG Quality.'}[what]
        for s in spots:
            if not s['filled']:
                add(s['sheet'], f"{col_letter(s['col'] - 1)}{s['row']}", guess, f'"{s["label"]}" is blank. Suggested: {guess}. {why}', 'Suggested', 'medium')


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

"""Which client and job a report belongs to: a verified registry keyed by SharePoint site/list, plus evidence.

Job labels repeat across client sites (2207 is a list on both SLAlabama_AlexanderCity and Hyundai_Montgomery),
so a job number or a filename alone never decides the client. Evidence, strongest first:
  1. a person's assignment for this exact file (kept with an audit trail)
  2. the SharePoint site in the workbook's own source record URLs
  3. the client named in the report title, then the filename (hints only)
  4. the verified registry (pinned site/list per client and job)
  5. the client job lists SharePoint shows (unverified)
When the evidence disagrees or several clients fit, the report needs a person to choose and stays on hold.
"""
import re
from datetime import datetime, timezone

REGISTRY_KEY = 'job_registry'
SNAPSHOT_KEY = 'clients_snapshot'
LOG_KEY = 'assignment_log'
LOG_LIMIT = 2000

# Verified by the operations owner (2026-11-07). site/list IDs are stable SharePoint identifiers.
SEED = [
    dict(client_key='SLAlabama', client='SL Alabama', site='SLAlabama_AlexanderCity', location='Alexander City', job='2207',
         list_id='32a42ee0-4592-4d73-b893-1b98970b3d4b', verified=True, note='SL Alabama 2207 source list.'),
    dict(client_key='Hyundai', client='Hyundai', site='Hyundai_Montgomery', location='Montgomery', job='2207',
         list_id='47f89ceb-d8b9-45c3-932a-66d46e3f4272', verified=True, separate=True,
         note='A separate Hyundai list with the same label. It never overrides the SL Alabama 2207 list.'),
    # Pratt & Whitney sources have no verified numeric job: each source is shown under its own name, never combined.
    dict(client_key='PrattWhitney', client='Pratt & Whitney', source='Audits_KN', job=None, verified=True, note='No verified job number.'),
    dict(client_key='PrattWhitney', client='Pratt & Whitney', source='DCA Main Audit', job=None, verified=True, note='No verified job number.'),
    dict(client_key='PrattWhitney', client='Pratt & Whitney', source='DCA Multi-Pack Audit', job=None, verified=True, note='No verified job number.'),
]
RESOLVED = ('manual', 'verified', 'matched', 'source')
NEEDS_PERSON = ('ambiguous', 'conflict', 'unmatched')


def _n(text):
    return re.sub(r'[^a-z0-9]+', '', str(text or '').lower())


def _nums(job):
    return set(re.findall(r'\d{3,}', str(job or '')))


def _same_job(a, b):
    if not a or not b:
        return False
    return _n(a) == _n(b) or bool(_nums(a) & _nums(b) and len(_nums(a)) == 1 and _nums(a) == _nums(b))


def entries(saved):
    """Seed entries plus the ones people verified in the app (theirs win on the same client/site/job/source)."""
    out = {}
    for e in SEED + list(saved or []):
        out[(e.get('client_key'), e.get('site_id') or e.get('site'), str(e.get('job')), e.get('list_id'), e.get('source'))] = e
    return list(out.values())


def candidates_for(job, registry, snapshot):
    """Every client list that carries this job label: verified registry entries and SharePoint job lists."""
    found = {}
    for e in registry:
        if e.get('job') and _same_job(job, e['job']):
            found[(e['client_key'], e.get('site_id') or e.get('site'), e.get('list_id'))] = dict(e, verified=bool(e.get('verified')), origin='registry')
    for c in snapshot or []:
        for j in c.get('jobs') or []:
            if _same_job(job, j.get('job')):
                key = (c['key'], j.get('site_id') or j.get('site'), j.get('list_id'))
                if not j.get('site_id'):
                    matches = [k for k, v in found.items() if v.get('client_key') == c['key'] and v.get('site') == j.get('site') and v.get('list_id') == j.get('list_id')]
                    if len(matches) == 1:
                        key = matches[0]
                if key in found:
                    found[key].setdefault('url', j.get('url'))
                    continue
                found[key] = dict(client_key=c['key'], client=c.get('client'), site=j.get('site'), job=j.get('job'),
                                  site_id=j.get('site_id'), list_id=j.get('list_id'), url=j.get('url'), verified=False, origin='sharepoint')
    return list(found.values())


def _company(site):
    return (site or '').partition('_')[0]


def _name_matches(hint, cand, snapshot):
    h = _n(hint)
    if len(h) < 2:
        return False
    names = {_n(cand.get('client')), _n(cand.get('client_key')), _n(cand.get('site')), _n(_company(cand.get('site'))), _n(cand.get('location'))}
    for c in snapshot or []:
        if c.get('key') == cand.get('client_key'):
            names |= {_n(c.get('client'))} | {_n(s.get('location')) for s in c.get('sites') or []}
    names.discard('')
    return any(n in h or (len(n) >= 4 and h in n) for n in names)


def filename_client(filename):
    """'SL Alabama - 2214 - 2026-11-06.xlsx' -> 'SL Alabama'. A hint only."""
    stem = re.sub(r'\.(xlsx|xlsm|csv)$', '', filename or '', flags=re.I)
    first = re.split(r'\s+[-–]\s+|_', stem)[0].strip()
    return None if not first or re.fullmatch(r'(ppg|report|daily)?\s*\d*', first, re.I) else first


def _pratt_source(result, filename):
    text = ' '.join([filename or ''] + [str(s.get('name') or '') for s in result.get('sheets') or []]
                    + [str(l.get('list') or '') for l in (result.get('dataset_selection') or {}).get('source_lists') or []]).lower()
    t = re.sub(r'[^a-z0-9]+', ' ', text)
    if 'multi pack' in t or 'multipack' in t:
        return 'DCA Multi-Pack Audit'
    if 'dca main' in t:
        return 'DCA Main Audit'
    if 'audits kn' in t or 'audit kn' in t:
        return 'Audits_KN'
    return None


def resolve(result, filename, job, day, registry, snapshot, manual=None):
    """-> dict(status, client_key, client, job, site, list_id, business_date, evidence[], candidates[])."""
    out = dict(business_date=day, evidence=[], candidates=[])
    if manual:
        return dict(out, status='manual', client_key=manual.get('client_key'), client=manual.get('client'), job=manual.get('job') or job,
                    site=manual.get('site'), site_id=manual.get('site_id'), list_id=manual.get('list_id'), source=manual.get('source'),
                    evidence=[f'Assigned by {manual.get("by") or "a reviewer"} on {str(manual.get("at") or "")[:10]}.'], assigned=manual)
    sites = sorted({l.get('site') for l in (result.get('dataset_selection') or {}).get('source_lists') or [] if l.get('site')})
    title_hint = (result.get('report_identity') or {}).get('client')
    file_hint = filename_client(filename)
    pratt = _pratt_source(result, filename)
    if pratt and not job:
        e = next(x for x in registry if x.get('source') == pratt)
        return dict(out, status='source', client_key=e['client_key'], client=e['client'], job=pratt, job_number=None, source=pratt,
                    evidence=[f'Pratt & Whitney source "{pratt}" (no verified job number, shown by source name).'])
    cands = candidates_for(job, registry, snapshot) if job else []
    out['candidates'] = [{k: c.get(k) for k in ('client_key', 'client', 'site', 'site_id', 'job', 'list_id', 'verified', 'separate', 'url')} for c in cands]
    if not job:
        return dict(out, status='unmatched', evidence=['No job number was found in the report.'])
    if sites:
        out['evidence'].append(f'Source record URLs point to SharePoint site {", ".join(sites)}.')
        by_site = [c for c in cands if c.get('site') in sites]
        if not by_site:
            return dict(out, status='unmatched', job=job,
                        evidence=out['evidence'] + [f'No client list for job {job} is known on {", ".join(sites)}.'])
        named = [h for h in (title_hint, file_hint) if h]
        if named and not any(_name_matches(h, c, snapshot) for h in named for c in by_site):
            others = [c for c in cands if any(_name_matches(h, c, snapshot) for h in named)]
            if others:
                return dict(out, status='conflict', job=job, evidence=out['evidence'] + [
                    f'The report names "{named[0]}" but its records come from {", ".join(sites)}.'])
        if len(by_site) == 1:
            c = by_site[0]
            return _pick(out, c, job, 'verified' if c.get('verified') else 'matched')
        # Several lists on the site carry the job label (for example an empty "#2" copy). The report's own source
        # record URLs name the exact list its rows came from, which settles it without asking a person.
        from urllib.parse import unquote
        named_lists = {str(l.get('list') or '').strip().lower() for l in (result.get('dataset_selection') or {}).get('source_lists') or [] if l.get('list')}
        exact = [c for c in by_site if c.get('url') and unquote(str(c['url']).rstrip('/').rsplit('/', 1)[-1]).strip().lower() in named_lists]
        if len(named_lists) == 1 and len(exact) == 1:
            out['evidence'].append(f'Its source record URLs point to the list "{sorted(named_lists)[0]}".')
            return _pick(out, exact[0], job, 'verified' if exact[0].get('verified') else 'matched')
        return dict(out, status='ambiguous', job=job, evidence=out['evidence'] + ['Several lists on that site carry this job label.'])
    pool = cands
    for label, hint in (('report title', title_hint), ('filename', file_hint)):
        if not hint:
            continue
        hits = [c for c in pool if _name_matches(hint, c, snapshot)]
        if hits:
            out['evidence'].append(f'The {label} names "{hint}".')
            pool = hits
            break
    keep = [c for c in pool if not c.get('separate')]
    pinned = [c for c in cands if c.get('verified') and not c.get('separate')]
    if not keep and pinned and out['evidence']:
        # Only a filename or title points at the separate list: that is a hint, not proof, so a person decides.
        return dict(out, status='ambiguous', job=job, evidence=out['evidence'] + [
            f'But job {job}\'s verified source list is on {pinned[0].get("site")}; the other list with this label never overrides it without proof '
            '(source record URLs in the workbook, or a person\'s choice).'])
    keep = keep or pool
    verified = [c for c in keep if c.get('verified')]
    if len(keep) == 1:
        c = keep[0]
        if not out['evidence']:
            out['evidence'].append(f'Job {job} is on one client list only ({c.get("site") or c.get("client")}).' if len(cands) == 1 else
                                   f'Job {job} is pinned in the verified registry to {c.get("site")}; the other list with this label is a separate client list.')
        return _pick(out, c, job, 'verified' if c.get('verified') else 'matched')
    if len(verified) == 1 and not out['evidence']:
        c = verified[0]
        out['evidence'].append(f'Job {job} is pinned in the verified registry to {c.get("site")}; other lists with the same label are unverified.')
        return _pick(out, c, job, 'verified')
    if not keep:
        return dict(out, status='unmatched', job=job, evidence=out['evidence'] + [f'Job {job} is on no known client list.'])
    return dict(out, status='ambiguous', job=job,
                evidence=out['evidence'] + [f'Job {job} is a list on {len(keep)} client sites: {", ".join(sorted(str(c.get("site") or c.get("client")) for c in keep))}.'])


def _pick(out, c, job, status):
    return dict(out, status=status, client_key=c['client_key'], client=c.get('client'), job=c.get('job') or job,
                site=c.get('site'), site_id=c.get('site_id'), list_id=c.get('list_id'))


def label(a):
    """Client → Job → Business Date, for display."""
    parts = [a.get('client') or 'Client not set', a.get('job') or 'Job not set', a.get('business_date') or 'Date not set']
    return ' → '.join(str(p) for p in parts)


# ---------------------------------------------------------------- manual assignment with audit trail
def assign(inbox, file_hash, choice, user, reason, current=None, remember=False):
    """Save a person's client/job choice for one file. Nothing about the report itself changes."""
    at = datetime.now(timezone.utc).isoformat()
    record = {k: choice.get(k) for k in ('client_key', 'client', 'job', 'site', 'site_id', 'list_id', 'source')}
    if not record.get('client_key') or not (record.get('job') or record.get('source')):
        raise ValueError('Choose a client and a job.')
    record.update(by=(user or {}).get('email') or 'reviewer', at=at, reason=(reason or '').strip()[:300] or None)
    before = inbox.setting(f'assignment|{file_hash}', None)
    inbox.set_setting(f'assignment|{file_hash}', record)
    log = list(inbox.setting(LOG_KEY, []) or [])
    log.append(dict(file_hash=file_hash, at=at, by=record['by'], reason=record['reason'], to=record,
                    previous=before or ({k: (current or {}).get(k) for k in ('status', 'client_key', 'job', 'site')} if current else None)))
    inbox.set_setting(LOG_KEY, log[-LOG_LIMIT:])
    if remember and record.get('site') and record.get('job'):
        saved = list(inbox.setting(REGISTRY_KEY, []) or [])
        saved.append(dict({k: record[k] for k in ('client_key', 'client', 'job', 'site', 'site_id', 'list_id')}, verified=True,
                          note=f'Verified by {record["by"]} on {at[:10]}.' + (f' {record["reason"]}' if record['reason'] else '')))
        inbox.set_setting(REGISTRY_KEY, saved)
    return record


def history(inbox, file_hash):
    return [e for e in inbox.setting(LOG_KEY, []) or [] if e.get('file_hash') == file_hash]

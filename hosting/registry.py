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
    # Active job lists, 2026-11-20. Each list ID was read from the SharePoint site itself on that date (list title and ID
    # matched) at the operations owner's direction.
    dict(client_key='SLAlabama', client='SL Alabama', site='SLAlabama_AlexanderCity', location='Alexander City', job='2169',
         list_id='38718598-a97e-4388-bcc5-709ede868b21', verified=True, note='Data #2169-SLALAL-SLAL.'),
    # The second list labelled 2169. It is not empty, but the rows seen in it carry no authoritative work date, so it is
    # kept apart: never verified, never chosen by itself, and not a source for production reports.
    dict(client_key='SLAlabama', client='SL Alabama', site='SLAlabama_AlexanderCity', location='Alexander City', job='2169',
         list_id='b85d1fdb-d7b3-4f5c-8223-8d8f737e06d5', verified=False, separate=True, production=False,
         note='Data #2169-SLALAL-SLAL #2: secondary list; rows lack an authoritative work date.'),
    dict(client_key='SLAlabama', client='SL Alabama', site='SLAlabama_AlexanderCity', location='Alexander City', job='2200',
         list_id='e402d426-4595-47bc-a305-a0704df18258', verified=True, note='Data #2200-SLALAL-SLALANNX.'),
    dict(client_key='SLAlabama', client='SL Alabama', site='SLAlabama_AlexanderCity', location='Alexander City', job='2214',
         list_id='d1a655b8-dc19-4d0f-bad2-35ed9999ccc2', verified=True, note='Data #2214-SLAL-SLAL.'),
    dict(client_key='Hyundai', client='Hyundai', site='Hyundai_Montgomery', location='Montgomery', job='2236',
         list_id='9876a097-558d-4407-9b90-216d46a81217', verified=True, note='Data #2236-SLAL-HMMA.'),
    dict(client_key='Hyundai', client='Hyundai', site='Hyundai_Montgomery', location='Montgomery', job='2250',
         list_id='4a6b1fb9-ede1-4d5c-80dd-9ad1d889df91', verified=True, note='Data #2250-LSAMX-HMMA.'),
    dict(client_key='Lear', client='Lear', site='Lear_T1', location='Tuscaloosa', job='2206',
         list_id='4cd2ca3c-c0c6-4092-8b8d-8b52ada1fb3d', verified=True, note='Data #2206-BROQU-LEART1.'),
    dict(client_key='BriggsStratton', client='Briggs Stratton', site='BriggsStratton_Auburn', location='Auburn', job='2228',
         list_id='fe964017-6594-4f24-8e8a-deb82119418c', verified=True, note='Data #2228-TENFM-BSAL.'),
    # Pratt & Whitney sources have no verified numeric job: each source is shown under its own name, never combined.
    dict(client_key='PrattWhitney', client='Pratt & Whitney', source='Audits_KN', job=None, verified=True, note='No verified job number.'),
    dict(client_key='PrattWhitney', client='Pratt & Whitney', source='DCA Main Audit', job=None, verified=True, note='No verified job number.'),
    dict(client_key='PrattWhitney', client='Pratt & Whitney', source='DCA Multi-Pack Audit', job=None, verified=True, note='No verified job number.'),
]
def not_for_production():
    """List IDs pinned as not a source for production reports. A saved entry cannot lift this."""
    return {e['list_id'] for e in SEED if e.get('production') is False and e.get('list_id')}


RESOLVED = ('manual', 'verified', 'matched', 'source')
BUILT_FROM_PREFIX = 'built_from|'
NEEDS_PERSON = ('ambiguous', 'conflict', 'unmatched')


def _n(text):
    return re.sub(r'[^a-z0-9]+', '', str(text or '').lower())


def _nums(job):
    return set(re.findall(r'\d{3,}', str(job or '')))


def _same_job(a, b):
    if not a or not b:
        return False
    return _n(a) == _n(b) or bool(_nums(a) & _nums(b) and len(_nums(a)) == 1 and _nums(a) == _nums(b))


def _job_key(job):
    """What makes two entries the same job: its number when it has exactly one, else its whole normalised label."""
    nums = _nums(job)
    return ('n', next(iter(nums))) if len(nums) == 1 else ('t', _n(job))


def entries(saved):
    """Seed entries plus the ones people verified in the app (theirs win on the same client/site/job/source).

    One list for one job is one entry. An entry that names its site only by name is resolved to the entry for the same
    client, site name, list and JOB that carries the site ID, but only when exactly one site ID is on record for it. Then
    the entry with the site ID is kept and a pin's verification carries over to it. Two different site IDs are a
    disagreement: nothing is resolved and every entry stays as it is. An entry for another job never replaces, hides or
    inherits from this one, even on the same list."""
    out = {}
    for e in SEED + list(saved or []):
        out[(e.get('client_key'), e.get('site_id') or e.get('site'), str(e.get('job')), e.get('list_id'), e.get('source'))] = e
    def group(e):
        return (e.get('client_key'), e.get('site'), e.get('list_id'), _job_key(e.get('job')))
    ids, named = {}, {}
    for e in out.values():
        if e.get('list_id') and e.get('site') and e.get('job'):
            (ids.setdefault(group(e), set()).add(e['site_id']) if e.get('site_id') else named.setdefault(group(e), []).append(e))
    result = []
    for e in out.values():
        g = group(e) if e.get('list_id') and e.get('site') and e.get('job') else None
        if g and not e.get('site_id') and len(ids.get(g, ())) == 1:
            continue                                   # resolved to the one entry that carries the site ID (below)
        if g and e.get('site_id') and len(ids[g]) == 1 and any(n.get('verified') for n in named.get(g, ())) and not e.get('verified'):
            e = dict(e, verified=True)
        result.append(e)
    return result


def candidates_for(job, registry, snapshot):
    """Every client list that carries this job label: verified registry entries and SharePoint job lists.

    A list is identified by its exact site ID and list ID. Everything that carries a site ID is collected first; only
    then is an entry known by site name alone matched against them, so the answer does not depend on the order in
    which lists were discovered. One exact match: they are the same list, and a pin's verification applies to it. Two
    exact matches with different site IDs: the name does not say which is meant, so the pin verifies neither and both
    stay as separate, unverified candidates for a person to choose between."""
    exact, loose = {}, []
    def take(c):
        if c.get('site_id'):
            key = (c['client_key'], c['site_id'], c.get('list_id'))
            if key in exact:
                was = exact[key]
                merged = dict(c, **{k: v for k, v in was.items() if v is not None}) if was.get('origin') == 'registry' else dict(was, **{k: v for k, v in c.items() if v is not None})
                merged['verified'] = bool(was.get('verified') or c.get('verified'))
                merged['origin'] = 'registry' if 'registry' in (was.get('origin'), c.get('origin')) else 'sharepoint'
                merged['url'] = was.get('url') or c.get('url')
                exact[key] = merged
            else:
                exact[key] = c
        else:
            loose.append(c)
    for e in registry:
        if e.get('job') and _same_job(job, e['job']):
            take(dict(e, verified=bool(e.get('verified')), origin='registry'))
    for c in snapshot or []:
        for j in c.get('jobs') or []:
            if _same_job(job, j.get('job')):
                take(dict(client_key=c['key'], client=c.get('client'), site=j.get('site'), job=j.get('job'),
                          site_id=j.get('site_id'), list_id=j.get('list_id'), url=j.get('url'), verified=False, origin='sharepoint'))
    found = dict(exact)
    for c in sorted(loose, key=lambda c: c.get('origin') != 'registry'):       # registry entries first, whatever order they came in
        same = [k for k, v in exact.items() if v.get('client_key') == c['client_key'] and v.get('site') == c.get('site') and v.get('list_id') == c.get('list_id')]
        if len(same) == 1:
            was = found[same[0]]
            merged = dict(was, **{k: v for k, v in c.items() if v is not None and k not in ('origin', 'verified', 'url', 'site_id')}) if c.get('origin') == 'registry' else dict(was)
            merged.update(verified=bool(was.get('verified') or c.get('verified')), url=was.get('url') or c.get('url'),
                          origin='registry' if 'registry' in (was.get('origin'), c.get('origin')) else 'sharepoint')
            found[same[0]] = merged
        elif len(same) > 1:
            for k in same:                                # two site IDs disagree: the name-only entry settles nothing
                found[k] = dict(found[k], name_only_entry_unresolved=True)
        else:
            key = (c['client_key'], c.get('site'), c.get('list_id'))
            if key in found:
                was = found[key]
                found[key] = dict(was, verified=bool(was.get('verified') or c.get('verified')), url=was.get('url') or c.get('url'))
            else:
                found[key] = c
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


def built_from(entry, binding, job):
    """What a build records about its source: the client and the exact site and list the rows were read from."""
    return dict(client_key=entry.get('client_key'), client=entry.get('client'), job=str(job or entry.get('job') or ''),
                job_number=entry.get('job'), site=entry.get('site'), site_id=binding['site_id'], list_id=binding['list_id'])


def resolve(result, filename, job, day, registry, snapshot, manual=None, built=None):
    """-> dict(status, client_key, client, job, site, list_id, business_date, evidence[], candidates[])."""
    out = dict(business_date=day, evidence=[], candidates=[])
    # A report the app built itself came from one verified client site and list, recorded at build time. A person's own
    # choice still comes first; nothing is inferred when the record is incomplete.
    if not manual and built and built.get('client_key') and built.get('site_id') and built.get('list_id'):
        return dict(out, status='verified', client_key=built['client_key'], client=built.get('client'), job=job or built.get('job') or built.get('job_number'),
                    site=built.get('site'), site_id=built['site_id'], list_id=built['list_id'],
                    evidence=[f'Built from the {built.get("client") or built["client_key"]} SharePoint list on site {built.get("site") or built["site_id"]}.'])
    if manual:
        return dict(out, status='manual', client_key=manual.get('client_key'), client=manual.get('client'), job=manual.get('job') or job,
                    site=manual.get('site'), site_id=manual.get('site_id'), list_id=manual.get('list_id'), source=manual.get('source'),
                    evidence=[f'Assigned by {manual.get("by") or "a reviewer"} on {str(manual.get("at") or "")[:10]}.'], assigned=manual)
    # A file named "PRACTICE ..." is made-up test data: it belongs to no client and no SharePoint list, and says so
    # instead of waiting for a person to choose a client.
    if re.match(r'PRACTICE\b', filename or '') and not job and not (result.get('dataset_selection') or {}).get('source_lists'):
        return dict(out, status='source', client_key='practice', client='Practice (not a client)', job='Practice', job_number=None, source='practice',
                    evidence=['The file is named as a practice report: made-up data, not a client report. It is not compared with SharePoint.'])
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
    # Every record row carries the id of the SharePoint list it came from (its stable source key). One list id that
    # is one of this job's lists settles which list it is, also when a "#2" copy carries the same job label.
    key_lists = (result.get('dataset_selection') or {}).get('source_key_lists') or []
    keyed = [c for c in cands if len(key_lists) == 1 and str(c.get('list_id') or '').lower() == key_lists[0]]
    named = [h for h in (title_hint, file_hint) if h]
    if len(keyed) == 1 and (not named or any(_name_matches(h, keyed[0], snapshot) for h in named)):
        out['evidence'].append(f'Every record row names the SharePoint list it came from: {keyed[0].get("job") or job} on {keyed[0].get("site")}.')
        return _pick(out, keyed[0], job, 'verified' if keyed[0].get('verified') else 'matched')
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

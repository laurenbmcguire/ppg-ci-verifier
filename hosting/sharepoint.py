"""Read-only lookup of a job's Power Automate daily export on SharePoint, via Microsoft Graph.

Configuration (Vercel environment variables; the secret is stored as Sensitive and never logged):
  MS_TENANT_ID, MS_CLIENT_ID, MS_CLIENT_SECRET   app registration with read-only Sites access
  SHAREPOINT_HOST      default ppgquality.sharepoint.com
  SHAREPOINT_SITES     comma-separated client site names (e.g. BriggsStratton_Auburn,Lear_T1). Optional when
                       the app has Sites.Read.All: sites are then discovered by search.
  SHAREPOINT_EXPORT_FOLDER  default "Daily Data Exports"
Path probed in each site's default document library:
  <folder>/<job>/<YYYY-MM-DD>/<job> <YYYY-MM-DD>.csv   (then .xlsx)
"""
import re
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

GRAPH = 'https://graph.microsoft.com/v1.0'
TIMEOUT = 15
_token = {'value': None, 'expires': 0}
_site_ids = {}


class SharePointNotConfigured(RuntimeError):
    pass


class SharePointNotFound(LookupError):
    pass


class SharePointError(RuntimeError):
    pass


def configured():
    return all(os.environ.get(k) for k in ('MS_TENANT_ID', 'MS_CLIENT_ID', 'MS_CLIENT_SECRET'))


def _open(request, opener=None):
    return (opener or urllib.request.urlopen)(request, timeout=TIMEOUT)


def _token_value(opener=None):
    if _token['value'] and _token['expires'] > time.time() + 60:
        return _token['value']
    if not configured():
        raise SharePointNotConfigured('SharePoint lookup is not set up yet (Microsoft app registration missing).')
    body = urllib.parse.urlencode({'client_id': os.environ['MS_CLIENT_ID'], 'client_secret': os.environ['MS_CLIENT_SECRET'],
                                   'grant_type': 'client_credentials', 'scope': 'https://graph.microsoft.com/.default'}).encode()
    url = f'https://login.microsoftonline.com/{urllib.parse.quote(os.environ["MS_TENANT_ID"])}/oauth2/v2.0/token'
    try:
        with _open(urllib.request.Request(url, data=body, method='POST'), opener) as response:
            data = json.loads(response.read())
    except urllib.error.HTTPError as exc:
        raise SharePointError(f'Microsoft sign-in for the SharePoint lookup failed (HTTP {exc.code}). Check the app registration ID, tenant and secret.') from None
    except (urllib.error.URLError, TimeoutError, ValueError):
        raise SharePointError('Could not reach Microsoft sign-in for the SharePoint lookup.') from None
    _token.update(value=data['access_token'], expires=time.time() + int(data.get('expires_in', 3000)))
    return _token['value']


def _get(path_or_url, opener=None, raw=False):
    url = path_or_url if path_or_url.startswith('https://') else GRAPH + path_or_url
    request = urllib.request.Request(url, headers={'Authorization': 'Bearer ' + _token_value(opener)})
    try:
        with _open(request, opener) as response:
            data = response.read()
            return data if raw else json.loads(data)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        if exc.code in (401, 403):
            raise SharePointError(f'The SharePoint app registration is not allowed to read this site (HTTP {exc.code}). Grant it read access to the client sites.') from None
        raise SharePointError(f'SharePoint returned HTTP {exc.code}.') from None
    except (urllib.error.URLError, TimeoutError):
        raise SharePointError('Could not reach SharePoint.') from None


SKIP_WORDS = ('test', 'demo', 'sandbox', 'template', 'training', 'sample')
SITES_TTL = 1800
_site_cache = {'until': 0, 'value': None}


def _is_test(*names):
    """Test, demo and sandbox sites are never treated as clients."""
    words = re.split(r'[^a-z0-9]+', ' '.join(_words(n or '') for n in names).lower())
    return any(w.startswith(SKIP_WORDS) for w in words if w)


def _configured_sites(opener=None):
    names = [s.strip() for s in os.environ.get('SHAREPOINT_SITES', '').split(',') if s.strip()]
    host = os.environ.get('SHAREPOINT_HOST', 'ppgquality.sharepoint.com')
    for name in names:
        if name not in _site_ids:
            site = _get(f'/sites/{host}:/sites/{urllib.parse.quote(name)}', opener)
            _site_ids[name] = site['id'] if site else None
        if _site_ids[name]:
            yield name, _site_ids[name]


def _site_lists(site_id, opener=None):
    return (_get(f'/sites/{site_id}/lists?$select=id,displayName,webUrl,lastModifiedDateTime&$top=500', opener) or {}).get('value', [])


def client_sites(opener=None, refresh=False):
    """Every client site: the configured ones plus any site on PPG's SharePoint with a "Data #" job list,
    leaving out tests and demos. [(name, site_id, lists)], cached for 30 minutes; lists are fetched in parallel."""
    from concurrent.futures import ThreadPoolExecutor
    key = (opener is None, os.environ.get('SHAREPOINT_SITES', ''), os.environ.get('SHAREPOINT_HOST', ''))
    if opener is None and not refresh and _site_cache.get('key') == key and _site_cache['value'] is not None and _site_cache['until'] > time.time():
        return _site_cache['value']
    sites = {sid: name for name, sid in _configured_sites(opener)}
    try:   # needs the app's Sites.Read.All; without it only the configured sites are used
        url = '/sites?search=*&$select=id,name,displayName,webUrl&$top=200'
        while url:
            page = _get(url, opener) or {}
            for site in page.get('value', []):
                name = site.get('name') or (site.get('webUrl') or '').rstrip('/').rsplit('/', 1)[-1]
                if site.get('id') and name and not _is_test(name, site.get('displayName')):
                    sites.setdefault(site['id'], name)
            url = page.get('@odata.nextLink')
    except SharePointError:
        pass
    configured = {sid for _, sid in _configured_sites(opener)}
    with ThreadPoolExecutor(8) as pool:
        lists = dict(zip(sites, pool.map(lambda sid: _safe_lists(sid, opener), sites)))
    value = [(name, sid, lists[sid]) for sid, name in sites.items()
             if not _is_test(name) and (sid in configured or any((l.get('displayName') or '').lower().startswith('data #') for l in lists[sid]))]
    _site_cache.update(key=key, until=time.time() + SITES_TTL, value=value)
    return value


def _safe_lists(site_id, opener=None):
    try:
        return _site_lists(site_id, opener)
    except SharePointError:
        return []


def _sites(opener=None):
    for name, sid, _ in client_sites(opener):
        yield name, sid


def fetch_export(job, day, opener=None, schedule=None):
    """Return (filename, bytes, meta) for the job's daily export, or raise SharePointNotFound."""
    if not configured():
        raise SharePointNotConfigured('SharePoint lookup is not set up yet (Microsoft app registration missing).')
    if not job or not day:
        raise SharePointNotFound('The report\'s job number or date could not be determined, so SharePoint could not be searched.')
    folder = os.environ.get('SHAREPOINT_EXPORT_FOLDER', 'Daily Data Exports').strip('/')
    tried = []
    for name, site_id in _sites(opener):
        for ext in ('csv', 'xlsx'):
            filename = f'{job} {day}.{ext}'
            path = f'{folder}/{job}/{day}/{filename}'
            tried.append(f'{name}/{path}')
            item = _get(f'/sites/{site_id}/drive/root:/{urllib.parse.quote(path)}', opener)
            if not item:
                continue
            content = _get(f'/sites/{site_id}/drive/items/{item["id"]}/content', opener, raw=True)
            if content is None:
                continue
            return filename, content, dict(site=name, path=path, web_url=item.get('webUrl'),
                                           modified=item.get('lastModifiedDateTime'), job=job, day=day)
    found = list_day(job, day, opener, schedule)
    if found:
        return found
    data = _list_items(job, opener)
    if data:
        days = sorted({d for d in (_item_day(it) for it in data[3]) if d}, reverse=True)
        raise SharePointNotFound(f'Found the SharePoint list "{data[1].get("displayName")}" ({len(data[3])} rows), but none are dated {day}.'
                                 + (f' Most recent days in the list: {", ".join(days[:5])}.' if days else ' Its rows have no dates.'))
    raise SharePointNotFound(f'No SharePoint data found for {job} on {day}: no daily export file, and no "Data #{job}" list with rows for that day.'
                             + (f' Looked in {len(tried) // 2} site(s).' if tried else ' No client sites are configured.')
                             + (f' Data lists seen: {", ".join(sorted(set(_seen_lists))[:12])}.' if _seen_lists else ''))


def recent_exports(job, before_day, limit=10, opener=None):
    """Earlier daily exports for a job: [(day, filename, bytes)], newest first, up to `limit`. Read-only."""
    import re as _re
    if not configured() or not job or not before_day:
        return []
    folder = os.environ.get('SHAREPOINT_EXPORT_FOLDER', 'Daily Data Exports').strip('/')
    for name, site_id in _sites(opener):
        listing = _get(f'/sites/{site_id}/drive/root:/{urllib.parse.quote(f"{folder}/{job}")}:/children?$select=name,folder&$top=500', opener)
        if not listing:
            continue
        days = sorted((c['name'] for c in listing.get('value', []) if c.get('folder') is not None
                       and _re.fullmatch(r'\d{4}-\d{2}-\d{2}', c.get('name', '')) and c['name'] < before_day), reverse=True)
        out = []
        for day in days:
            if len(out) >= limit:
                break
            for ext in ('csv', 'xlsx'):
                filename = f'{job} {day}.{ext}'
                item = _get(f'/sites/{site_id}/drive/root:/{urllib.parse.quote(f"{folder}/{job}/{day}/{filename}")}', opener)
                if not item:
                    continue
                content = _get(f'/sites/{site_id}/drive/items/{item["id"]}/content', opener, raw=True)
                if content:
                    out.append((day, filename, content))
                    break
        if out:
            return out
    return list_history(job, before_day, limit, opener)


# ---------------------------------------------------------------- the job's own SharePoint data list
# Field crews' scans live in a list named "Data #<job>" on the client site (the daily exports are built from it).

_SKIP_FIELDS = {'id', 'ContentType', 'Modified', 'Created', 'AuthorLookupId', 'EditorLookupId', '_UIVersionString', 'Attachments',
                'Edit', 'LinkTitleNoMenu', 'LinkTitle', 'ItemChildCount', 'FolderChildCount', '_ComplianceFlags', '_ComplianceTag',
                '_ComplianceTagWrittenTime', '_ComplianceTagUserId', 'AppAuthorLookupId', 'AppEditorLookupId', '@odata.etag', 'DocIcon'}
_list_cache = {}
_seen_lists = []
LIST_TTL = 120


def _tokens(text):
    return frozenset(t for t in re.split(r'[^a-z0-9]+', (text or '').lower()) if t and t != 'data')


def _list_match(job, display, web_url=''):
    """2 = same job written in any order or run together ("Data #2219-HANDS-HMMA", "Data 2219HANDSHMMA" for
    HANDS-HMMA-2219), 1 = same job number, 0 = no."""
    best = 0
    tail = urllib.parse.unquote((web_url or '').rstrip('/').split('/Lists/')[-1].split('/')[0]) if '/Lists/' in (web_url or '') else ''
    for text in (display, tail):
        name = (text or '').strip().lower()
        if not name or (not name.startswith('data') and name != (job or '').lower()):
            continue
        want, have = _tokens(job), _tokens(text)
        compact = re.sub(r'[^a-z0-9]', '', name)
        compact = compact[4:] if compact.startswith('data') else compact
        if want and (want == have or (all(t in compact for t in want) and len(compact) == sum(len(t) for t in want))):
            return 2
        numbers = {t for t in want if t.isdigit() and len(t) >= 3}
        if numbers and any(n in compact for n in numbers):
            best = 1
    return best


def _find_list(job, opener=None):
    """The job's "Data #<job>" list. Field lists put the job number first (Data #2219-HANDS-HMMA) while reports
    often put it last (HANDS-HMMA-2219), so match on the job's parts in any order, then on the job number alone."""
    exact, near = [], []
    _seen_lists.clear()
    for name, site_id, site_lists in client_sites(opener):
        for lst in site_lists:
            if (lst.get('displayName') or '').lower().startswith('data #'):
                _seen_lists.append(lst['displayName'])
            score = _list_match(job, lst.get('displayName'), lst.get('webUrl'))
            if score == 2:
                exact.append((name, site_id, lst))
            elif score == 1:
                near.append((name, site_id, lst))
    # The same job list can exist on two sites (e.g. SL Alabama's Alexander City site and the Hyundai Montgomery
    # site). The one the tablets are writing to is the one changed most recently.
    from hosting import registry
    pinned = {e['list_id'] for e in registry.SEED if e.get('list_id') and not e.get('separate') and e.get('job') and registry._same_job(job, e['job'])}
    for found in (exact, near):
        hit = [f for f in found if f[2].get('id') in pinned]
        if hit:   # the verified registry decides between two sites with the same job label
            return hit[0]
        if found:
            return max(found, key=lambda f: f[2].get('lastModifiedDateTime') or '')
    return None


def _list_items(job, opener=None):
    """(site name, list info, column display names, items) for the job's data list, cached briefly."""
    key = job.upper()
    hit = _list_cache.get(key)
    if hit and hit[0] > time.time():
        return hit[1]
    found = _find_list(job, opener)
    if not found:
        _list_cache[key] = (time.time() + LIST_TTL, None)
        return None
    name, site_id, lst = found
    cols = (_get(f'/sites/{site_id}/lists/{lst["id"]}/columns?$select=name,displayName,hidden,readOnly&$top=500', opener) or {}).get('value', [])
    names = {c['name']: c.get('displayName') or c['name'] for c in cols if not c.get('hidden') and not c.get('readOnly')}
    items, url = [], f'/sites/{site_id}/lists/{lst["id"]}/items?$expand=fields&$top=999'
    while url and len(items) < 50000:
        page = _get(url, opener) or {}
        items.extend(page.get('value', []))
        url = page.get('@odata.nextLink')
    value = (name, lst, names, items)
    _list_cache[key] = (time.time() + LIST_TTL, value)
    return value


def _stamp(item):
    fields = item.get('fields') or {}
    return fields.get('Date0') or fields.get('Date') or item.get('createdDateTime')


def _item_day(item, schedule=None):
    """The report day a scan belongs to. With shift times, a scan after midnight in a shift that started the
    evening before belongs to that earlier day."""
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from hosting.reconcile import is_local_midnight, business_day
    business = (item.get('fields') or {}).get('Date')
    if business and is_local_midnight(business):   # the date-only business Date wins over scan / submission times
        return business_day(business, True)
    if schedule:
        from hosting.shifts import local_time, assign
        local = local_time(_stamp(item))
        if local is not None:
            return assign(local, schedule)[1]
    fields = item.get('fields') or {}
    stamp = fields.get('Date0') or fields.get('Date') or item.get('createdDateTime')
    if not stamp:
        return None
    try:
        dt = datetime.fromisoformat(str(stamp).replace('Z', '+00:00'))
    except ValueError:
        return str(stamp)[:10]
    if dt.tzinfo is None:
        return dt.date().isoformat()
    return dt.astimezone(ZoneInfo('America/Chicago')).date().isoformat()


def _items_csv(names, items):
    import csv, io
    order = [n for n in names if n not in _SKIP_FIELDS]
    extra = sorted({k for it in items for k in (it.get('fields') or {}) if k not in names and k not in _SKIP_FIELDS
                    and not k.startswith('_') and not k.endswith('LookupId') and not k.startswith('@')})
    keys = order + extra
    out = io.StringIO(newline='')
    w = csv.writer(out)
    w.writerow([names.get(k, k) for k in keys])
    for it in items:
        f = it.get('fields') or {}
        w.writerow(['' if f.get(k) is None else (f[k] if not isinstance(f[k], (dict, list)) else json.dumps(f[k])) for k in keys])
    return out.getvalue().encode('utf-8')


def _by_time(names, rows, schedule):
    """Rows with Shift set from the scan time (the inspector's pick kept as "Picked shift") and a Scan time column."""
    from hosting.shifts import local_time, assign, number, label
    shift_key = next((k for k, v in names.items() if re.sub(r'[^a-z]', '', str(v).lower()) == 'shift'), None) or 'Shift'
    names = dict(names)
    names[shift_key] = names.get(shift_key, 'Shift')
    names['ShiftPicked'], names['ScanTime'] = 'Picked shift', 'Scan time'
    out, scans = [], {}
    for it in rows:
        f = dict(it.get('fields') or {})
        local = local_time(_stamp(it))
        shift, report_day = assign(local, schedule) if local else (None, None)
        f['ShiftPicked'] = f.get(shift_key)
        f['ScanTime'] = f'{local.month}/{local.day} at {(local.hour % 12) or 12}:{local.minute:02d} {"AM" if local.hour < 12 else "PM"}' if local else None
        if shift:
            f[shift_key] = number(shift['name'])
        ident = f.get('Identifier') or f.get('VIN') or f.get('Title')
        if ident:
            scans[re.sub(r'[^A-Z0-9]', '', str(ident).upper())] = dict(time=f['ScanTime'], shift=number(shift['name']) if shift else None,
                                                                        label=label(shift) if shift else 'outside every shift',
                                                                        picked=None if f['ShiftPicked'] is None else str(f['ShiftPicked']), day=report_day)
        out.append(dict(it, fields=f))
    return names, out, scans


def timeline(rows, schedule):
    """Internal scan timeline for the review screen (never sent to clients): scans per half hour in plant time,
    first and last scan per shift, long gaps, scans outside every shift, per-person activity, and shift picks
    that flip back and forth, which the end-of-shift routine lists as red flags."""
    from hosting.shifts import local_time, assign, number, clock, _minutes
    pts = []
    for it in rows:
        local = local_time(_stamp(it))
        if not local:
            continue
        f = it.get('fields') or {}
        who = (((it.get('createdBy') or {}).get('user') or {}).get('displayName')) or f.get('Author') or None
        shift = assign(local, schedule)[0]
        picked = f.get('ShiftPicked', f.get('Shift'))
        pts.append((local, shift, who, None if picked in (None, '') else str(picked)))
    if not pts:
        return None
    pts.sort(key=lambda p: p[0])
    buckets = {}
    for local, *_ in pts:
        key = local.replace(minute=0 if local.minute < 30 else 30, second=0, microsecond=0)
        buckets[key] = buckets.get(key, 0) + 1
    flags = []
    gaps = []
    for (a, sa, *_), (b, sb, *_) in zip(pts, pts[1:]):
        mins = (b - a).total_seconds() / 60
        if mins >= 45 and sa and sb and sa['name'] == sb['name']:
            gaps.append(dict(start=a.isoformat(), end=b.isoformat(), minutes=round(mins), shift=sa['name']))
    for g in gaps[:3]:
        flags.append(f'No scans for {g["minutes"]} minutes on {g["shift"]} shift ({_hm(g["start"])} to {_hm(g["end"])}). A break, or a problem on the line?')
    if len(gaps) > 3:
        flags.append(f'{len(gaps) - 3} more gap(s) of 45 minutes or longer; see the timeline.')
    outside = [p for p in pts if not p[1]]
    if outside:
        flags.append(f'{len(outside)} scan(s) fall outside every shift (first at {outside[0][0].strftime("%I:%M %p").lstrip("0")}). Check the shift times or the scans.')
    per_shift = {}
    for local, shift, who, picked in pts:
        if shift:
            per_shift.setdefault(shift['name'], []).append((local, who))
    shifts_out = []
    for sh in schedule:
        got = per_shift.get(sh['name'])
        if not got:
            continue
        first, last = got[0][0], got[-1][0]
        start_m = _minutes(sh['start']); first_m = first.hour * 60 + first.minute
        late = (first_m - start_m) % 1440
        if 60 <= late < 600 and len(got) >= 5:
            flags.append(f'First {sh["name"]}-shift scan was at {first.strftime("%I:%M %p").lstrip("0")}, {late // 60} h {late % 60} min after the shift started.')
        shifts_out.append(dict(name=sh['name'], start=sh['start'], end=sh['end'], scans=len(got), first=first.isoformat(), last=last.isoformat()))
    people = {}
    for local, shift, who, picked in pts:
        if who:
            p = people.setdefault(who, dict(name=who, scans=0, first=local, last=local, shifts=set()))
            p['scans'] += 1; p['last'] = local
            if shift:
                p['shifts'].add(shift['name'])
    for p in people.values():
        if len(p['shifts']) > 1:
            flags.append(f'{p["name"]} scanned on more than one shift ({", ".join(sorted(p["shifts"]))}), {p["first"].strftime("%I:%M %p").lstrip("0")} to {p["last"].strftime("%I:%M %p").lstrip("0")}. A phone handed over without logging out?')
    picks = [p[3] for p in pts if p[3]]
    flips = sum(1 for a, b, c in zip(picks, picks[1:], picks[2:]) if a == c != b)
    if flips:
        flags.append(f'The picked shift flips back and forth {flips} time(s) in a row of scans (like 1, 2, 1). A wrong pick or a swapped phone?')
    return dict(zone='Central', first=pts[0][0].isoformat(), last=pts[-1][0].isoformat(), scans=len(pts),
                buckets=[dict(at=k.isoformat(), scans=v) for k, v in sorted(buckets.items())],
                shifts=shifts_out, gaps=gaps[:20], outside=len(outside), flags=flags,
                people=[dict(name=p['name'], scans=p['scans'], first=p['first'].isoformat(), last=p['last'].isoformat(), shifts=sorted(p['shifts']))
                        for p in sorted(people.values(), key=lambda p: p['first'])][:30])


def _hm(iso):
    from datetime import datetime
    return datetime.fromisoformat(iso).strftime('%I:%M %p').lstrip('0')


def list_day(job, day, opener=None, schedule=None):
    """(filename, csv bytes, meta) built from the job's data list for one day, or None.
    With shift times, the day runs from the first shift's start to the last shift's end, and each scan's
    shift comes from its time."""
    if not configured() or not job or not day:
        return None
    data = _list_items(job, opener)
    if not data:
        return None
    site, lst, names, items = data
    rows = [it for it in items if _item_day(it, schedule) == day]
    if not rows:
        return None
    modified = max((it.get('lastModifiedDateTime') or '') for it in rows) or None
    meta = dict(site=site, path=f'list: {lst.get("displayName")}', web_url=lst.get('webUrl'), modified=modified,
                job=job, day=day, source_kind='list', rows=len(rows))
    if schedule:
        names, rows, scans = _by_time(names, rows, schedule)
        meta.update(scan_shifts=scans, shift_times=schedule, timeline=timeline(rows, schedule))
    return (f'{job} {day} (SharePoint list).csv', _items_csv(names, rows), meta)


def list_history(job, before_day, limit=10, opener=None, schedule=None):
    """[(day, filename, csv bytes)] from the job's data list for earlier days, newest first."""
    data = _list_items(job, opener) if configured() and job and before_day else None
    if not data:
        return []
    _, _, names, items = data
    by_day = {}
    for it in items:
        d = _item_day(it, schedule)
        if d and d < before_day:
            by_day.setdefault(d, []).append(it)
    return [(d, f'{job} {d} (SharePoint list).csv', _items_csv(names, by_day[d])) for d in sorted(by_day, reverse=True)[:limit]]


# ---------------------------------------------------------------- clients: one per company, from the client sites
_clients_cache = {'until': 0, 'value': None}
CLIENTS_TTL = 1800


NICE_NAMES = {'BriggsStratton': 'Briggs & Stratton', 'PrattWhitney': 'Pratt & Whitney', 'Hyundai': 'Hyundai (HMMA)'}


def _words(name):
    """'BriggsStratton' -> 'Briggs Stratton', 'HyundaiMobisAlabama' -> 'Hyundai Mobis Alabama', 'Lear' -> 'Lear'."""
    return re.sub(r'(?<=[a-z])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])', ' ', name.replace('-', ' ')).strip()


def clients(opener=None, refresh=False):
    """[{client, sites: [{site, location, url}], jobs: [{job, number, site}]}], one per company, from each client
    site's "Data #<job>" lists. Cached for 30 minutes."""
    if not configured():
        raise SharePointNotConfigured('SharePoint is not connected.')
    if not refresh and _clients_cache['value'] is not None and _clients_cache['until'] > time.time():
        return _clients_cache['value']
    out = {}
    for name, site_id, site_lists in client_sites(opener, refresh=refresh):
        company, _, location = name.partition('_')
        c = out.setdefault(company, dict(client=NICE_NAMES.get(company) or _words(company), key=company, sites=[], jobs=[]))
        c['sites'].append(dict(site=name, location=_words(location) if location else ''))
        for lst in site_lists:
            title = (lst.get('displayName') or '').strip()
            if not title.lower().startswith('data #'):
                continue
            job = title[6:].strip()
            numbers = sorted({t for t in re.split(r'[^0-9]+', job) if len(t) >= 3})
            c['jobs'].append(dict(job=job, numbers=numbers, site=name, site_id=site_id, list_id=lst.get('id'), url=lst.get('webUrl'), modified=lst.get('lastModifiedDateTime')))
    value = sorted(out.values(), key=lambda c: c['client'].lower())
    for c in value:
        c['jobs'].sort(key=lambda j: j['job'])
    _clients_cache.update(until=time.time() + CLIENTS_TTL, value=value)
    return value

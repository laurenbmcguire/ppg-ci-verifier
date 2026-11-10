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
    return _collection(f'/sites/{urllib.parse.quote(site_id, safe="")}/lists?$select=id,displayName,webUrl,lastModifiedDateTime&$top=500', opener)


# what the last site discovery could and could not read, shown with the client list (a failed read is never "no lists")
last_discovery = dict(at=None, search_failed=None, unreadable_sites=[])
_JOB_LIST = re.compile(r'^\s*data\s*#|#\s*\d{4}\b|^\s*\d{4}-[A-Z0-9]', re.I)


def client_sites(opener=None, refresh=False):
    """Every client site: the configured ones plus any site on PPG's SharePoint with a "Data #" job list,
    leaving out tests and demos. [(name, site_id, lists)], cached for 30 minutes; lists are fetched in parallel."""
    from concurrent.futures import ThreadPoolExecutor
    key = (opener is None, os.environ.get('SHAREPOINT_SITES', ''), os.environ.get('SHAREPOINT_HOST', ''))
    if opener is None and not refresh and _site_cache.get('key') == key and _site_cache['value'] is not None and _site_cache['until'] > time.time():
        return _site_cache['value']
    sites = {sid: name for name, sid in _configured_sites(opener)}
    try:   # needs the app's Sites.Read.All; without it only the configured sites are used
        for site in _collection('/sites?search=*&$select=id,name,displayName,webUrl&$top=200', opener):
            name = site.get('name') or (site.get('webUrl') or '').rstrip('/').rsplit('/', 1)[-1]
            if site.get('id') and name and not _is_test(name, site.get('displayName')):
                sites.setdefault(site['id'], name)
    except SharePointError as exc:
        search_error = str(exc)
    else:
        search_error = None
    configured = {sid for _, sid in _configured_sites(opener)}
    with ThreadPoolExecutor(8) as pool:
        got = dict(zip(sites, pool.map(lambda sid: _lists_or_error(sid, opener), sites)))
    lists = {sid: v[0] for sid, v in got.items()}
    last_discovery.update(at=time.time(), search_failed=search_error,
                          unreadable_sites=[dict(site=sites[sid], error=v[1]) for sid, v in got.items() if v[1] and not _is_test(sites[sid])])
    value = [(name, sid, lists[sid]) for sid, name in sites.items()
             if not _is_test(name) and (sid in configured or any(_JOB_LIST.search(l.get('displayName') or '') for l in lists[sid]))]
    _site_cache.update(key=key, until=time.time() + SITES_TTL, value=value)
    return value


def _lists_or_error(site_id, opener=None):
    try:
        return _site_lists(site_id, opener), None
    except SharePointError as exc:
        return [], str(exc)[:200]


def _safe_lists(site_id, opener=None):
    try:
        return _site_lists(site_id, opener)
    except SharePointError:
        return []


def _sites(opener=None):
    for name, sid, _ in client_sites(opener):
        yield name, sid


def fetch_export(job, day, opener=None, schedule=None, *, site_id=None, list_id=None, refresh=False):
    """Return (filename, bytes, meta) for the job's daily export, or raise SharePointNotFound."""
    if not configured():
        raise SharePointNotConfigured('SharePoint lookup is not set up yet (Microsoft app registration missing).')
    if not job or not day:
        raise SharePointNotFound('The report\'s job number or date could not be determined, so SharePoint could not be searched.')
    if site_id or list_id:
        found = list_day(job, day, opener, schedule, site_id=site_id, list_id=list_id, refresh=refresh)
        if found:
            return found
        raise SharePointNotFound('No records for this date in the assigned source; no other site or export was substituted.')
    raise SharePointError('Report lookup requires the verified site_id and list_id. No cross-site export fallback is allowed.')


def recent_exports(job, before_day, limit=10, opener=None, *, site_id=None, list_id=None, schedule=None):
    """Earlier daily exports for a job: [(day, filename, bytes)], newest first, up to `limit`. Read-only."""
    if site_id or list_id:
        # Earlier production data must come from the assigned list, not job-named
        # files whose provenance cannot be established by their directory name.
        return list_history(job, before_day, limit, opener, schedule, site_id=site_id, list_id=list_id)
    raise SharePointError('History requires the verified site_id and list_id; cross-site export fallback is disabled.')


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


def _find_list(job, opener=None, *, site_id=None, list_id=None):
    """Resolve exact identity or fail closed on an ambiguous job label.

    Production callers should supply both IDs from a verified assignment. A job
    number, modification time, or global registry default is not report identity.
    """
    if bool(site_id) != bool(list_id):
        raise SharePointError('Both site_id and list_id are required for an exact source.')
    if site_id:
        path = f'/sites/{urllib.parse.quote(site_id, safe="")}/lists/{urllib.parse.quote(list_id, safe="")}'
        lst = _get(path, opener)
        if not lst or lst.get('id') != list_id:
            raise SharePointNotFound('The assigned SharePoint list was not found. No alternate source was selected.')
        # IDs, not a potentially stale display name, are authoritative here.
        match = re.search(r'/sites/([^/]+)', urllib.parse.urlsplit(lst.get('webUrl') or '').path)
        name = urllib.parse.unquote(match.group(1)) if match else site_id
        return name, site_id, lst
    exact, near = [], []
    _seen_lists.clear()
    for name, sid, site_lists in client_sites(opener):
        for lst in site_lists:
            if (lst.get('displayName') or '').lower().startswith('data #'):
                _seen_lists.append(lst['displayName'])
            score = _list_match(job, lst.get('displayName'), lst.get('webUrl'))
            if score:
                (exact if score == 2 else near).append((name, sid, lst))
    found = exact or near
    identities = {(f[1], f[2]['id']): f for f in found}
    if len(identities) > 1:
        raise SharePointError('Ambiguous SharePoint job: select the verified site_id and list_id. No newest-list fallback is allowed.')
    return next(iter(identities.values()), None)


def pagination_collection(url):
    """Canonical Graph collection identity; continuation may change only query."""
    if not isinstance(url, str) or not url or any(ord(c) < 32 for c in url):
        raise SharePointError('Invalid SharePoint pagination destination.')
    parsed = urllib.parse.urlsplit(url)
    if parsed.fragment or parsed.username or parsed.password:
        raise SharePointError('Invalid SharePoint pagination credentials or fragment.')
    path = parsed.path
    if parsed.scheme or parsed.netloc:
        if parsed.scheme != 'https' or parsed.netloc != 'graph.microsoft.com' or not path.startswith('/v1.0/'):
            raise SharePointError('Unexpected SharePoint pagination destination.')
        path = path[len('/v1.0'):]
    elif not path.startswith('/') or path.startswith('//'):
        raise SharePointError('Invalid SharePoint pagination destination.')
    # Reject ambiguous separators, double encoding and path traversal rather than
    # depending on how a proxy normalizes them before forwarding credentials.
    if re.search(r'%(?:2f|5c|00)', path, re.I):
        raise SharePointError('Encoded path separators are not allowed in pagination.')
    try:
        path = urllib.parse.unquote(path, errors='strict')
    except (UnicodeError, ValueError):
        raise SharePointError('Invalid encoded SharePoint pagination path.') from None
    if any(ord(c) < 32 for c in path) or '%' in path or '\\' in path or '//' in path or any(p in ('.','..') for p in path.split('/')):
        raise SharePointError('Ambiguous SharePoint pagination path.')
    return path


def _collection(url, opener=None, *, cap=None):
    """Complete Graph collection or an explicit error; never return a partial success."""
    out, visited = [], set()
    collection = pagination_collection(url)
    while url:
        if url in visited:
            raise SharePointError('SharePoint pagination repeated a cursor; capture is incomplete.')
        visited.add(url)
        if pagination_collection(url) != collection:
            raise SharePointError('SharePoint pagination changed the requested collection; capture stopped.')
        page = _get(url, opener)
        if not isinstance(page, dict) or not isinstance(page.get('value'), list):
            raise SharePointError('SharePoint collection page is unavailable or malformed; capture is incomplete.')
        out.extend(page['value'])
        url = page.get('@odata.nextLink')
        if url is not None and (not isinstance(url, str) or not url):
            raise SharePointError('Invalid SharePoint next-page cursor; capture is incomplete.')
        if cap is not None and (len(out) > cap or (len(out) == cap and url)):
            raise SharePointError(f'SharePoint capture reached its {cap}-item safety limit with incomplete coverage. No partial source was returned.')
    return out


def assignment_identity(assignment, opener=None):
    """Convert an already resolved assignment to complete IDs; never infer from job."""
    from hosting import registry
    if assignment.get('status') not in registry.RESOLVED or not assignment.get('list_id'):
        raise SharePointError('Resolve the report client and exact source assignment before checking SharePoint.')
    sid = assignment.get('site_id')
    if not sid and assignment.get('site'):
        from hosting.reconcile import site_id_for
        sid = site_id_for(assignment['site'], opener)
    if not sid:
        raise SharePointError('The assigned site ID is unavailable; no other source was selected.')
    return dict(site_id=sid, list_id=assignment['list_id'])


def _list_items(job, opener=None, *, site_id=None, list_id=None, refresh=False):
    """Complete source capture cached by tenant, application and exact list identity.

    The tuple API is retained; list metadata carries explicit capture evidence.
    Test/custom openers bypass the shared cache to avoid crossing authentication contexts.
    """
    found = _find_list(job, opener, site_id=site_id, list_id=list_id)
    if not found:
        return None
    name, sid, lst = found
    key = (os.environ.get('MS_TENANT_ID'), os.environ.get('MS_CLIENT_ID'), sid, lst['id'])
    hit = _list_cache.get(key) if opener is None and not refresh else None
    if hit and hit[0] > time.time():
        return hit[1]
    base = f'/sites/{urllib.parse.quote(sid, safe="")}/lists/{urllib.parse.quote(lst["id"], safe="")}'
    cols = _collection(base + '/columns?$top=500', opener)
    names = {c['name']: c.get('displayName') or c['name'] for c in cols if not c.get('hidden') and not c.get('readOnly')}
    items = _collection(base + '/items?$expand=fields&$top=999', opener, cap=50000)
    identifiers = [it.get('id') for it in items]
    if any(not i for i in identifiers) or len(set(identifiers)) != len(identifiers):
        raise SharePointError('Missing or repeated item IDs across source pages; recapture required.')
    from datetime import datetime, timezone
    lst = dict(lst, _capture=dict(site_id=sid, list_id=lst['id'], terminal=True,
               remaining_cursor=None, record_count=len(items), columns=cols,
               captured_at=datetime.now(timezone.utc).isoformat()))
    value = (name, lst, names, items)
    if opener is None:
        _list_cache[key] = (time.time() + LIST_TTL, value)
    return value


def _stamp(item):
    fields = item.get('fields') or {}
    return fields.get('Date0') or fields.get('Date') or item.get('createdDateTime')


def _item_day(item, schedule=None, columns=None, *, site_id=None, list_id=None, date_contract=None):
    """Business Date wins, even if typed dateTime; never silently use submission."""
    from hosting.reconcile import item_day
    day, basis = item_day(item.get('fields') or {}, columns or {}, schedule, site_id=site_id, list_id=list_id, date_contract=date_contract)
    return day if basis in ('business_date', 'business_date_with_time') else None


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
    """Preserve recorded Shift; expose any time-based alternative as a suggestion only."""
    from hosting.shifts import local_time, assign, number, label
    shift_key = next((k for k, v in names.items() if re.sub(r'[^a-z]', '', str(v).lower()) == 'shift'), None) or 'Shift'
    names = dict(names)
    names[shift_key] = names.get(shift_key, 'Shift')
    names['ShiftPicked'], names['ScanTime'] = 'Picked shift', 'Scan time'
    names['ShiftFromTime'] = 'Time-based shift suggestion'
    out, scans = [], {}
    for it in rows:
        f = dict(it.get('fields') or {})
        local = local_time(_stamp(it))
        shift, report_day = assign(local, schedule) if local else (None, None)
        f['ShiftPicked'] = f.get(shift_key)
        f['ScanTime'] = f'{local.month}/{local.day} at {(local.hour % 12) or 12}:{local.minute:02d} {"AM" if local.hour < 12 else "PM"}' if local else None
        if shift:
            f['ShiftFromTime'] = number(shift['name'])  # suggestion only; preserve recorded Shift
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


def list_day(job, day, opener=None, schedule=None, *, site_id=None, list_id=None, refresh=False):
    """(filename, csv bytes, meta) built from the job's data list for one day, or None.
    With shift times, the day runs from the first shift's start to the last shift's end, and each scan's
    shift comes from its time."""
    if not configured() or not job or not day:
        return None
    data = _list_items(job, opener, site_id=site_id, list_id=list_id, refresh=refresh)
    if not data:
        return None
    site, lst, names, items = data
    capture = lst.get('_capture') or {}
    columns = {c['name']: c for c in capture.get('columns', [])}
    identity = dict(site_id=capture.get('site_id'), list_id=capture.get('list_id'))
    from hosting.source_date_contracts import capture_contract
    identity['date_contract'] = capture_contract(identity['site_id'], identity['list_id'], columns, opener)
    if any(_item_day(it, schedule, columns, **identity) is None for it in items):
        raise SharePointError('Source includes missing business dates or timestamps without a verified site/list timezone contract. Confirm source date settings before comparison; plain calendar dates remain usable.')
    rows = [it for it in items if _item_day(it, schedule, columns, **identity) == day]
    if not rows:
        return None
    modified = max((it.get('lastModifiedDateTime') or '') for it in rows) or None
    meta = dict(site=site, path=f'list: {lst.get("displayName")}', web_url=lst.get('webUrl'), modified=modified,
                job=job, day=day, source_kind='list', rows=len(rows), capture=dict(capture,date_contract=identity['date_contract']))
    if schedule:
        names, rows, scans = _by_time(names, rows, schedule)
        meta.update(scan_shifts=scans, shift_times=schedule, timeline=timeline(rows, schedule))
    return (f'{job} {day} (SharePoint list).csv', _items_csv(names, rows), meta)


def list_history(job, before_day, limit=10, opener=None, schedule=None, *, site_id=None, list_id=None, refresh=False):
    """[(day, filename, csv bytes)] from the job's data list for earlier days, newest first."""
    data = _list_items(job, opener, site_id=site_id, list_id=list_id, refresh=refresh) if configured() and job and before_day else None
    if not data:
        return []
    _, lst, names, items = data
    capture = lst.get('_capture') or {}
    columns = {c['name']: c for c in capture.get('columns', [])}
    identity = dict(site_id=capture.get('site_id'), list_id=capture.get('list_id'))
    from hosting.source_date_contracts import capture_contract
    identity['date_contract'] = capture_contract(identity['site_id'], identity['list_id'], columns, opener)
    if any(_item_day(it, schedule, columns, **identity) is None for it in items):
        raise SharePointError('History includes records without a confirmed business date or verified site/list timezone contract. Confirm source date settings before selecting history.')
    by_day = {}
    for it in items:
        d = _item_day(it, schedule, columns, **identity)
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

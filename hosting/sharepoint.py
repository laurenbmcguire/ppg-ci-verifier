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


def _sites(opener=None):
    names = [s.strip() for s in os.environ.get('SHAREPOINT_SITES', '').split(',') if s.strip()]
    host = os.environ.get('SHAREPOINT_HOST', 'ppgquality.sharepoint.com')
    if names:
        for name in names:
            if name not in _site_ids:
                site = _get(f'/sites/{host}:/sites/{urllib.parse.quote(name)}', opener)
                _site_ids[name] = site['id'] if site else None
            if _site_ids[name]:
                yield name, _site_ids[name]
        return
    found = _get('/sites?search=*&$select=id,name,webUrl&$top=200', opener) or {}
    for site in found.get('value', []):
        yield site.get('name') or site['id'], site['id']


def fetch_export(job, day, opener=None):
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
    found = list_day(job, day, opener)
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


def _list_match(job, display):
    """2 = same job written in any order ("Data #2219-HANDS-HMMA" for HANDS-HMMA-2219), 1 = same job number, 0 = no."""
    name = (display or '').strip().lower()
    if not name.startswith('data') and name != (job or '').lower():
        return 0
    want, have = _tokens(job), _tokens(display)
    if want and want == have:
        return 2
    numbers = {t for t in want if t.isdigit() and len(t) >= 3}
    return 1 if numbers and numbers & have else 0


def _find_list(job, opener=None):
    """The job's "Data #<job>" list. Field lists put the job number first (Data #2219-HANDS-HMMA) while reports
    often put it last (HANDS-HMMA-2219), so match on the job's parts in any order, then on the job number alone."""
    best = None
    _seen_lists.clear()
    for name, site_id in _sites(opener):
        lists = _get(f'/sites/{site_id}/lists?$select=id,displayName,webUrl&$top=500', opener) or {}
        for lst in lists.get('value', []):
            if (lst.get('displayName') or '').lower().startswith('data #'):
                _seen_lists.append(lst['displayName'])
            score = _list_match(job, lst.get('displayName'))
            if score == 2:
                return name, site_id, lst
            if score == 1 and best is None:
                best = (name, site_id, lst)
    return best


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


def _item_day(item):
    from datetime import datetime
    from zoneinfo import ZoneInfo
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


def list_day(job, day, opener=None):
    """(filename, csv bytes, meta) built from the job's data list for one day, or None."""
    if not configured() or not job or not day:
        return None
    data = _list_items(job, opener)
    if not data:
        return None
    site, lst, names, items = data
    rows = [it for it in items if _item_day(it) == day]
    if not rows:
        return None
    modified = max((it.get('lastModifiedDateTime') or '') for it in rows) or None
    return (f'{job} {day} (SharePoint list).csv', _items_csv(names, rows),
            dict(site=site, path=f'list: {lst.get("displayName")}', web_url=lst.get('webUrl'), modified=modified,
                 job=job, day=day, source_kind='list', rows=len(rows)))


def list_history(job, before_day, limit=10, opener=None):
    """[(day, filename, csv bytes)] from the job's data list for earlier days, newest first."""
    data = _list_items(job, opener) if configured() and job and before_day else None
    if not data:
        return []
    _, _, names, items = data
    by_day = {}
    for it in items:
        d = _item_day(it)
        if d and d < before_day:
            by_day.setdefault(d, []).append(it)
    return [(d, f'{job} {d} (SharePoint list).csv', _items_csv(names, by_day[d])) for d in sorted(by_day, reverse=True)[:limit]]

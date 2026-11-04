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
    raise SharePointNotFound(f'No SharePoint daily export found for {job} on {day}.' + (f' Looked in {len(tried)} place(s).' if tried else ' No client sites are configured.'))

"""Read-only, exact-source date interpretation; runtime settings are mandatory.

The source identities, internal Date/dateOnly field and Central Time selector
(value 11) were verified in SharePoint settings on 2026-11-10. Date0 is not a
business-Date fallback. No plant-location or global timezone assumptions.
"""
from datetime import datetime, timezone, timedelta
import re

SITE = 'ppgquality.sharepoint.com,1c5b45bb-c484-42a8-920b-4b9e978d9299,89739af3-35fb-4b0e-ae82-03a761aa0a41'
LIST = 'e402d426-4595-47bc-a305-a0704df18258'
LISTS = (LIST, 'd1a655b8-dc19-4d0f-bad2-35ed9999ccc2')
SITE_URL = 'https://ppgquality.sharepoint.com/sites/SLAlabama_AlexanderCity'
REVISION = 'selected-source-Date-runtime-v2'
GUID = r'[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}'
MAX_AGE_SECONDS = 600
EXPECTED_TIMEZONE = {'Id':11, 'Description':'(UTC-06:00) Central Time (US and Canada)',
                     'Information':{'Bias':360,'StandardBias':0,'DaylightBias':-60}}


def contract_for(site_id, list_id, columns):
    """Reviewed policy only, never authority to parse a timestamp without a capture."""
    from hosting import photo_embed
    parts = str(site_id or '').split(',')
    if (len(parts) != 3 or parts[0] != photo_embed._host() or not parts[0].endswith('.sharepoint.com')
            or not all(re.fullmatch(GUID, p) for p in parts[1:]) or not re.fullmatch(GUID, str(list_id or ''))):
        return None
    field = (columns or {}).get('Date') or {}
    if (field.get('name') != 'Date' or (field.get('dateTime') or {}).get('format') != 'dateOnly'
            or not re.fullmatch(r'[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}', str(field.get('id') or ''))):
        return None
    return dict(revision=REVISION, site_id=site_id, list_id=list_id, field='Date', format='dateOnly',
                column_id=field.get('id'), zone='America/Chicago', encoding='aware-local-midnight',
                policy_reviewed_on='2026-11-10')


def _timezone_evidence(payload):
    if not isinstance(payload,dict):return None
    data=payload.get('d',payload)
    if not isinstance(data,dict):return None
    info=data.get('Information')
    if not isinstance(info,dict):return None
    selected={k:data.get(k) for k in ('Id','Description')}
    selected['Information']={k:info.get(k) for k in ('Bias','StandardBias','DaylightBias')}
    # Booleans and alternate data types cannot impersonate numeric evidence.
    if type(selected['Id']) is not int or any(type(v) is not int for v in selected['Information'].values()):return None
    return selected if selected==EXPECTED_TIMEZONE else None


def _validated_site_url(site_id, url):
    from urllib.parse import urlsplit, unquote
    if not isinstance(url,str) or any(ord(c)<32 for c in url):return None
    try:
        p=urlsplit(url)
        if (p.scheme!='https' or p.netloc!=str(site_id).split(',')[0] or p.query or p.fragment
                or re.search(r'%(?:2f|5c|00)',p.path,re.I)):return None
        decoded=unquote(p.path,errors='strict')
        if any(ord(c)<32 for c in decoded):return None
        if not re.fullmatch(r'/sites/[^/]+/?',decoded) or any(c in decoded for c in ('\\','%')):return None
        if decoded.rstrip('/').rsplit('/',1)[-1] in ('.','..'):return None
        return url.rstrip('/')
    except (ValueError,UnicodeError):return None


def _site_url(site_id,opener=None):
    from hosting.reconcile import _fetch
    from urllib.parse import quote
    path='/sites/'+quote(site_id,safe='')+'?$select=id,webUrl'
    status,data=_fetch(path,metadata_opener('https://graph.microsoft.com/v1.0'+path,opener),retries=0)
    if status!=200 or not isinstance(data,dict) or data.get('id')!=site_id:return None
    return _validated_site_url(site_id,data.get('webUrl'))


def metadata_opener(regional_url, opener=None):
    """No redirects for regional metadata or its existing-token request."""
    import os
    import urllib.request
    import urllib.parse
    import urllib.error
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None
    transport=opener or urllib.request.build_opener(NoRedirect()).open
    tenant=urllib.parse.quote(os.environ.get('MS_TENANT_ID',''))
    token_url='https://login.microsoftonline.com/'+tenant+'/oauth2/v2.0/token'
    def open_exact(request,timeout=None):
        allowed=(request.full_url==regional_url and request.get_method()=='GET') or (request.full_url==token_url and request.get_method()=='POST')
        if not allowed:raise urllib.error.URLError('Unexpected source-date metadata destination')
        return transport(request,timeout=timeout)
    return open_exact


def capture_contract(site_id,list_id,columns,opener=None):
    """One allowlisted GET, no retries/fallback; reuse the existing photo certificate."""
    policy=contract_for(site_id,list_id,columns)
    if not policy:return None
    from hosting import photo_embed
    if not photo_embed.certificate_ready():return None
    try:
        site_url=_site_url(site_id,opener)
        if not site_url:return None
        regional_url=site_url+'/_api/web/RegionalSettings/TimeZone'
        status,payload=photo_embed._get(regional_url,metadata_opener(regional_url,opener))
        evidence=_timezone_evidence(payload) if status==200 else None
    except (RuntimeError,KeyError,TypeError,ValueError,OSError):
        return None
    if not evidence:return None
    stamp=datetime.now(timezone.utc)
    return dict(policy, site_url=site_url, observed_timezone=evidence, captured_at=stamp.isoformat(),
                expires_at=(stamp+timedelta(seconds=MAX_AGE_SECONDS)).isoformat())


def receipt_contract_current(contract):
    """Fail closed on policy change, malformed evidence or the bounded capture TTL."""
    if not isinstance(contract,dict):return False
    columns={'Date':{'id':contract.get('column_id'),'name':'Date','dateTime':{'format':'dateOnly'}}}
    policy=contract_for(contract.get('site_id'),contract.get('list_id'),columns)
    if not policy or any(contract.get(k)!=v for k,v in policy.items()):return False
    if not _validated_site_url(contract.get('site_id'),contract.get('site_url')):return False
    if _timezone_evidence(contract.get('observed_timezone')) is None:return False
    try:
        captured=datetime.fromisoformat(contract['captured_at']);expires=datetime.fromisoformat(contract['expires_at'])
        now=datetime.now(timezone.utc)
        return (captured.tzinfo is not None and expires.tzinfo is not None and
                captured<=now<expires and 0<(expires-captured).total_seconds()<=MAX_AGE_SECONDS)
    except (KeyError,TypeError,ValueError):return False


def contract_matches(contract,site_id,list_id,columns):
    policy=contract_for(site_id,list_id,columns)
    return bool(policy and receipt_contract_current(contract) and all(contract.get(k)==v for k,v in policy.items()))

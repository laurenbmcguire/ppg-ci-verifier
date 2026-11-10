"""Read-only exact-source capture with bounded terminal page and photo receipts."""
from copy import deepcopy
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime
import json
import re
from urllib.parse import quote, urlsplit
from hosting import source_first as sf

MAX_LIST_ITEMS = 50000
MAX_CAPTURE_BYTES = 8 * 1024 * 1024
MAX_PHOTOS = 12
MAX_PHOTO_BYTES = 2 * 1024 * 1024
MAX_TOTAL_PHOTO_BYTES = 8 * 1024 * 1024


class CaptureFailure(sf.ReviewError):
    def __init__(self, message, evidence):
        super().__init__(message)
        self.evidence = deepcopy(evidence)


def cooldown(headers):
    value = (headers or {}).get('Retry-After')
    if value is None:
        return {'retry_after': None, 'not_before': None, 'known': False}
    try:
        if re.fullmatch(r'[0-9]+', str(value)):
            due = datetime.now(timezone.utc) + timedelta(seconds=int(value))
        else:
            due = parsedate_to_datetime(value)
            if due.tzinfo is None: raise ValueError('Unknown timezone')
        return {'retry_after': str(value), 'not_before': due.astimezone(timezone.utc).isoformat(), 'known': True}
    except (TypeError, ValueError, OverflowError):
        return {'retry_after': str(value), 'not_before': None, 'known': False}


def graph_fetch(url):
    """Single request, no retries/redirects. Preserve the provider cooldown."""
    import urllib.request
    import urllib.error
    from hosting import sharepoint
    sharepoint.pagination_collection(url)
    full = url if url.startswith('https://') else sharepoint.GRAPH + url
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl): return None
    req = urllib.request.Request(full, headers={'Authorization': 'Bearer ' + sharepoint._token_value()})
    try:
        with urllib.request.build_opener(NoRedirect()).open(req, timeout=30) as response:
            raw = response.read(MAX_CAPTURE_BYTES + 1)
            if len(raw) > MAX_CAPTURE_BYTES: raise sf.ReviewError('Source page exceeds the capture byte limit.')
            return response.status, json.loads(raw), {}
    except urllib.error.HTTPError as exc:
        return exc.code, None, {'Retry-After': exc.headers.get('Retry-After')}
    except (OSError, ValueError):
        return 0, None, {}


def response(fetch, url):
    result = fetch(url)
    return (*result, {}) if len(result) == 2 else result


def substantive(item):
    return {k: item.get(k) for k in ('id','eTag','fields','lastModifiedDateTime','createdDateTime')}


def collection(url, kind, pages, fetch, raw_pages=None):
    from hosting.sharepoint import pagination_collection
    original = pagination_collection(url)
    records, seen = [], set()
    raw_pages = raw_pages if raw_pages is not None else []
    def fail(message, status=None, headers=None):
        evidence = dict(pages=deepcopy(pages), raw_pages=deepcopy(raw_pages), failed_request=url,
                        failed_request_hash=sf.digest(url), status=status, items_terminal=False,
                        columns_terminal=False, remaining_cursor=url, captured_at=sf.now())
        if status in (429, 503): evidence['cooldown'] = cooldown(headers)
        raise CaptureFailure(message, evidence)
    while url is not None:
        if not isinstance(url, str) or not url or url in seen or len(seen) >= 100:
            fail('Invalid, repeated, or excessive source pagination. No partial snapshot was accepted.')
        try:
            valid = pagination_collection(url) == original
        except RuntimeError:
            valid = False
        if not valid:
            fail('Source pagination changed the exact collection.')
        seen.add(url)
        status, page, headers = response(fetch, url)
        raw_pages.append(dict(request=url, status=status, response=deepcopy(page), headers=headers))
        if status != 200 or not isinstance(page, dict) or not isinstance(page.get('value'), list):
            fail(f'Source capture stopped at HTTP {status}. Partial evidence was retained privately; it cannot be approved.', status, headers)
        nxt = page.get('@odata.nextLink')
        if nxt is not None and (not isinstance(nxt, str) or not nxt):
            fail('Invalid source next-page cursor.')
        pages.append(dict(kind=kind, collection=original, request_hash=sf.digest(url), page_hash=sf.digest(page),
                          status=status, count=len(page['value']), terminal=nxt is None,
                          next_cursor_hash=sf.digest(nxt) if nxt is not None else None))
        records.extend(deepcopy(page['value']))
        if len(records) > MAX_LIST_ITEMS or len(sf.canonical(records)) > MAX_CAPTURE_BYTES:
            fail('Source exceeds the pilot capture limit. No records were truncated.')
        url = nxt
    return records


def regional_capture(site_id, list_id, columns):
    import urllib.request
    import urllib.error
    from hosting.source_date_contracts import capture_contract
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl): return None
    transport=urllib.request.build_opener(NoRedirect()).open
    failures=[]
    def observe(request, timeout=None):
        try:
            return transport(request, timeout=timeout)
        except urllib.error.HTTPError as exc:
            if exc.code in (429,503):
                failures.append(dict(status=exc.code,failed_request=request.full_url,
                                     cooldown=cooldown({'Retry-After':exc.headers.get('Retry-After')}),
                                     remaining_cursor=request.full_url, pages=[], raw_pages=[]))
            raise
    contract=capture_contract(site_id,list_id,columns,opener=observe)
    if failures: raise CaptureFailure('Source regional metadata has a provider cooldown. No early retry is allowed.',failures[0])
    return contract


def photo_download(url):
    """Existing certificate, one exact HTTPS URL, no redirects, bounded response."""
    import urllib.request
    import urllib.error
    from hosting import photo_embed
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None
    request = urllib.request.Request(url, headers={'Authorization': 'Bearer ' + photo_embed._token()})
    try:
        with urllib.request.build_opener(NoRedirect()).open(request, timeout=20) as response:
            raw = response.read(MAX_PHOTO_BYTES + 1)
            return response.status, raw, {}
    except urllib.error.HTTPError as exc:
        return exc.code, b'', {'Retry-After':exc.headers.get('Retry-After')}
    except (OSError, RuntimeError):
        return 0, b'', {}


def _capture(scope, *, fetch=None, date_capture=None, photo_fetch=None, _evidence=None):
    from hosting.reconcile import item_day
    from hosting.source_date_contracts import capture_contract
    from hosting import photo_embed
    s = sf.scope(scope)
    evidence = _evidence if _evidence is not None else {}
    evidence.update(scope=s, captured_at=sf.now(), accepted=False)
    fetch = fetch or graph_fetch
    date_capture = date_capture or regional_capture
    photo_fetch = photo_fetch or photo_download
    base = '/sites/' + quote(s['site_id'], safe='') + '/lists/' + quote(s['list_id'], safe='')
    metadata_url = base + '?$select=id,displayName,webUrl,lastModifiedDateTime'
    status, metadata, headers = response(fetch, metadata_url)
    if status != 200 or not isinstance(metadata, dict) or metadata.get('id') != s['list_id']:
        raise CaptureFailure('Exact list identity could not be verified.', dict(status=status, failed_request=metadata_url, pages=[], raw_pages=[], remaining_cursor=metadata_url, cooldown=cooldown(headers) if status in (429,503) else None))
    pages, raw_pages = [], []
    evidence.update(pages=pages,raw_pages=raw_pages,list_metadata=metadata)
    raw_columns = collection(base + '/columns?$top=500', 'columns', pages, fetch, raw_pages)
    columns = {}
    for col in raw_columns:
        if not isinstance(col, dict) or not col.get('name') or col['name'] in columns:
            raise sf.ReviewError('Source schema has missing or duplicate column identities.')
        columns[col['name']] = col
    if (columns.get('Date', {}).get('dateTime') or {}).get('format') != 'dateOnly':
        raise sf.ReviewError('This pilot requires a verified Date/dateOnly business-date column.')
    date_contract = date_capture(s['site_id'], s['list_id'], columns)
    from hosting.source_date_contracts import contract_matches
    if not contract_matches(date_contract, s['site_id'], s['list_id'], columns):
        raise sf.ReviewError('Current exact source Date-column and regional-timezone evidence is required, including for literal calendar dates. No item capture was accepted.')
    all_records = collection(base + '/items?$expand=fields&$top=999', 'items', pages, fetch, raw_pages)
    seen, records, uncertain = set(), [], []
    for raw in all_records:
        if not isinstance(raw, dict) or not isinstance(raw.get('id'), str) or not re.fullmatch(r'[0-9]+', raw['id']) or raw['id'] in seen or not isinstance(raw.get('fields'), dict):
            raise sf.ReviewError('Source has missing/repeated IDs or malformed raw fields. No deduplication was performed.')
        seen.add(raw['id'])
        # This pilot pins internal Date; another date/submission column is never
        # a fallback for absent or unreadable business-date evidence.
        day, basis = item_day({'Date': raw['fields'].get('Date')}, columns, site_id=s['site_id'], list_id=s['list_id'], date_contract=date_contract)
        if day is None:
            uncertain.append(raw['id'])
        if day == s['business_date']:
            records.append(dict(id=raw['id'], version=raw.get('eTag') or raw['fields'].get('_UIVersionString'),
                                modified=raw.get('lastModifiedDateTime'), day=day, date_basis=basis, raw=deepcopy(raw['fields'])))
    if len(records) > sf.MAX_ROWS:
        raise sf.ReviewError('The selected day exceeds the manual pilot row limit. No rows were truncated.')
    # Photo association is based only on the exact source field's filename. The
    # reviewer explicitly chooses representative defect photos after capture.
    photos, blobs, total, photo_attempts = [], {}, 0, 0
    from hosting.sheet_roles import source_url_identity
    identity = source_url_identity(metadata.get('webUrl'), list_only=True)
    source_host = s['site_id'].split(',')[0]
    photo_base = None
    if identity and identity.get('origin') == 'https://' + source_host and source_host == photo_embed._host():
        photo_base = f"https://{source_host}/sites/{quote(identity['site'], safe='')}/_api/web/lists(guid'{s['list_id']}')"
    photo_columns = [name for name, col in columns.items() if col.get('thumbnail') is not None or col.get('type') == 'Thumbnail']
    # Graph often omits the thumbnail facet. JSON serverRelativeUrl/fileName is
    # recognized only as a photo candidate, never as permission to visit a URL.
    for row in records:
        candidates = set(photo_columns)
        for name, value in row['raw'].items():
            if isinstance(value, str) and value.startswith('{'):
                try:
                    parsed = json.loads(value)
                    if isinstance(parsed, dict) and parsed.get('fileName') and ('serverRelativeUrl' in parsed or parsed.get('type') == 'thumbnail'):
                        candidates.add(name)
                except ValueError:
                    pass
        for field in sorted(candidates):
            value = row['raw'].get(field)
            if value in (None, ''):
                continue
            try:
                data = json.loads(value) if isinstance(value, str) else value
            except ValueError:
                data = None
            name = data.get('fileName') if isinstance(data, dict) else None
            photo = dict(record_id=row['id'], record_hash=sf.digest(row), field=field,
                         field_hash=sf.digest(value), title=columns.get(field, {}).get('displayName', field),
                         status='unavailable', reason='Unsupported or unavailable source photo.')
            photos.append(photo)
            if not photo_base or not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9 _().-]{1,180}\.(?:jpe?g|png)', name, re.I):
                continue
            if photo_attempts >= MAX_PHOTOS:
                photo['reason'] = 'Photo attempt limit reached; this photo was not requested and no substitute was inserted.'
                continue
            photo_attempts += 1
            # Re-read the exact item to reject source changes during photo capture.
            item_url = base + '/items/' + row['id'] + '?$expand=fields'
            st, before, item_headers = response(fetch, item_url)
            if st in (429,503):
                raise CaptureFailure('Exact source item has a provider cooldown before photo capture.',dict(status=st,failed_request=item_url,remaining_cursor=item_url,cooldown=cooldown(item_headers),pages=pages,raw_pages=raw_pages))
            original = next(r for r in all_records if r['id'] == row['id'])
            if st != 200 or not isinstance(before,dict):
                raise CaptureFailure('Exact source item could not be read before photo capture.',dict(status=st,failed_request=item_url,remaining_cursor=item_url,pages=pages,raw_pages=raw_pages))
            if substantive(before) != substantive(original):
                raise sf.ReviewError('Source record changed during photo capture. Recapture and review it again.')
            photo_url = photo_base + f"/items({int(row['id'])})/AttachmentFiles('{quote(name, safe='')}')/$value"
            st, blob, photo_headers = response(photo_fetch, photo_url)
            if st in (429,503):
                raise CaptureFailure('Source photo provider has a cooldown. No early retry is allowed.',dict(status=st,failed_request=photo_url,remaining_cursor=photo_url,cooldown=cooldown(photo_headers),pages=pages,raw_pages=raw_pages))
            size = photo_embed.image_size(blob) if st == 200 and 0 < len(blob) <= MAX_PHOTO_BYTES else None
            st_after, after, item_headers = response(fetch, item_url)
            if st_after in (429,503):
                raise CaptureFailure('Exact source item has a provider cooldown after photo capture.',dict(status=st_after,failed_request=item_url,remaining_cursor=item_url,cooldown=cooldown(item_headers),pages=pages,raw_pages=raw_pages))
            if st_after != 200 or not isinstance(after,dict):
                raise CaptureFailure('Exact source item could not be read after photo capture.',dict(status=st_after,failed_request=item_url,remaining_cursor=item_url,pages=pages,raw_pages=raw_pages))
            if substantive(after) != substantive(original):
                raise sf.ReviewError('Source record changed while its photo was downloaded. Recapture required.')
            if not size or any(n <= 0 or n > 20000 for n in size) or total + len(blob) > MAX_TOTAL_PHOTO_BYTES:
                photo['reason'] = 'Photo unavailable, unsupported, or over the safe size limit.'
                continue
            sha = sf.digest(blob)
            photo.update(status='supported', reason=None, sha256=sha, bytes=len(blob), filename=name,
                         media_type='image/png' if blob.startswith(b'\x89PNG') else 'image/jpeg')
            blobs[sha] = blob
            total += len(blob)
    out = dict(version=sf.VERSION, scope=s, captured_at=sf.now(), columns=columns, date_contract=date_contract,
               records=records, uncertain_dates=uncertain, raw_all_records=all_records, raw_pages=raw_pages, list_metadata=metadata,
               photos=photos, coverage=dict(items_terminal=True, columns_terminal=True, remaining_cursor=None,
                                          all_list_count=len(all_records), scoped_count=len(records), pages=pages, photo_attempts=photo_attempts,
                                          meaning='Complete observed traversal only; not proof that production or shifts have finished.'))
    return sf.seal(out), blobs


def capture(scope, **kwargs):
    evidence = {}
    try:
        return _capture(scope, _evidence=evidence, **kwargs)
    except CaptureFailure as exc:
        combined = dict(evidence, **exc.evidence, accepted=False)
        raise CaptureFailure(str(exc), combined) from None
    except sf.ReviewError as exc:
        raise CaptureFailure(str(exc), dict(evidence, accepted=False)) from None

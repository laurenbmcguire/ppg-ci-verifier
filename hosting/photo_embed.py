"""A downloadable copy of a report with each record's SharePoint photos embedded. Read-only toward SharePoint.

The stored original is never changed. The copy gets one extra tab ("SharePoint Photos", or "Defect Photos" when the
workbook has none) with a row per record that has a photo: SharePoint item, identifier, part, and the label and
defect pictures themselves. Every other part of the workbook is copied byte for byte (existing pictures included).
Photos are the inspector's original files from SharePoint, never resized, replaced or substituted; a record whose
photo cannot be read says so in its row.
"""
import hashlib
import io
import json
import os
import re
import struct
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor
from xml.sax.saxutils import escape

PHOTO_FIELDS = ('PictureLabel', 'PictureDefect', 'Photo2', 'Photo20', 'PictureLH', 'PictureRH', 'PictureBack_x002d_LH')
MAX_PHOTOS = 600
WORKERS = 8
_sp_token = {'value': None, 'expires': 0}


class PhotoError(RuntimeError):
    pass


# ---------------------------------------------------------------- SharePoint (read-only)
def _host():
    return os.environ.get('SHAREPOINT_HOST', 'ppgquality.sharepoint.com')


def _token(opener=None):
    """App token for SharePoint itself (list attachments are not reachable through Microsoft Graph)."""
    from hosting import sharepoint as sp
    if _sp_token['value'] and _sp_token['expires'] > time.time() + 60:
        return _sp_token['value']
    url = f'https://login.microsoftonline.com/{urllib.parse.quote(os.environ["MS_TENANT_ID"])}/oauth2/v2.0/token'
    form = {'client_id': os.environ['MS_CLIENT_ID'], 'grant_type': 'client_credentials', 'scope': f'https://{_host()}/.default'}
    if certificate_ready():   # SharePoint only accepts app sign-in with a certificate, not with a client secret
        form.update(client_assertion_type='urn:ietf:params:oauth:client-assertion-type:jwt-bearer', client_assertion=client_assertion(url))
    else:
        form['client_secret'] = os.environ['MS_CLIENT_SECRET']
    body = urllib.parse.urlencode(form).encode()
    try:
        with sp._open(urllib.request.Request(url, data=body, method='POST'), opener) as r:
            data = json.loads(r.read())
    except urllib.error.HTTPError as exc:
        raise PhotoError(f'Microsoft sign-in for SharePoint photos failed (HTTP {exc.code}).') from None
    _sp_token.update(value=data['access_token'], expires=time.time() + int(data.get('expires_in', 3000)))
    return _sp_token['value']


_server_cert = {'key': None, 'thumb': None}
CERT_KEY = 'sharepoint_photo_cert'


def certificate_ready():
    return bool((os.environ.get('MS_SP_CERT_KEY') and os.environ.get('MS_SP_CERT_THUMBPRINT')) or (_server_cert['key'] and _server_cert['thumb']))


def _cipher():
    import base64
    from cryptography.fernet import Fernet
    # the stored private key is only readable with the app's own client secret, which lives in Vercel, not in the database
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(('ppg-verifier-cert|' + os.environ['MS_CLIENT_SECRET']).encode()).digest()))


def use_stored_certificate(stored):
    """Load a certificate made by make_certificate() (as kept in settings). Returns True when usable."""
    if not stored:
        return False
    try:
        _server_cert.update(key=_cipher().decrypt(stored['key_enc'].encode()).decode(), thumb=stored['thumbprint'])
        return True
    except Exception:
        return False


def make_certificate(days=730):
    """A new self-signed certificate. Returns (stored dict with the encrypted private key, public .cer PEM bytes).
    The private key is never returned or shown; only the public certificate leaves the server."""
    from datetime import datetime, timedelta, timezone
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'PPG Verifier SharePoint photos')])
    now = datetime.now(timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - timedelta(minutes=5))
            .not_valid_after(now + timedelta(days=days)).sign(key, hashes.SHA256()))
    pem = cert.public_bytes(serialization.Encoding.PEM)
    key_pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()
    stored = dict(key_enc=_cipher().encrypt(key_pem.encode()).decode(), thumbprint=cert.fingerprint(hashes.SHA1()).hex().upper(),
                  cert_pem=pem.decode(), created=now.isoformat(), expires=cert.not_valid_after_utc.isoformat())
    return stored, pem


def client_assertion(audience, now=None):
    """A signed JWT proving the app holds the certificate's private key (RS256, x5t = SHA-1 thumbprint)."""
    import base64, uuid
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding
    b64 = lambda b: base64.urlsafe_b64encode(b).rstrip(b'=').decode()
    key_pem = os.environ.get('MS_SP_CERT_KEY') or _server_cert['key']
    thumb = bytes.fromhex((os.environ.get('MS_SP_CERT_THUMBPRINT') or _server_cert['thumb']).replace(':', '').strip())
    now = int(now or time.time())
    head = {'alg': 'RS256', 'typ': 'JWT', 'x5t': b64(thumb)}
    claims = {'aud': audience, 'iss': os.environ['MS_CLIENT_ID'], 'sub': os.environ['MS_CLIENT_ID'], 'jti': str(uuid.uuid4()),
              'nbf': now - 60, 'exp': now + 600}
    signing = b64(json.dumps(head, separators=(',', ':')).encode()) + '.' + b64(json.dumps(claims, separators=(',', ':')).encode())
    key = serialization.load_pem_private_key(key_pem.replace('\\n', '\n').encode(), password=None)
    return signing + '.' + b64(key.sign(signing.encode(), padding.PKCS1v15(), hashes.SHA256()))


def _get(url, opener=None, raw=False):
    from hosting import sharepoint as sp
    url = url.replace(' ', '%20')
    req = urllib.request.Request(url, headers={'Authorization': 'Bearer ' + _token(opener), 'Accept': 'application/json;odata=nometadata'})
    try:
        with sp._open(req, opener) as r:
            data = r.read()
            return 200, (data if raw else json.loads(data))
    except urllib.error.HTTPError as exc:
        detail = ''
        try:
            detail = exc.read()[:300].decode('utf-8', 'replace')
        except Exception:
            pass
        return exc.code, detail
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        return 0, 'no answer'


def fetch_photos(site, list_id, items, opener=None):
    """{item: [dict(field, title, name, bytes|None, error)]} for the given SharePoint item IDs."""
    base = f'https://{_host()}/sites/{urllib.parse.quote(site)}/_api/web/lists(guid\'{list_id}\')'
    status, fields = _get(base + "/fields?$filter=Hidden eq false and TypeAsString eq 'Thumbnail'&$select=Title,InternalName", opener)
    if status in (401, 403):
        raise PhotoError('SharePoint refused the app\'s photo request (HTTP %s). %s' % (status, _permission_hint(fields)))
    if status != 200:
        raise PhotoError(f'The photo columns of the SharePoint list could not be read (HTTP {status or "no answer"}).')
    cols = [(f['InternalName'], f['Title']) for f in fields.get('value', [])]
    if not cols:
        return {}

    def one(item):
        st, data = _get(base + f'/items({int(item)})?$select=Id,' + ','.join(c for c, _ in cols), opener)
        if st != 200:
            return item, [dict(field=None, title='Photo', name=None, bytes=None, error=f'item could not be read (HTTP {st})')]
        out = []
        for col, title in cols:
            val = data.get(col)
            if not val:
                continue
            try:
                name = json.loads(val).get('fileName')
            except (ValueError, AttributeError):
                name = None
            if not name:
                out.append(dict(field=col, title=title, name=None, bytes=None, error='no file name in SharePoint'))
                continue
            st2, blob = _get(base + f"/items({int(item)})/AttachmentFiles('{urllib.parse.quote(name)}')/$value", opener, raw=True)
            out.append(dict(field=col, title=title, name=name, bytes=blob if st2 == 200 else None,
                            error=None if st2 == 200 else f'photo could not be downloaded (HTTP {st2})'))
        return item, out

    with ThreadPoolExecutor(WORKERS) as pool:
        return dict(pool.map(one, list(items)[:MAX_PHOTOS]))


def _permission_hint(detail):
    text = str(detail or '')
    if not certificate_ready() or 'Unsupported app only token' in text or 'app only' in text.lower():
        return ('SharePoint only lets the app download photos when it signs in with a certificate. An admin needs to download the '
                'verifier certificate (/api/settings/sharepoint-cert), upload it to the app registration in Entra ID, and grant '
                'SharePoint "Sites.Read.All" (application) with admin consent.')
    return ('The app registration needs the SharePoint "Sites.Read.All" application permission (under SharePoint, not Microsoft Graph) '
            'with admin consent.')


# ---------------------------------------------------------------- building the copy (no existing part is rewritten except to register the new tab)
def image_size(data):
    """(width, height) of a JPEG or PNG, or None."""
    if data[:8] == b'\x89PNG\r\n\x1a\n' and len(data) >= 24:
        return struct.unpack('>II', data[16:24])
    if data[:2] == b'\xff\xd8':
        i = 2
        while i + 9 < len(data):
            if data[i] != 0xFF:
                i += 1
                continue
            marker = data[i + 1]
            if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                h, w = struct.unpack('>HH', data[i + 5:i + 9])
                return w, h
            seg = struct.unpack('>H', data[i + 2:i + 4])[0]
            i += 2 + seg
    return None


def _ext(data):
    return 'png' if data[:8] == b'\x89PNG\r\n\x1a\n' else ('gif' if data[:3] == b'GIF' else 'jpeg')


def _col(n):
    s = ''
    n += 1
    while n:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def _cell(ref, value):
    if value is None or value == '':
        return f'<c r="{ref}"/>'
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return f'<c r="{ref}"><v>{value}</v></c>'
    return f'<c r="{ref}" t="inlineStr"><is><t xml:space="preserve">{escape(str(value))}</t></is></c>'


def embed(raw, rows, sheet_title=None):
    """raw: the original .xlsx bytes. rows: [dict(item, identifier, part, defect, photos=[dict(title, bytes, error, name)])].
    Returns the new workbook bytes."""
    zin = zipfile.ZipFile(io.BytesIO(raw))
    names = zin.namelist()
    wb = zin.read('xl/workbook.xml').decode('utf-8')
    existing = re.findall(r'<sheet\b[^>]*\bname="([^"]+)"', wb)
    title = sheet_title or ('Defect Photos' if 'Defect Photos' not in existing else 'SharePoint Photos')
    n = 2
    while title in existing:
        title = f'SharePoint Photos {n}'; n += 1
    sheet_no = 1 + max([int(m) for m in re.findall(r'xl/worksheets/sheet(\d+)\.xml', ' '.join(names))] or [0])
    draw_no = 1 + max([int(m) for m in re.findall(r'xl/drawings/drawing(\d+)\.xml', ' '.join(names))] or [0])
    sheet_ids = [int(x) for x in re.findall(r'<sheet\b[^>]*\bsheetId="(\d+)"', wb)] or [0]
    wb_rels = zin.read('xl/_rels/workbook.xml.rels').decode('utf-8')
    rid_nums = [int(x) for x in re.findall(r'Id="rId(\d+)"', wb_rels)] or [0]
    rid = f'rId{max(rid_nums) + 1}'

    labels = sorted({p['title'] for r in rows for p in r['photos']}, key=lambda t: (0 if 'label' in t.lower() else 1, t))
    heads = ['SharePoint item', 'Identifier', 'Part number', 'Recorded defect'] + labels
    pic_w, pic_h, row_pt = 220, 165, 128
    xml_rows = [f'<row r="1"><c r="A1" t="inlineStr"><is><t>PPG Quality Record Photos (from SharePoint)</t></is></c></row>',
                '<row r="2">' + ''.join(_cell(f'{_col(i)}2', h) for i, h in enumerate(heads)) + '</row>']
    anchors, media, img_rels = [], [], []
    for i, r in enumerate(rows):
        rn = i + 3
        cells = [_cell(f'A{rn}', int(r['item']) if str(r['item']).isdigit() else r['item']), _cell(f'B{rn}', r.get('identifier')),
                 _cell(f'C{rn}', r.get('part')), _cell(f'D{rn}', r.get('defect'))]
        by_title = {}
        for p in r['photos']:
            by_title.setdefault(p['title'], p)
        for j, t in enumerate(labels):
            p = by_title.get(t)
            ref = f'{_col(4 + j)}{rn}'
            if p and p.get('bytes'):
                cells.append(f'<c r="{ref}"/>')
                k = len(media) + 1
                ext = _ext(p['bytes'])
                path = f'xl/media/sp_photo_{sheet_no}_{k}.{ext}'
                media.append((path, p['bytes']))
                img_rels.append(f'<Relationship Id="rIdImg{k}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" Target="../media/sp_photo_{sheet_no}_{k}.{ext}"/>')
                size = image_size(p['bytes']) or (pic_w, pic_h)
                scale = min(pic_w / size[0], pic_h / size[1]) if size[0] and size[1] else 1
                cx, cy = int(size[0] * scale * 9525), int(size[1] * scale * 9525)
                anchors.append(
                    f'<xdr:oneCellAnchor><xdr:from><xdr:col>{4 + j}</xdr:col><xdr:colOff>38100</xdr:colOff><xdr:row>{rn - 1}</xdr:row><xdr:rowOff>38100</xdr:rowOff></xdr:from>'
                    f'<xdr:ext cx="{cx}" cy="{cy}"/><xdr:pic><xdr:nvPicPr><xdr:cNvPr id="{k + 1}" name="{escape(t)} {escape(str(r["item"]))}" descr="{escape(p.get("name") or "")}"/>'
                    '<xdr:cNvPicPr><a:picLocks noChangeAspect="1"/></xdr:cNvPicPr></xdr:nvPicPr>'
                    f'<xdr:blipFill><a:blip r:embed="rIdImg{k}"/><a:stretch><a:fillRect/></a:stretch></xdr:blipFill>'
                    f'<xdr:spPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="{cx}" cy="{cy}"/></a:xfrm><a:prstGeom prst="rect"><a:avLst/></a:prstGeom></xdr:spPr></xdr:pic><xdr:clientData/></xdr:oneCellAnchor>')
            else:
                cells.append(_cell(ref, (p or {}).get('error') and f'Photo not available: {p["error"]}' or ''))
        xml_rows.append(f'<row r="{rn}" ht="{row_pt}" customHeight="1">' + ''.join(cells) + '</row>')
    cols = '<cols><col min="1" max="1" width="16" customWidth="1"/><col min="2" max="3" width="24" customWidth="1"/><col min="4" max="4" width="22" customWidth="1"/>' + \
           (f'<col min="5" max="{4 + len(labels)}" width="34" customWidth="1"/>' if labels else '') + '</cols>'
    sheet_xml = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                 '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
                 '<sheetPr><pageSetUpPr fitToPage="1"/></sheetPr><sheetViews><sheetView workbookViewId="0"><pane ySplit="2" topLeftCell="A3" activePane="bottomLeft" state="frozen"/></sheetView></sheetViews>'
                 f'{cols}<sheetData>{"".join(xml_rows)}</sheetData>'
                 '<pageMargins left="0.5" right="0.5" top="0.5" bottom="0.5" header="0.3" footer="0.3"/>'
                 '<pageSetup orientation="landscape" fitToWidth="1" fitToHeight="0"/>'
                 + ('<drawing r:id="rIdDraw1"/>' if anchors else '') + '</worksheet>')
    drawing_xml = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                   '<xdr:wsDr xmlns:xdr="http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing" '
                   'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
                   + ''.join(anchors) + '</xdr:wsDr>')
    rel_head = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    sheet_rels = rel_head + f'<Relationship Id="rIdDraw1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/drawing" Target="../drawings/drawing{draw_no}.xml"/></Relationships>'
    drawing_rels = rel_head + ''.join(img_rels) + '</Relationships>'

    wb_new = wb.replace('</sheets>', f'<sheet xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" '
                                     f'name="{escape(title)}" sheetId="{max(sheet_ids) + 1}" r:id="{rid}"/></sheets>', 1)
    wb_rels_new = wb_rels.replace('</Relationships>', f'<Relationship Id="{rid}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet{sheet_no}.xml"/></Relationships>', 1)
    ct = zin.read('[Content_Types].xml').decode('utf-8')
    adds = f'<Override PartName="/xl/worksheets/sheet{sheet_no}.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
    if anchors:
        adds += f'<Override PartName="/xl/drawings/drawing{draw_no}.xml" ContentType="application/vnd.openxmlformats-officedocument.drawing+xml"/>'
    for ext, mime in (('jpeg', 'image/jpeg'), ('png', 'image/png'), ('gif', 'image/gif')):
        if any(p.endswith('.' + ext) for p, _ in media) and f'Extension="{ext}"' not in ct:
            adds += f'<Default Extension="{ext}" ContentType="{mime}"/>'
    ct_new = ct.replace('</Types>', adds + '</Types>', 1)

    out = io.BytesIO()
    with zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED) as zout:
        for item in zin.infolist():
            data = zin.read(item)
            if item.filename == 'xl/workbook.xml':
                data = wb_new.encode('utf-8')
            elif item.filename == 'xl/_rels/workbook.xml.rels':
                data = wb_rels_new.encode('utf-8')
            elif item.filename == '[Content_Types].xml':
                data = ct_new.encode('utf-8')
            zout.writestr(item, data)
        zout.writestr(f'xl/worksheets/sheet{sheet_no}.xml', sheet_xml)
        if anchors:
            zout.writestr(f'xl/worksheets/_rels/sheet{sheet_no}.xml.rels', sheet_rels)
            zout.writestr(f'xl/drawings/drawing{draw_no}.xml', drawing_xml)
            zout.writestr(f'xl/drawings/_rels/drawing{draw_no}.xml.rels', drawing_rels)
        for path, data in media:
            zout.writestr(path, data, compress_type=zipfile.ZIP_STORED)
    return out.getvalue()


def photos_filename(filename):
    stem = re.sub(r'\.xlsx$', '', filename, flags=re.I)
    return f'{stem} (with photos).xlsx'


def build(filename, raw, site, list_id, opener=None):
    """(new filename, bytes, summary). Records come from the report's own record rows (the authoritative sheet)."""
    from hosting.spreadsheets import report_and_export_records
    records, _ = report_and_export_records(filename, raw)
    ordered, seen = [], set()
    for r in records:
        item = (r.get('source') or {}).get('item')
        if item and item not in seen:
            seen.add(item); ordered.append(r)
    if not ordered:
        raise PhotoError('This report has no SharePoint item IDs on its record rows, so its photos cannot be matched.')
    started = time.time()
    found = fetch_photos(site, list_id, [r['source']['item'] for r in ordered], opener)
    rows = []
    for r in ordered:
        photos = found.get(r['source']['item']) or []
        if photos:
            rows.append(dict(item=r['source']['item'], identifier=r['labels'].get('identifier'), part=r['labels'].get('part'),
                             defect=r['labels'].get('defect'), photos=photos))
    embedded = sum(1 for x in rows for p in x['photos'] if p.get('bytes'))
    missing = sum(1 for x in rows for p in x['photos'] if not p.get('bytes'))
    if not rows:
        raise PhotoError('None of this report\'s SharePoint records have photos attached.')
    data = embed(raw, rows)
    existing = re.findall(r'<sheet\b[^>]*\bname="([^"]+)"', zipfile.ZipFile(io.BytesIO(raw)).read('xl/workbook.xml').decode('utf-8'))
    tab = 'Defect Photos' if 'Defect Photos' not in existing else 'SharePoint Photos'
    return photos_filename(filename), data, dict(tab=tab, records=len(ordered), with_photos=len(rows), embedded=embedded, missing=missing,
                                                 seconds=round(time.time() - started, 1), sha256=hashlib.sha256(data).hexdigest())

"""Embedded photos and photo links in a workbook: listed, hashed and tied to their records. Never production.

Pictures are read straight from the file's drawing parts, so nothing is decoded, resized or replaced: each one is
identified by the SHA-256 of its original bytes and the cell it is anchored to. When the picture's row carries a
SharePoint item ID it is tied to that record. Photo links are listed; links that cannot work for anyone else
(a local file path, a non-https address) are flagged. Sample or placeholder images are never substituted.
"""
import hashlib
import posixpath
import re
import zipfile
import io
import xml.etree.ElementTree as ET

NS = {'m': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main',
      'r': 'http://schemas.openxmlformats.org/officeDocument/2006/relationships',
      'rel': 'http://schemas.openxmlformats.org/package/2006/relationships',
      'xdr': 'http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing',
      'a': 'http://schemas.openxmlformats.org/drawingml/2006/main'}
MAX_IMAGES = 5000
PHOTO_HEADERS = ('photo', 'picture', 'image', 'label photo', 'defect photo')


def _rels(z, part):
    folder, name = posixpath.split(part)
    path = posixpath.join(folder, '_rels', name + '.rels')
    if path not in z.namelist():
        return {}
    root = ET.fromstring(z.read(path))
    return {r.get('Id'): posixpath.normpath(posixpath.join(folder, r.get('Target'))) if not r.get('Target', '').startswith('/') else r.get('Target').lstrip('/')
            for r in root.findall('rel:Relationship', NS)}


def _col(n):
    s = ''
    n += 1
    while n:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def images(raw):
    """[{sheet, cell, row, media, sha256, bytes}] for every picture anchored on a worksheet."""
    out = []
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        names = set(z.namelist())
        if 'xl/workbook.xml' not in names:
            return out
        wb = ET.fromstring(z.read('xl/workbook.xml'))
        wb_rels = _rels(z, 'xl/workbook.xml')
        for sh in wb.findall('m:sheets/m:sheet', NS):
            part = wb_rels.get(sh.get(f'{{{NS["r"]}}}id'))
            if not part or part not in names:
                continue
            root = ET.fromstring(z.read(part))
            srels = _rels(z, part)
            for d in root.findall('m:drawing', NS):
                dpart = srels.get(d.get(f'{{{NS["r"]}}}id'))
                if not dpart or dpart not in names:
                    continue
                droot = ET.fromstring(z.read(dpart))
                drels = _rels(z, dpart)
                for anchor in list(droot):
                    frm = anchor.find('xdr:from', NS)
                    blip = anchor.find('.//a:blip', NS)
                    if blip is None:
                        continue
                    media = drels.get(blip.get(f'{{{NS["r"]}}}embed'))
                    if frm is not None:
                        col, row = int(frm.find('xdr:col', NS).text), int(frm.find('xdr:row', NS).text)
                    else:
                        pos = anchor.find('xdr:pos', NS)
                        col = row = None
                    data = z.read(media) if media in names else None
                    out.append(dict(sheet=sh.get('name'), row=None if row is None else row + 1, cell=None if row is None else f'{_col(col)}{row + 1}',
                                    media=media, sha256=hashlib.sha256(data).hexdigest() if data else None, bytes=len(data) if data else 0,
                                    missing=data is None))
                    if len(out) >= MAX_IMAGES:
                        return out
    return out


def _norm(v):
    return re.sub(r'\s+', ' ', re.sub(r'[^a-z0-9]+', ' ', str(v).lower())).strip()


def inventory(raw, sources, dataset_selection=None):
    """Pictures with their record association, photo links, and findings for broken ones."""
    rows_by = {title: list(rows) for title, rows in sources}

    def id_column(rows):
        for r_i, row in enumerate(rows[:15]):
            for c_i, v in enumerate(row):
                if isinstance(v, str) and _norm(v) in ('source item id', 'item id', 'sharepoint item id'):
                    return r_i + 1, c_i
        return None, None

    try:
        pics = images(raw)
    except (zipfile.BadZipFile, ET.ParseError, KeyError, ValueError):
        pics = []
    for p in pics:
        rows = rows_by.get(p['sheet']) or []
        head, col = id_column(rows)
        item = None
        if col is not None and p['row'] and p['row'] > head and p['row'] - 1 < len(rows):
            v = rows[p['row'] - 1][col] if col < len(rows[p['row'] - 1]) else None
            item = str(int(v)) if isinstance(v, (int, float)) and not isinstance(v, bool) else (v.strip() if isinstance(v, str) and v.strip().isdigit() else None)
        p['item'] = item
    links, broken = [], []
    for title, rows in rows_by.items():
        heads = {}
        for r_i, row in enumerate(rows[:15]):
            found = {c_i: _norm(v) for c_i, v in enumerate(row) if isinstance(v, str) and any(w in _norm(v) for w in PHOTO_HEADERS)}
            if found:
                heads = found
                start = r_i + 1
                break
        for c_i in heads:
            for r_i in range(start, len(rows)):
                v = rows[r_i][c_i] if c_i < len(rows[r_i]) else None
                if not isinstance(v, str) or not v.strip():
                    continue
                text = v.strip()
                if re.match(r'(?i)^(https?://|file:|[a-z]:\\|\\\\)', text):
                    entry = dict(sheet=title, cell=f'{_col(c_i)}{r_i + 1}', link=text[:300])
                    links.append(entry)
                    if not text.lower().startswith('https://'):
                        broken.append(entry)
    from hosting.sheet_roles import PHOTO_WORDS
    is_photo = lambda p: bool(p.get('item')) or any(w in PHOTO_WORDS for w in _norm(p['sheet']).split())
    photos = [p for p in pics if is_photo(p)]   # logos and banner images are not inspection photos
    return dict(images=photos, image_count=len(photos), other_images=len(pics) - len(photos), linked_to_records=sum(1 for p in photos if p.get('item')),
                links=links[:500], inaccessible_links=broken[:500], production=False,
                note='Photos are evidence, never production. Original image bytes are kept unchanged in the stored file.')

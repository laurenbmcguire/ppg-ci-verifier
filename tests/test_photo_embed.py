"""Download with SharePoint photos: pictures embedded in a copy; the original's parts are untouched."""
import io
import json
import unittest
import zipfile
from unittest.mock import patch

from openpyxl import load_workbook

from hosting import photo_embed
from hosting.spreadsheets import inspect_spreadsheet
from tests.sl_daily_fixture import PNG, sl_daily, SITE, LIST

BINDING = dict(site_id="synthetic-site-id", list_url=f"https://ppg.sharepoint.com/sites/{SITE}/Lists/{LIST}")


def fake_sharepoint(photo_for=('432', '434'), fail=None):
    def get(url, opener=None, raw=False):
        if fail:
            return fail
        if '/fields?' in url:
            return 200, {'value': [{'InternalName': 'PictureLabel', 'Title': 'Picture Label'}, {'InternalName': 'PictureDefect', 'Title': 'Picture Defect'}]}
        if '/AttachmentFiles(' in url:
            return (404, 'gone') if 'missing' in url else (200, PNG)
        item = url.split('/items(')[1].split(')')[0]
        if item not in photo_for:
            return 200, {'Id': int(item)}
        return 200, {'Id': int(item), 'PictureLabel': json.dumps({'fileName': f'label_{item}.png'}),
                     'PictureDefect': json.dumps({'fileName': 'missing.png'}) if item == '434' else None}
    return get


class WithPhotos(unittest.TestCase):
    def setUp(self):
        host = patch("hosting.photo_embed._host", return_value="ppg.sharepoint.com")
        host.start()
        self.addCleanup(host.stop)

    def test_photos_are_embedded_in_their_rows_and_the_original_parts_are_kept(self):
        raw = sl_daily(photos=False)
        with patch('hosting.photo_embed._get', fake_sharepoint()):
            name, data, info = photo_embed.build('SL Alabama - 2214 - 2026-11-06.xlsx', raw, SITE, 'list-1', **BINDING)
        self.assertEqual(name, 'SL Alabama - 2214 - 2026-11-06 (with photos).xlsx')
        self.assertEqual((info['with_photos'], info['embedded'], info['missing'], info['tab']), (2, 2, 1, 'Defect Photos'))
        wb = load_workbook(io.BytesIO(data))
        tab = wb['Defect Photos']
        self.assertEqual(len(tab._images), 2)
        self.assertEqual([tab['A3'].value, tab['A4'].value], [432, 434])
        self.assertIn('Photo not available', tab['F4'].value)
        before, after = zipfile.ZipFile(io.BytesIO(raw)), zipfile.ZipFile(io.BytesIO(data))
        for n in before.namelist():
            if n not in ('xl/workbook.xml', 'xl/_rels/workbook.xml.rels', '[Content_Types].xml'):
                self.assertEqual(before.read(n), after.read(n), n)
        with after.open([n for n in after.namelist() if n.startswith('xl/media/sp_photo')][0]) as f:
            self.assertEqual(f.read(), PNG)   # the inspector's original file, not resized or replaced

    def test_the_copy_still_counts_the_same_production(self):
        with patch('hosting.photo_embed._get', fake_sharepoint()):
            _, data, _ = photo_embed.build('r.xlsx', sl_daily(photos=False), SITE, 'L', **BINDING)
        r = inspect_spreadsheet('r.xlsx', data)
        self.assertEqual((r['record_count'], r['totals']['inspected']['total']), (5, '400'))

    def test_existing_defect_photos_tab_is_kept_and_a_new_tab_is_added(self):
        with patch('hosting.photo_embed._get', fake_sharepoint()):
            _, data, info = photo_embed.build('r.xlsx', sl_daily(photos=True), SITE, 'L', **BINDING)
        self.assertEqual(info['tab'], 'SharePoint Photos')
        self.assertIn('Defect Photos', load_workbook(io.BytesIO(data)).sheetnames)

    def test_refused_app_token_explains_the_admin_step(self):
        with patch('hosting.photo_embed._get', fake_sharepoint(fail=(401, 'Unsupported app only token.'))):
            with self.assertRaises(photo_embed.PhotoError) as caught:
                photo_embed.build('r.xlsx', sl_daily(), SITE, 'L', **BINDING)
        self.assertIn('certificate', str(caught.exception))

    def test_urls_sent_to_sharepoint_have_no_spaces(self):
        seen = []
        def opener(req, timeout=None):
            seen.append(req.full_url); raise OSError('offline')
        with patch('hosting.photo_embed._token', return_value='t'):
            photo_embed._get("https://x/_api/web/lists(guid'1')/fields?$filter=Hidden eq false", opener)
        self.assertNotIn(' ', seen[0])

    def test_image_size_reader(self):
        self.assertEqual(photo_embed.image_size(PNG), (1, 1))


if __name__ == '__main__':
    unittest.main()


class CertificateSignIn(unittest.TestCase):
    def test_client_assertion_is_signed_with_the_certificate_key(self):
        import base64, os
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding, rsa
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()
        env = {'MS_SP_CERT_KEY': pem, 'MS_SP_CERT_THUMBPRINT': 'AB' * 20, 'MS_CLIENT_ID': 'app-1'}
        with patch.dict(os.environ, env):
            self.assertTrue(photo_embed.certificate_ready())
            jwt = photo_embed.client_assertion('https://login/token', now=1000)
        head, body, sig = jwt.split('.')
        pad = lambda s: s + '=' * (-len(s) % 4)
        claims = json.loads(base64.urlsafe_b64decode(pad(body)))
        self.assertEqual((claims['iss'], claims['aud'], claims['exp']), ('app-1', 'https://login/token', 1600))
        key.public_key().verify(base64.urlsafe_b64decode(pad(sig)), f'{head}.{body}'.encode(), padding.PKCS1v15(), hashes.SHA256())

    def test_server_made_certificate_keeps_its_key_encrypted_and_signs(self):
        import os
        with patch.dict(os.environ, {'MS_CLIENT_SECRET': 'app-secret', 'MS_CLIENT_ID': 'app-1'}):
            for k in ('MS_SP_CERT_KEY', 'MS_SP_CERT_THUMBPRINT'):
                os.environ.pop(k, None)
            stored, pem = photo_embed.make_certificate()
            self.assertTrue(pem.startswith(b'-----BEGIN CERTIFICATE-----'))
            self.assertNotIn('PRIVATE KEY', json.dumps(stored))            # only the encrypted form is stored
            self.assertTrue(photo_embed.use_stored_certificate(stored))
            self.assertEqual(len(photo_embed.client_assertion('https://login/token').split('.')), 3)
        with patch.dict(os.environ, {'MS_CLIENT_SECRET': 'a-different-secret'}):
            photo_embed._server_cert.update(key=None, thumb=None)
            self.assertFalse(photo_embed.use_stored_certificate(stored))   # unreadable without the app's own secret

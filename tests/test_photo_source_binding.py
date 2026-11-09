"""Exact source-bound photo export; all metadata and photo requests are mocked."""
import io
from contextlib import nullcontext
from unittest.mock import Mock, patch

import pytest
from openpyxl import load_workbook

from hosting import photo_embed, sharepoint
from tests.test_photo_package import original, PNG

SITE = 'Synthetic'
SID = 'exact-site-id'
LID = 'exact-list-id'
URL = 'https://ppgquality.sharepoint.com/sites/Synthetic/Lists/Exact'
BOUND = {'item': '123', 'site_id': SID, 'list_id': LID}
LINKED = {'item': '123', 'url': URL + '/123_.000', 'site': SITE, 'list': 'Exact'}


def record(source):
    return dict(source=source, labels={'identifier': 'Synthetic row'})


def build(sources, **binding):
    kwargs = dict(site_id=SID, list_url=URL)
    kwargs.update(binding)
    return photo_embed.build('synthetic.xlsx', original(True), SITE, LID, **kwargs)


@pytest.fixture
def fetch():
    with patch('hosting.photo_embed.fetch_photos', return_value={'123': [dict(title='Label', bytes=PNG)]}) as mocked:
        yield mocked


@pytest.mark.parametrize('source', [
    BOUND, LINKED, dict(BOUND, **{'url': URL + '/DispForm.aspx?ID=123'}),
    {'item': 123, 'site_id': SID, 'list_id': LID},
    dict(LINKED, url=URL.upper().replace('HTTPS:', 'https:') + '/123_.000'),
])
def test_exact_ids_or_verified_url_allow_photos(source, fetch):
    with patch('hosting.spreadsheets.report_and_export_records', return_value=([record(source)], [])):
        _, data, info = build([source])
    fetch.assert_called_once_with(SITE, LID, ['123'], None)
    assert info['embedded'] == 1
    assert load_workbook(io.BytesIO(data))[info['tab']]._images[0]._data() == PNG


@pytest.mark.parametrize('source', [
    None, {}, {'item': '123'}, dict(BOUND, site_id=None), dict(BOUND, list_id=None),
    dict(BOUND, site_id='wrong'), dict(BOUND, list_id='wrong'),
    dict(LINKED, url='https://foreign.example/sites/Synthetic/Lists/Exact/123_.000'),
    dict(LINKED, url=URL.replace('/Exact', '/Other') + '/123_.000'),
    dict(LINKED, url=URL + '/999_.000'), dict(LINKED, site='Wrong'), dict(LINKED, list='Wrong'),
    dict(LINKED, url='http://ppgquality.sharepoint.com/sites/Synthetic/Lists/Exact/123_.000'),
    dict(BOUND, url='malformed'), dict(BOUND, url=URL + '/999_.000'), dict(BOUND, site='Wrong'),
    dict(BOUND, item=True), dict(BOUND, item='123/other'),
])
def test_unbound_conflicting_or_foreign_record_blocks_before_fetch(source, fetch):
    # Put a valid duplicate first: validation must precede item-only deduplication.
    with patch('hosting.spreadsheets.report_and_export_records', return_value=([record(BOUND), record(source)], [])):
        with pytest.raises(photo_embed.PhotoError):
            build([BOUND, source])
    fetch.assert_not_called()


@pytest.mark.parametrize('binding', [
    {'site_id': None}, {'list_url': None}, {'list_url': 'https://foreign.example/sites/Synthetic/Lists/Exact'},
    {'list_url': URL.replace('/Synthetic/', '/Other/')}, {'list_url': URL + '/123_.000'},
])
def test_missing_or_wrong_verified_metadata_blocks_before_fetch(binding, fetch):
    with patch('hosting.spreadsheets.report_and_export_records', return_value=([record(BOUND)], [])):
        with pytest.raises(photo_embed.PhotoError):
            build([BOUND], **binding)
    fetch.assert_not_called()


def test_same_valid_item_is_fetched_once_after_all_rows_validate(fetch):
    with patch('hosting.spreadsheets.report_and_export_records', return_value=([record(BOUND), record(LINKED)], [])):
        _, _, info = build([BOUND, LINKED])
    fetch.assert_called_once_with(SITE, LID, ['123'], None)
    assert info['records'] == 1


def call_route(assignment, sources, metadata=None):
    from api.index import handler
    h = object.__new__(handler)
    h.path = '/api/spreadsheets/download-photos?id=' + 'a' * 64
    h.checked_route = Mock(return_value='/api/spreadsheets/download-photos')
    h.gate = Mock(return_value=None)
    h.can_hash = Mock(return_value=True)
    h.store = Mock(return_value=Mock(session=lambda: nullcontext('synthetic-only')))
    h.send = Mock(side_effect=lambda *args: args)
    h.fail = Mock(side_effect=lambda *args: args)
    inbox = Mock()
    inbox.assignment_for.return_value = assignment
    inbox.download.return_value = ('synthetic.xlsx', original(True))
    exact = metadata if metadata is not None else (SITE, SID, {'id': LID, 'webUrl': URL})
    with patch('api.index.production_forward', return_value=False), \
         patch('api.index.SpreadsheetInbox', return_value=inbox), \
         patch('hosting.review_summary.summarize', return_value={}), \
         patch('hosting.sharepoint.configured', return_value=True), \
         patch('hosting.sharepoint._find_list', return_value=exact) as lookup, \
         patch('hosting.photo_embed.use_stored_certificate'), \
         patch('hosting.spreadsheets.report_and_export_records', return_value=([record(s) for s in sources], [])):
        response = h.do_GET()
    return response, lookup


ASSIGNMENT = dict(status='verified', site=SITE, site_id=SID, list_id=LID, needs_person=False)


@pytest.mark.parametrize('changes', [dict(needs_person=True), dict(status='ambiguous'), dict(site_id=None), dict(list_id=None)])
def test_api_rejects_unresolved_or_partial_assignment_before_metadata_or_photos(changes, fetch):
    response, lookup = call_route(dict(ASSIGNMENT, **changes), [BOUND])
    assert response[0] == 409
    lookup.assert_not_called()
    fetch.assert_not_called()


def test_api_uses_exact_verified_metadata_instead_of_stale_display_site(fetch):
    response, lookup = call_route(dict(ASSIGNMENT, site='Stale display site'), [LINKED])
    assert response[0] == 200
    lookup.assert_called_once_with(None, site_id=SID, list_id=LID)
    fetch.assert_called_once_with(SITE, LID, ['123'], None)
    assert len(load_workbook(io.BytesIO(response[1]))['SharePoint Photos 3']._images) == 1


def test_api_rejects_cross_source_same_item_before_fetch(fetch):
    response, _ = call_route(ASSIGNMENT, [BOUND, dict(LINKED, url=URL.replace('/Exact', '/Wrong') + '/123_.000')])
    assert response[0] == 409
    fetch.assert_not_called()


@pytest.mark.parametrize('metadata', [
    (SITE, 'wrong-site-id', {'id': LID, 'webUrl': URL}),
    (SITE, SID, {'id': 'wrong-list-id', 'webUrl': URL}),
    (SITE, SID, {'id': LID}),
    (SITE, SID, {'id': LID, 'webUrl': 'https://foreign.example/sites/Synthetic/Lists/Exact'}),
])
def test_api_rejects_unverified_metadata(fetch, metadata):
    response, _ = call_route(ASSIGNMENT, [BOUND], metadata)
    assert response[0] == 409
    fetch.assert_not_called()


@pytest.mark.parametrize('count', [600, 601])
def test_unique_record_limit_is_explicit_and_deduplicated(count):
    sources = [dict(BOUND, item=str(i)) for i in range(count)]
    records = [record(s) for s in sources] + [record(sources[0])]
    with patch('hosting.spreadsheets.report_and_export_records', return_value=(records, [])), \
         patch('hosting.photo_embed.fetch_photos', return_value={str(i): [dict(title='Label', error='Unavailable')] for i in range(count)}) as fetch:
        if count > photo_embed.MAX_PHOTOS:
            with pytest.raises(photo_embed.PhotoError, match='600-record photo download limit'):
                build(sources)
            fetch.assert_not_called()
        else:
            _, data, info = build(sources)
            assert len(fetch.call_args.args[2]) == 600
            assert info['records'] == info['with_photos'] == info['missing'] == 600
            assert load_workbook(io.BytesIO(data))[info['tab']]['A602'].value == 599


def test_fetch_refuses_over_limit_before_any_provider_request():
    with patch('hosting.photo_embed._get') as get:
        with pytest.raises(photo_embed.PhotoError, match='600-record photo download limit'):
            photo_embed.fetch_photos(SITE, LID, range(601))
    get.assert_not_called()

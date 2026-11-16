"""A report laid out in headed sections with a hidden work sheet is checked as the report it is.

Only the rows under "INSPECTION RECORDS" are inspection rows. The results, billing and traceability lines under the
later headings, and the hidden work sheet the visible sheets read by formula, are not a second set of records.
"""
from hosting.adaptive_reports import inspect_adaptive

HEAD = ['Date', 'Shift', 'Part Number', 'Identifier', 'Quantity Inspected', 'Recorded NOK', 'Recorded OK', 'Stable source key']
KEY = '38718598-a97e-4388-bcc5-709ede868b21:'


def _sheet(rows):
    return ([['INSPECTION RECORDS'], HEAD] + rows + [[], [],
            ['2ND SHIFT RESULTS'],
            ['Boxes Inspected', None, 'Known inspected subtotal', 'Recorded OK subtotal', 'Known NOK subtotal', 'Defect Rate'],
            [None, None, 96, 96, 0, 0], [],
            ['BILLING'], ['Team Member', None, None, 'Hours on Shift'], [],
            ['Shift Productivity', None, None, None, 'parts inspected per labor hour'], [],
            ['SOURCE TRACEABILITY'], [4403, None, 'Maria Edwards', None, 'SL LIGHTECH', 'Source record']])


def _codes(result):
    return sorted({f['code'] for f in result['findings']})


def test_rows_under_later_headings_are_not_inspection_rows():
    rows = [['2026-11-14', '2', '00376452-OPT LH', 'LOT-1', 48, 0, 48, KEY + '4403'],
            ['2026-11-14', '2', '00376515-STD LH', 'LOT-2', 48, 0, 48, KEY + '4404']]
    result = inspect_adaptive([('2nd Shift', _sheet(rows))], {'limitations': []})
    assert [s['data_row_count'] for s in result['sheets']] == [2]
    assert not {'invalid_date', 'invalid_numeric_value', 'missing_part_number', 'missing_identifier', 'missing_value'} & set(_codes(result))


def test_a_real_mistake_in_an_inspection_row_is_still_found():
    rows = [['2026-11-14', '2', '00376452-OPT LH', 'LOT-1', 'forty', 0, 48, KEY + '4403']]
    assert 'invalid_numeric_value' in _codes(inspect_adaptive([('2nd Shift', _sheet(rows))], {'limitations': []}))


def test_without_a_heading_above_the_table_nothing_changes():
    rows = [HEAD, ['2026-11-14', '2', 'P1', 'LOT-1', 48, 0, 48, None], [], ['MORE ROWS'], ['2026-11-14', '2', 'P2', 'LOT-2', 'x', 0, 48, None]]
    result = inspect_adaptive([('2nd Shift', rows)], {'limitations': []})
    assert 'invalid_numeric_value' in _codes(result)   # the row after the capitals line is still checked


def test_separate_list_items_with_the_same_lot_are_not_duplicates():
    same = ['2026-11-14', '2', '00376452-OPT LH', '20261113', 48, 0, 48]
    result = inspect_adaptive([('2nd Shift', _sheet([same + [KEY + '4403'], same + [KEY + '4404'], same + [KEY + '4405']]))], {'limitations': []})
    assert not {'duplicate_row', 'duplicate_identifier'} & set(_codes(result))
    # the very same list item entered twice is still a duplicate
    result = inspect_adaptive([('2nd Shift', _sheet([same + [KEY + '4403'], same + [KEY + '4403']]))], {'limitations': []})
    assert {'duplicate_row', 'duplicate_identifier'} & set(_codes(result))
    # and without a key, identical rows are still reported
    result = inspect_adaptive([('2nd Shift', _sheet([same + [None], same + [None]]))], {'limitations': []})
    assert 'duplicate_row' in _codes(result)


def test_hidden_work_sheet_read_by_the_visible_sheet_is_not_counted_twice():
    calc = [['Stable source key', 'Business date', 'Recorded shift', 'Part number', 'Identifier', 'Inspected', 'Recorded OK', 'Recorded NOK'],
            [KEY + '4403', '2026-11-14', '2', 'P1', 'LOT-1', 48, 48, 0], [KEY + '4404', '2026-11-14', '2', 'P2', 'LOT-2', 48, 48, 0]]
    formulas = [[f'=_Calc!B{n}', f'=_Calc!C{n}', f'=_Calc!D{n}', f'=_Calc!E{n}', f'=_Calc!F{n}', f'=_Calc!H{n}', f'=_Calc!G{n}', f'=_Calc!A{n}'] for n in (2, 3)]
    shown = [['2026-11-14', '2', 'P1', 'LOT-1', 48, 0, 48, KEY + '4403'], ['2026-11-14', '2', 'P2', 'LOT-2', 48, 0, 48, KEY + '4404']]
    sources = [('2nd Shift', _sheet(formulas)), ('_Calc', calc)]
    cached = {'2nd Shift': _sheet(shown), '_Calc': calc}
    hidden = {'2nd Shift': dict(state='visible', rows=set(), cols={7}), '_Calc': dict(state='hidden', rows=set(), cols=set())}
    result = inspect_adaptive(sources, {'limitations': []}, cached=cached, hidden=hidden)
    codes = _codes(result)
    assert not {'duplicate_identifier', 'duplicate_row', 'hidden_sheet_with_data', 'hidden_columns_with_data'} & set(codes), codes
    assert {s['name']: s['data_row_count'] for s in result['sheets']} == {'2nd Shift': 2, '_Calc': 0}
    # a hidden sheet nothing reads is still pointed out
    result = inspect_adaptive([('2nd Shift', _sheet(shown)), ('_Calc', calc)], {'limitations': []}, hidden=hidden)
    assert 'hidden_sheet_with_data' in _codes(result)


def test_the_list_named_in_every_row_settles_the_client_when_two_lists_share_the_job_label():
    from hosting import registry
    site = 'SLAlabama_AlexanderCity'
    cands = [dict(client_key='SLAlabama', client='SL Alabama', site=site, site_id='s', job='2169-SLALAL-SLAL', list_id=KEY[:-1].upper()),
             dict(client_key='SLAlabama', client='SL Alabama', site=site, site_id='s', job='2169-SLALAL-SLAL #2', list_id='b85d1fdb-d7b3-4f5c-8223-8d8f737e06d5')]
    real = registry.candidates_for
    registry.candidates_for = lambda job, reg, snap: cands
    try:
        name = 'SL Alabama - SLALAL-SLAL-2169 - 2026-11-14.xlsx'
        keyed = dict(dataset_selection=dict(source_key_lists=[KEY[:-1]]))
        a = registry.resolve(keyed, name, 'SLALAL-SLAL-2169', '2026-11-14', [], [])
        assert a['status'] == 'matched' and a['job'] == '2169-SLALAL-SLAL' and a['list_id'] == KEY[:-1].upper()
        # nothing in the rows names a list: a person still chooses
        assert registry.resolve(dict(dataset_selection={}), name, 'SLALAL-SLAL-2169', '2026-11-14', [], [])['status'] == 'ambiguous'
        # rows from two lists, or a list that is not one of this job's: a person still chooses
        for lists in ([KEY[:-1], 'b85d1fdb-d7b3-4f5c-8223-8d8f737e06d5'], ['00000000-0000-4000-8000-000000000000']):
            assert registry.resolve(dict(dataset_selection=dict(source_key_lists=lists)), name, 'SLALAL-SLAL-2169', '2026-11-14', [], [])['status'] == 'ambiguous'
    finally:
        registry.candidates_for = real


def test_rows_tell_which_list_they_came_from_only_when_every_row_does():
    row = ['2026-11-14', '2', 'P1', 'LOT-1', 48, 0, 48]
    result = inspect_adaptive([('2nd Shift', _sheet([row + [KEY + '4403'], row[:3] + ['LOT-2', 48, 0, 48, KEY + '4404']]))], {'limitations': []})
    assert result['dataset_selection']['source_key_lists'] == [KEY[:-1]]
    result = inspect_adaptive([('2nd Shift', _sheet([row + [KEY + '4403'], row[:3] + ['LOT-2', 48, 0, 48, None]]))], {'limitations': []})
    assert result['dataset_selection']['source_key_lists'] == []

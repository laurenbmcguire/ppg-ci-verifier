"""The selected-template writer and a record with no part number at the source (SYNTHETIC data).

Found while testing regeneration: a day in which one record has no part number and others do could not be laid out at
all (the writer stopped with a sorting error). A missing part number is a fact about the source. The draft keeps it
missing: an empty cell on the record's row, its own line in the by-part summary labelled as unknown, and the typed
"null" in the source ledger. No part number is filled in from anywhere.
"""
import sys
import unittest
from copy import deepcopy
from io import BytesIO
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(Path(__file__).parent))
from hosting import source_first as sf, source_first_workbook as wb
from source_first_fixture import MAP, capture


def rows(parts):
    base = capture()['records'][0]
    out = []
    for n, part in enumerate(parts, 1):
        r = deepcopy(base); r['id'] = str(n); r['raw'].update(Identifier=f'SYN{n:07d}', Part=part, Inspected=10 * n, Rejected=n, Certified=9 * n)
        if part == 'ABSENT':
            del r['raw']['Part']
        out.append(r)
    return out


def built(parts):
    from openpyxl import load_workbook
    c = capture(rows(parts)); reviewed, findings = sf.reviewed_rows(c, MAP, []); head = wb.header({}, c); contract = wb.contract()
    bind = sf.binding(c, MAP, [], head, contract['clean_sha256'], sf.digest(contract), wb.generator_hash())
    raw, verification = wb.build(reviewed, head, c, bind, {})
    return load_workbook(BytesIO(raw), data_only=True), verification, findings   # saved results, as a reader sees them


class BlankPartNumber(unittest.TestCase):
    def test_one_record_without_a_part_number_among_others(self):
        for missing in (None, '', '   ', 'ABSENT'):
            with self.subTest(source_value=missing):
                book, verification, findings = built(['00001234', missing, '00005678', '00001234'])
                shift = book['1st Shift']
                by_id = {r[4].value: r for r in shift.iter_rows(min_row=16) if isinstance(r[4].value, str) and r[4].value.startswith('SYN')}
                self.assertEqual([by_id[f'SYN{n:07d}'][3].value for n in (1, 3, 4)], ['00001234', '00005678', '00001234'])
                self.assertIn(by_id['SYN0000002'][3].value, (None,) if missing in (None, 'ABSENT') else (None, missing))   # never a made-up number
                self.assertEqual((by_id['SYN0000002'][6].value, by_id['SYN0000002'][9].value, by_id['SYN0000002'][10].value), (20, 2, 18))
                summary = book['Summary']
                lines = [(summary.cell(r, 2).value, summary.cell(r, 10).value, summary.cell(r, 12).value, summary.cell(r, 14).value) for r in range(29, 32)]
                if missing in (None, 'ABSENT'):
                    self.assertEqual(lines, [('00001234', 50, 45, 5), ('00005678', 30, 27, 3), (wb.UNKNOWN_PART, 20, 18, 2)])
                    self.assertEqual(verification['part_totals'][wb.UNKNOWN_PART], dict(inspected=20, rejected=2, certified=18))
                self.assertEqual((summary.cell(32 if missing in (None, 'ABSENT') else 29 + len({'00001234', '00005678', missing}), 10).value), 100)   # every record once
                self.assertTrue(any(f['code'] == 'missing_value' and f['record_id'] == '2' and f['field'] == 'part' for f in findings))
                src = book['_Source']; head = [c.value for c in src[1]]
                ledger = next(dict(zip(head, [c.value for c in r])) for r in src.iter_rows(min_row=2) if r[0].value == '2')
                self.assertEqual(ledger['part_original_type'], {None: 'null', 'ABSENT': 'absent'}.get(missing, 'text'))
                text = ' '.join(str(c.value) for ws in book for r in ws.iter_rows() for c in r if c.value is not None)
                self.assertEqual(text.count('00001234') >= 2, True)

    def test_a_day_where_no_record_has_a_part_number(self):
        book, verification, _ = built([None, None])
        self.assertEqual(book['Summary']['B29'].value, wb.UNKNOWN_PART)
        self.assertEqual((book['Summary']['J29'].value, book['Summary']['J30'].value), (30, 30))
        self.assertEqual(list(verification['part_totals']), [wb.UNKNOWN_PART])

    def test_a_part_number_that_exists_is_still_saved_as_text_and_days_without_blanks_are_unchanged(self):
        book, verification, _ = built(['00001234', '00005678'])
        self.assertEqual([book['1st Shift'].cell(r, 4).value for r in (16, 17)], ['00001234', '00005678'])     # leading zeros kept
        self.assertEqual([book['1st Shift'].cell(r, 4).data_type for r in (16, 17)], ['s', 's'])
        self.assertEqual(sorted(verification['part_totals']), ['00001234', '00005678'])
        self.assertNotIn(wb.UNKNOWN_PART, ' '.join(str(c.value) for ws in book for r in ws.iter_rows() for c in r if c.value is not None))


if __name__ == '__main__':
    unittest.main()

"""Saved formula results are brought up to date after a fix (so previews and the checks see the new totals)."""
import io, os, re, sys, unittest, zipfile
sys.path.insert(0, os.path.dirname(__file__))
from eos_fixture import build
from hosting.recalc import _shift, _Book, _Eval, recalc
from hosting.fixes import apply_fixes


class RecalcTests(unittest.TestCase):
    def ev(self, formula, cells=None, formulas=None):
        return _Eval(_Book(cells or {}, formulas or {}), 'S').run(formula)

    def test_evaluator(self):
        cells = {('S', 7, 16): 34.0, ('S', 10, 16): 8.0, ('S', 7, 17): 30.0, ('T', 3, 5): 'x'}
        self.assertEqual(self.ev('G16-J16', cells), 26)
        self.assertEqual(self.ev('SUM(G16:G17)', cells), 64)
        self.assertEqual(self.ev('IF($B$8="","",$B$8)', cells), '')
        self.assertEqual(self.ev("COUNTA('T'!C5:C9)", cells), 1)
        self.assertEqual(self.ev('IFERROR(G16/0,0)', cells), 0)
        self.assertEqual(self.ev('ROUND(G16/3,1)', cells), 11.3)
        self.assertEqual(self.ev('COUNTIF(G16:G17,">31")', cells), 1)

    def test_shared_formula_shift(self):
        self.assertEqual(_shift('G16-J16', 3, 0), 'G19-J19')
        self.assertEqual(_shift('SUM($G$16:G16)*"G1"', 2, 1), 'SUM($G$16:H18)*"G1"')

    def test_fix_updates_totals(self):
        raw = build()
        fixed = apply_fixes('r.xlsx', raw, [dict(sheet='1st Shift', cell='G16', after='40', kind='number')])
        x = zipfile.ZipFile(io.BytesIO(fixed)).read('xl/worksheets/sheet2.xml').decode()
        k16 = re.search(r'<c r="K16"[^>]*>.*?<v>(.*?)</v>', x, re.S).group(1)
        self.assertEqual(k16, '32')                                  # 40 inspected - 8 rejected, saved like Excel would
        self.assertEqual(recalc(b'not a zip'), b'not a zip')        # anything odd: file left as it was


if __name__ == '__main__':
    unittest.main()

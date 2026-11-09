"""Synthetic value-type regression for direct manual kind:auto captures."""
import io,unittest
from openpyxl import Workbook,load_workbook
from hosting.fixes import apply_fixes
class DirectManualTypes(unittest.TestCase):
    def test_auto_preserves_numeric_text_and_distinguishes_blank_zero(self):
        for before,after,expected in [('00123','00124','00124'),('', '0','0'),(0,'',None),(12,'13',13)]:
            with self.subTest(before=before,after=after):
                wb=Workbook();wb.active.title='Synthetic';wb.active['A1']=before;out=io.BytesIO();wb.save(out);raw=out.getvalue()
                corrected=apply_fixes('synthetic.xlsx',raw,[dict(sheet='Synthetic',cell='A1',before=str(before),after=after,kind='auto',source='Your edit')])
                self.assertEqual(load_workbook(io.BytesIO(corrected)).active['A1'].value,expected)
                self.assertEqual(load_workbook(io.BytesIO(raw)).active['A1'].value,None if before=='' else before)

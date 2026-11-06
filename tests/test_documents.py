"""PDF and Word reports: their tables become a workbook that gets the normal checks."""
import io, os, sys, tempfile, unittest
from datetime import date

sys.path.insert(0, os.path.dirname(__file__))
from eos_fixture import ROWS, HEADERS
from hosting import documents
from hosting.spreadsheet_inbox import SpreadsheetInbox
from hosting.review_summary import summarize


def rows(n=15, typo_at=3):
    out = []
    for i, (ident, part, ins, rej, defect, comment) in enumerate((ROWS * 4)[:n]):
        out.append(['10/09/2026', '1', 'Line 2', '8O139189' if i == typo_at else part, ident, 'Holes / Glue', str(ins), 'NOK', defect, str(rej), str(ins - rej), comment])
    return out


def make_docx(table=True):
    import docx
    d = docx.Document(); d.add_paragraph('GRAMMER-2250 end of shift. Line 2 ran short one hour.')
    if table:
        t = d.add_table(rows=1, cols=len(HEADERS))
        for c, h in zip(t.rows[0].cells, HEADERS): c.text = h
        for r in rows():
            for c, v in zip(t.add_row().cells, r): c.text = v
    b = io.BytesIO(); d.save(b); return b.getvalue()


def make_pdf(n=40):
    from reportlab.lib.pagesizes import landscape, letter
    from reportlab.platypus import SimpleDocTemplate, Table, TableStyle
    from reportlab.lib import colors
    b = io.BytesIO(); t = Table([HEADERS] + rows(n), repeatRows=1)
    t.setStyle(TableStyle([('GRID', (0, 0), (-1, -1), 0.5, colors.grey), ('FONTSIZE', (0, 0), (-1, -1), 7)]))
    SimpleDocTemplate(b, pagesize=landscape(letter)).build([t]); return b.getvalue()


class Documents(unittest.TestCase):
    def setUp(self):
        self.inbox = SpreadsheetInbox(tempfile.mkdtemp())

    def test_word_table_is_checked(self):
        r = self.inbox.ingest('GRAMMER-2250 - EOS - 2026-10-09.docx', make_docx())
        self.assertEqual(r['filename'], 'GRAMMER-2250 - EOS - 2026-10-09 (from Word).xlsx')
        self.assertEqual((r['source_document']['type'], r['source_document']['tables'], r['source_document']['rows']), ('Word', 1, 15))
        self.assertIn('ran short', r['source_document']['text'])
        s = summarize(r)
        self.assertEqual((s['job'], s['inspected']), ('GRAMMER-2250', sum(x[2] for x in (ROWS * 2)[:15])))
        self.assertTrue(any('8O139189' in i['text'] for i in s['issues']))
        self.assertFalse(any('stored as text' in i['text'].lower() for i in s['issues']))   # no noise from the conversion

    def test_pdf_across_pages_is_one_table_and_same_file_is_recognised(self):
        raw = make_pdf()
        r = self.inbox.ingest('GRAMMER-2250 - EOS - 2026-10-09.pdf', raw)
        d = r['source_document']
        self.assertEqual((d['type'], d['tables'], d['rows']), ('PDF', 1, 40))
        self.assertGreaterEqual(d['pages'], 2)
        self.assertTrue(self.inbox.ingest('GRAMMER-2250 - EOS - 2026-10-09.pdf', raw).get('idempotent'))

    def test_numbers_and_dates_become_real_values(self):
        self.assertEqual(documents._value('34'), 34)
        self.assertEqual(documents._value('1,250.5'), 1250.5)
        self.assertEqual(documents._value('0123'), '0123')
        self.assertEqual(documents._value('10/09/2026'), date(2026, 10, 9))

    def test_text_only_and_bad_files(self):
        r = self.inbox.ingest('memo.docx', make_docx(table=False))
        self.assertEqual((r['source_document']['tables'], [s['name'] for s in r['sheets']]), (0, ['Text']))
        with self.assertRaises(ValueError):
            self.inbox.ingest('broken.pdf', b'not a pdf')
        with self.assertRaises(ValueError):
            self.inbox.ingest('broken.docx', b'not a docx')


if __name__ == '__main__':
    unittest.main()

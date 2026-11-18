"""Real synthetic conversion through local HTTP upload; no external sources or model calls."""
import hashlib
import io
from unittest.mock import patch
import test_chunked_upload as upload_tests
from hosting.spreadsheet_inbox import SpreadsheetInbox


def document(kind):
    rows=[['Identifier','Part Number','Qty Inspected','Qty OK','Qty NOK'],['SYN-1','PART-X','12','10','2']]
    out=io.BytesIO()
    if kind=='docx':
        import docx
        d=docx.Document();d.add_paragraph('Synthetic demonstration only')
        table=d.add_table(rows=0,cols=5)
        for row in rows:
            for cell,value in zip(table.add_row().cells,row):cell.text=value
        d.save(out)
    else:
        from reportlab.platypus import SimpleDocTemplate,Table,TableStyle
        from reportlab.lib import colors
        table=Table(rows);table.setStyle(TableStyle([('GRID',(0,0),(-1,-1),1,colors.black)]))
        SimpleDocTemplate(out).build([table])
    return out.getvalue()


class DocumentReceipts(upload_tests.ChunkedUploadHTTPTests):
    def test_actual_pdf_and_docx_conversion_receipts(self):
        for index,kind in enumerate(('pdf','docx')):
            with self.subTest(kind=kind):
                raw=document(kind);digest=hashlib.sha256(raw).hexdigest()
                status,response=self.upload('synthetic.'+kind,raw,chunk=len(raw),upload_id=str(index+1)*32)
                self.assertEqual(status,200,response)
                receipt=response['upload_receipt'];item=response['item']
                self.assertEqual(receipt['protocol'],'ppg-upload-v1')
                self.assertEqual(receipt['input_sha256'],digest)
                self.assertEqual(receipt['stored_file_hash'],item['file_hash'])
                self.assertEqual(receipt['file_hash'],item['file_hash'])
                self.assertNotEqual(item['file_hash'],digest)
                with self.server.workspace_store.session() as root:
                    saved=SpreadsheetInbox(root).get(item['file_hash'])
                    self.assertNotIn('upload_receipt',saved)
                # Reuploading exact document bytes returns same saved converted item and fresh truthful transport binding.
                status,again=self.upload('synthetic.'+kind,raw,chunk=len(raw),upload_id=str(index+3)*32)
                self.assertEqual(status,200,again)
                self.assertEqual(again['upload_receipt'],receipt)
                self.assertTrue(again['item']['idempotent'])
                status,bad=self.upload('synthetic.'+kind,raw,chunk=len(raw),upload_id=str(index+5)*32,sha256='0'*64)
                self.assertEqual(status,409,bad)
                self.assertNotIn('upload_receipt',bad)

    def test_verified_input_bound_to_server_formatted_child_without_mutating_receipt_into_storage(self):
        raw=b'Identifier,Qty Inspected,Qty OK,Qty NOK\nSYN-1,12,10,2\n'
        child_bytes=b'Identifier,Qty Inspected,Qty OK,Qty NOK\nSYN-1,12,10,2\nSYN-2,1,1,0\n'
        def formatted(handler,original_hash):
            with handler.store().session() as root:
                child=SpreadsheetInbox(root).ingest('synthetic-child.csv',child_bytes)
                return child
        with patch.dict('os.environ',{'PPG_AUTOFORMAT_ON_UPLOAD':'1'}),patch('hosting.autoformat.run',side_effect=formatted):
            status,response=self.upload('synthetic.csv',raw,chunk=len(raw))
        self.assertEqual(status,200,response)
        self.assertEqual(response['upload_receipt']['input_sha256'],hashlib.sha256(raw).hexdigest())
        self.assertEqual(response['upload_receipt']['stored_file_hash'],hashlib.sha256(raw).hexdigest())
        self.assertEqual(response['upload_receipt']['file_hash'],hashlib.sha256(child_bytes).hexdigest())
        self.assertEqual(response['item']['file_hash'],hashlib.sha256(child_bytes).hexdigest())

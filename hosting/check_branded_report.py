"""Local read-only client workbook check against optional separate source evidence."""
import argparse
import json
import os
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from hosting.spreadsheets import inspect_spreadsheet
from hosting.reconcile_spreadsheet import reconcile_result

def check(workbook,source=None):
    path=Path(workbook);result=inspect_spreadsheet(path.name,path.read_bytes())
    if source is not None:result=reconcile_result(result,Path(source).read_bytes())
    return result

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('workbook');parser.add_argument('--source');parser.add_argument('--output',required=True);args=parser.parse_args()
    if os.environ.get('VERCEL')=='1':raise SystemExit('Use the protected application ingestion route in a hosted environment.')
    result=check(args.workbook,args.source);Path(args.output).write_text(json.dumps(result,indent=2,allow_nan=False)+'\n')
    print(json.dumps({k:result.get(k) for k in ('file_sha256','source_sha256','record_count','numeric_cells_checked','numeric_values_compared','source_reconciliation_status','finding_count','release_status')}))

"""Import an explicit private manifest index into an isolated local review workspace.

No discovery, fetching, credential use, source edits, or message sending occurs.
Run: python3 hosting/import_source_envelopes.py INDEX.json --workspace DIRECTORY
The workspace can then be viewed with hosting/local_server.py --data DIRECTORY.
"""
import argparse
import json
import os
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from hosting.local_server import LocalWorkspace
from hosting.report_workflow import ReportWorkflow,WorkflowError,parse
from hosting.source_adapter import convert_private_envelope


def import_index(index_path,workspace):
    index_path=Path(index_path).resolve();index=parse(index_path.read_bytes())
    entries=index.get('manifests')
    if not isinstance(entries,list) or not 1<=len(entries)<=100:raise WorkflowError('Index requires 1 to 100 explicit manifests.')
    manifests=[]
    for entry in entries:
        name=entry.get('filename') if isinstance(entry,dict) else None
        if not isinstance(name,str) or Path(name).name!=name or '\\' in name:raise WorkflowError('Index filenames must not include folders.')
        path=(index_path.parent/name).resolve()
        if path.parent!=index_path.parent:raise WorkflowError('Index file is outside the selected directory.')
        envelope=parse(path.read_bytes())
        if entry.get('manifest_sha256')!=envelope.get('manifest_sha256'):raise WorkflowError('Index and envelope hash disagree.')
        manifests.append(convert_private_envelope(envelope))
    with LocalWorkspace(workspace).session() as directory:
        return ReportWorkflow(directory).ingest_batch(manifests)

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('index');parser.add_argument('--workspace',required=True);args=parser.parse_args()
    if os.environ.get('VERCEL')=='1':raise SystemExit('This local-only importer is not a hosted ingestion connector.')
    try:print(json.dumps(import_index(args.index,args.workspace),indent=2))
    except (OSError,WorkflowError) as exc:raise SystemExit(str(exc))

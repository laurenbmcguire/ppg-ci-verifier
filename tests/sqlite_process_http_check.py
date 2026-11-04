"""Independent synthetic acceptance: two real launcher processes, one SQLite store."""
import base64
import concurrent.futures
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import tempfile
import urllib.request

ROOT = Path(__file__).resolve().parents[1]


def main():
    environment = os.environ.copy()
    for key in ('VERCEL','VERCEL_ENV','AWS_LAMBDA_FUNCTION_NAME','AWS_EXECUTION_ENV','K_SERVICE','FUNCTIONS_WORKER_RUNTIME','NETLIFY','NETLIFY_DEV','FUNCTION_TARGET'):
        environment.pop(key, None)
    environment['DATABASE_URL'] = 'postgresql://never-use.invalid/original'
    processes = []
    def start(directory):
        process = subprocess.Popen([sys.executable, 'hosting/sqlite_server.py', '--data', str(directory), '--port', '0'], cwd=ROOT, env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        processes.append(process)
        line = process.stdout.readline()
        match = re.search(r'http://127\.0\.0\.1:\d+', line)
        if not match:
            raise AssertionError('Launcher did not return a loopback URL: ' + line)
        return match.group()
    def stop_all():
        for process in processes:
            process.terminate()
            process.communicate(timeout=10)
        processes.clear()
    def request(base, path, payload=None):
        data = None if payload is None else json.dumps(payload).encode()
        request = urllib.request.Request(base + path, data=data, headers={'Content-Type':'application/json','Origin':base,'X-Review-Token':'same-origin'})
        with urllib.request.urlopen(request, timeout=20) as response:
            assert response.status == 200
            return json.loads(response.read())
    with tempfile.TemporaryDirectory(prefix='sqlite-http-acceptance-') as directory:
        try:
            urls = [start(directory), start(directory)]
            def upload(index):
                raw = f'label,inspected,accepted,rejected\nsynthetic-{index},10,8,2\n'.encode()
                result = request(urls[index % 2], '/api/spreadsheets/import', {'filename':f'synthetic-{index}.csv','content_base64':base64.b64encode(raw).decode()})['item']
                assert result['file_hash'] == hashlib.sha256(raw).hexdigest()
                return result['file_hash']
            with concurrent.futures.ThreadPoolExecutor(max_workers=6) as executor:
                expected_hashes = set(executor.map(upload, range(24)))
            before = request(urls[0], '/api/spreadsheets')
            assert before['count'] == 24
            assert {row['file_hash'] for row in before['spreadsheets']} == expected_hashes
            stop_all()
            restarted = start(directory)
            after = request(restarted, '/api/spreadsheets')
            assert before == after
            health = request(restarted, '/api/health')
            assert health['storage'] == 'local-sqlite'
            assert health['local_only'] and not health['send_enabled'] and not health['reviewer_identity_available']
            with sqlite3.connect(Path(directory) / 'workspace.sqlite3') as db:
                integrity = db.execute('PRAGMA integrity_check').fetchone()[0]
                revision = db.execute('SELECT revision FROM workspace').fetchone()[0]
            assert integrity == 'ok'
            result = {'status':'passed','synthetic_only':True,'real_launcher_processes':2,'parallel_http_clients':6,'committed_distinct_uploads':24,'uploads_after_full_process_restart':24,'sqlite_integrity':integrity,'workspace_revision':revision,'database_url_used':False,'send_enabled':False,'reviewer_identity_available':False}
            print(json.dumps(result, indent=2))
        finally:
            stop_all()


if __name__ == '__main__':
    main()

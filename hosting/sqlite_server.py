"""Explicit loopback-only launcher for the isolated LOCAL SQLITE COPY."""
import argparse
from http.server import ThreadingHTTPServer
from pathlib import Path
import os
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from api.index import handler
from hosting.sqlite_storage import SQLiteWorkspace, local_environment


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=8766)
    parser.add_argument('--data', default=str(Path.home() / '.ci-verifier-sqlite-copy'))
    parser.add_argument('--demo', action='store_true', help='Enable clearly labeled synthetic review simulations; never real approval')
    args = parser.parse_args()
    if not local_environment():
        parser.error('Local SQLite cannot run on Vercel or serverless hosts.')
    # Durable default is a persistent home directory, never a scratch directory.
    store = SQLiteWorkspace(args.data)
    with store.session():
        pass
    os.environ['CI_LOCAL_DEV'] = '1'
    if args.demo:
        os.environ['CI_DEVELOPMENT_DEMO'] = '1'
    server = ThreadingHTTPServer(('127.0.0.1', args.port), handler)
    server.workspace_store = store
    print(f'LOCAL SQLITE COPY / PROTOTYPE: http://127.0.0.1:{server.server_port}', flush=True)
    print(f'Durable SQLite database: {store.database}', flush=True)
    print('Synthetic data only. No authenticated approval identity. Not an online deployment.', flush=True)
    if args.demo:
        print('Development simulations enabled: approve/deny/hold practice only; customer release remains held.', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == '__main__':
    main()

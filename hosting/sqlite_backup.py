"""Create a consistent SQLite backup, including committed WAL changes."""
import argparse
from contextlib import closing
from pathlib import Path
import sqlite3
import sys
import os
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from hosting.sqlite_storage import local_environment


def backup(source, destination):
    if not local_environment():
        raise ValueError('SQLite copy tools are local-only.')
    source, destination = Path(source).resolve(), Path(destination).absolute()
    if not source.is_file():
        raise ValueError('Source SQLite database does not exist.')
    if source == destination.resolve():
        raise ValueError('Backup must be a different file.')
    fd = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(fd)
    try:
        with closing(sqlite3.connect(source.as_uri() + '?mode=ro', uri=True)) as original:
            with closing(sqlite3.connect(destination)) as target:
                original.backup(target)
                if target.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                    raise ValueError('Backup integrity check failed.')
    except BaseException:
        destination.unlink(missing_ok=True)
        raise
    return destination


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('destination', help='New backup filename; never overwrites an existing file')
    parser.add_argument('--data', default=str(Path.home() / '.ci-verifier-sqlite-copy'))
    args = parser.parse_args()
    print(backup(Path(args.data) / 'workspace.sqlite3', args.destination))

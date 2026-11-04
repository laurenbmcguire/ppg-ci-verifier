"""Bounded deterministic snapshot serialization for the local SQLite prototype."""
import io
from pathlib import Path, PurePosixPath
import re
import zipfile

MAX_SNAPSHOT_BYTES = 16 * 1024 * 1024
MAX_FILES = 2000
NAMESPACE = 'synthetic-verifier-v1'
class StorageUnavailable(Exception):
    pass


def safe_name(name):
    p = PurePosixPath(name)
    if p.is_absolute() or any(x in ('', '.', '..') for x in name.split('/')) or '\\' in name:
        raise StorageUnavailable('Invalid stored workspace path.')
    if name in ('review.sqlite3', 'report-intake/report_inbox.sqlite3', 'spreadsheet_inbox.sqlite3', 'report_workflow.sqlite3'):
        return True
    if len(p.parts) == 2 and p.parts[0] in ('original', 'working', 'approved', 'simulated_reports', 'uploads', 'upload_parts'):
        if re.fullmatch(r'[A-Za-z0-9_.-]+', p.parts[1]):
            return True
    raise StorageUnavailable('Unexpected stored workspace file.')


def pack(root):
    output = io.BytesIO()
    files = sorted(p for p in Path(root).rglob('*') if p.is_file())
    if len(files) > MAX_FILES:
        raise StorageUnavailable('Demo storage limit reached; export and archive before continuing.')
    size = 0
    with zipfile.ZipFile(output, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        for path in files:
            name = path.relative_to(root).as_posix()
            safe_name(name)
            if path.is_symlink():
                raise StorageUnavailable('Workspace symlinks are not permitted.')
            data = path.read_bytes()
            size += len(data)
            if size > MAX_SNAPSHOT_BYTES:
                raise StorageUnavailable('Demo storage limit reached; export and archive before continuing.')
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, data)
    result = output.getvalue()
    if len(result) > MAX_SNAPSHOT_BYTES:
        raise StorageUnavailable('Demo storage limit reached.')
    return result


def restore(root, payload):
    if not payload:
        return
    payload = bytes(payload)
    if len(payload) > MAX_SNAPSHOT_BYTES:
        raise StorageUnavailable('Stored workspace exceeds the demo size limit.')
    try:
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            entries = archive.infolist()
            if len(entries) > MAX_FILES or sum(e.file_size for e in entries) > MAX_SNAPSHOT_BYTES:
                raise StorageUnavailable('Stored workspace exceeds the demo size limit.')
            names = set()
            for item in entries:
                safe_name(item.filename)
                if item.filename in names or item.is_dir():
                    raise StorageUnavailable('Invalid stored workspace archive.')
                names.add(item.filename)
                destination = Path(root) / item.filename
                destination.parent.mkdir(parents=True, exist_ok=True)
                data = archive.read(item)
                if len(data) != item.file_size:
                    raise StorageUnavailable('Stored workspace integrity check failed.')
                destination.write_bytes(data)
                destination.chmod(0o600 if destination.suffix == '.sqlite3' else 0o400)
    except (zipfile.BadZipFile, OSError, RuntimeError, ValueError) as exc:
        raise StorageUnavailable('Stored workspace could not be restored.') from exc



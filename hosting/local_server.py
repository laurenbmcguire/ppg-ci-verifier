"""Compatibility launcher: this isolated copy always uses local SQLite."""
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from hosting.sqlite_server import main
from hosting.sqlite_storage import SQLiteWorkspace
LocalWorkspace = SQLiteWorkspace
if __name__ == '__main__':
    main()

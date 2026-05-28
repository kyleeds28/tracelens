import sqlite3
from pathlib import Path
from contextlib import contextmanager

_DB_PATH: Path | None = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS project (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    xlsx_filename TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS program_row (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id TEXT NOT NULL,
    row_idx INTEGER NOT NULL,
    program_id TEXT,
    program_name TEXT,
    kind TEXT,
    kind_norm TEXT,           -- 'front' | 'api' | null
    menu_url TEXT,
    package TEXT,
    module_name TEXT,
    description TEXT,
    dev_type TEXT,
    category_l1 TEXT,
    category_l2 TEXT,
    FOREIGN KEY(project_id) REFERENCES project(id)
);

CREATE INDEX IF NOT EXISTS idx_program_row_proj ON program_row(project_id);
CREATE INDEX IF NOT EXISTS idx_program_row_pid ON program_row(project_id, program_id);

CREATE TABLE IF NOT EXISTS source_bundle (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id TEXT NOT NULL,
    archive_name TEXT NOT NULL,
    extract_dir TEXT NOT NULL,
    file_count INTEGER DEFAULT 0,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS source_file (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id TEXT NOT NULL,
    bundle_id INTEGER,
    abs_path TEXT NOT NULL,
    rel_path TEXT NOT NULL,
    ext TEXT,
    lang TEXT,                -- 'java' | 'vue' | 'ts' | 'tsx' | 'js' | 'jsx' | 'python' | ...
    package TEXT,             -- detected java package for .java files
    fqcn TEXT,                -- package + simple_name (without extension)
    simple_name TEXT          -- file stem
);

CREATE INDEX IF NOT EXISTS idx_source_file_proj ON source_file(project_id);
CREATE INDEX IF NOT EXISTS idx_source_file_fqcn ON source_file(project_id, fqcn);
CREATE INDEX IF NOT EXISTS idx_source_file_name ON source_file(project_id, simple_name);

CREATE TABLE IF NOT EXISTS mapping (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id TEXT NOT NULL,
    program_row_id INTEGER NOT NULL,
    source_file_id INTEGER,
    status TEXT NOT NULL,     -- 'O' | 'X' | 'PARTIAL'
    match_strategy TEXT,
    ast_json TEXT,            -- cached analyze result (json)
    FOREIGN KEY(program_row_id) REFERENCES program_row(id),
    FOREIGN KEY(source_file_id) REFERENCES source_file(id)
);

CREATE INDEX IF NOT EXISTS idx_mapping_proj ON mapping(project_id);
CREATE INDEX IF NOT EXISTS idx_mapping_row ON mapping(program_row_id);
"""


def init_db(db_path: Path) -> None:
    global _DB_PATH
    _DB_PATH = Path(db_path)
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with get_conn() as conn:
        conn.executescript(SCHEMA)
        # additive migration: manual selections survive re-match
        cols = [r[1] for r in conn.execute("PRAGMA table_info(mapping)").fetchall()]
        if "manual_override" not in cols:
            conn.execute("ALTER TABLE mapping ADD COLUMN manual_override INTEGER DEFAULT 0")


@contextmanager
def get_conn():
    assert _DB_PATH is not None, "init_db() must be called first"
    conn = sqlite3.connect(_DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def reset_project(project_id: str) -> None:
    with get_conn() as c:
        c.execute("DELETE FROM mapping WHERE project_id=?", (project_id,))
        c.execute("DELETE FROM source_file WHERE project_id=?", (project_id,))
        c.execute("DELETE FROM source_bundle WHERE project_id=?", (project_id,))
        c.execute("DELETE FROM program_row WHERE project_id=?", (project_id,))
        c.execute("DELETE FROM project WHERE id=?", (project_id,))

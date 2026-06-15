"""Run the JavaParser+SymbolSolver analyzer (java_analyzer) and load results.

The analyzer is a separate Java tool under app/java_analyzer/. This module
shells out to it, reads its JSON output, and exposes simple query helpers.

Build the analyzer once before first use:
    cd app/java_analyzer && python build.py
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Iterable

from db import get_conn

BACKEND_DIR = Path(__file__).resolve().parent
ANALYZER_DIR = BACKEND_DIR.parent / "java_analyzer"
JAR_PATH = ANALYZER_DIR / "build" / "java-analyzer.jar"
LIB_DIR = ANALYZER_DIR / "lib"
MAIN_CLASS = "com.sourcemapping.JavaSemanticAnalyzer"


class AnalyzerNotBuiltError(RuntimeError):
    pass


def _java_executable() -> str:
    java_home = os.environ.get("JAVA_HOME")
    if java_home:
        exe = Path(java_home) / "bin" / ("java.exe" if os.name == "nt" else "java")
        if exe.exists():
            return str(exe)
    # The jar is compiled to Java 17 (build.py --release 17). The first `java` on
    # PATH may be an old JRE (e.g. 8) that cannot load it → UnsupportedClassVersionError.
    # Prefer the `java` sitting next to `javac` — a real JDK that matches the build toolchain.
    javac = shutil.which("javac")
    if javac:
        exe = Path(javac).with_name("java.exe" if os.name == "nt" else "java")
        if exe.exists():
            return str(exe)
    return "java"


def _classpath() -> str:
    if not JAR_PATH.exists():
        raise AnalyzerNotBuiltError(
            f"java analyzer not built: {JAR_PATH} missing. "
            f"Run: cd {ANALYZER_DIR} && python build.py"
        )
    sep = ";" if os.name == "nt" else ":"
    parts = [str(JAR_PATH)] + [str(j) for j in sorted(LIB_DIR.glob("*.jar"))]
    return sep.join(parts)


def run_analyzer(source_root: Path, output_json: Path, timeout: int = 600) -> dict:
    """Invoke the Java analyzer on `source_root`, write JSON to `output_json`, return parsed dict."""
    output_json.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        _java_executable(), "-cp", _classpath(), MAIN_CLASS,
        str(source_root), str(output_json),
    ]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError(
            f"java analyzer failed (exit={r.returncode})\n"
            f"stdout: {r.stdout.strip()}\nstderr: {r.stderr.strip()}"
        )
    with output_json.open(encoding="utf-8") as f:
        return json.load(f)


# ---------------- persistence ----------------

SEMANTIC_SCHEMA = """
CREATE TABLE IF NOT EXISTS java_class_semantic (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id TEXT NOT NULL,
    fqcn TEXT NOT NULL,
    simple_name TEXT,
    is_interface INTEGER,
    layer TEXT,
    file TEXT,
    start_line INTEGER,
    end_line INTEGER,
    annotations_json TEXT,
    extends_json TEXT,
    implements_json TEXT,
    method_count INTEGER
);
CREATE INDEX IF NOT EXISTS idx_jcs_proj ON java_class_semantic(project_id);
CREATE INDEX IF NOT EXISTS idx_jcs_fqcn ON java_class_semantic(project_id, fqcn);

CREATE TABLE IF NOT EXISTS java_method_semantic (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id TEXT NOT NULL,
    fqsig TEXT NOT NULL,
    name TEXT,
    class_fqcn TEXT,
    file TEXT,
    layer TEXT,
    return_type TEXT,
    start_line INTEGER,
    end_line INTEGER,
    sloc INTEGER,
    statement_count INTEGER,
    is_abstract INTEGER,
    is_default INTEGER,
    is_static INTEGER,
    has_endpoint_annotation INTEGER,
    throws_not_impl INTEGER,
    body_shape TEXT,
    param_usage_rate REAL,
    annotations_json TEXT,
    parameters_json TEXT,
    calls_json TEXT,
    delegation_target TEXT,
    delegation_target_sloc INTEGER,
    delegation_target_layer TEXT,
    fan_in_count INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_jms_proj ON java_method_semantic(project_id);
CREATE INDEX IF NOT EXISTS idx_jms_fqsig ON java_method_semantic(project_id, fqsig);
CREATE INDEX IF NOT EXISTS idx_jms_class ON java_method_semantic(project_id, class_fqcn);

CREATE TABLE IF NOT EXISTS java_interface_impl (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id TEXT NOT NULL,
    interface_fqcn TEXT NOT NULL,
    impl_fqcn TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_jii_proj ON java_interface_impl(project_id);
CREATE INDEX IF NOT EXISTS idx_jii_iface ON java_interface_impl(project_id, interface_fqcn);

CREATE TABLE IF NOT EXISTS java_semantic_run (
    project_id TEXT PRIMARY KEY,
    source_root TEXT,
    parsed_files INTEGER,
    parse_errors INTEGER,
    class_count INTEGER,
    method_count INTEGER,
    resolved_calls INTEGER,
    unresolved_calls INTEGER,
    duration_ms INTEGER,
    output_path TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
"""


def ensure_schema() -> None:
    with get_conn() as c:
        c.executescript(SEMANTIC_SCHEMA)


def _reset_project(project_id: str) -> None:
    with get_conn() as c:
        for t in ("java_class_semantic", "java_method_semantic",
                  "java_interface_impl", "java_semantic_run"):
            c.execute(f"DELETE FROM {t} WHERE project_id=?", (project_id,))


def load_into_db(project_id: str, output_json: Path) -> dict:
    """Read analyzer JSON and populate the per-project semantic tables."""
    with output_json.open(encoding="utf-8") as f:
        data = json.load(f)
    ensure_schema()
    _reset_project(project_id)

    fan_in: dict[str, list[str]] = data.get("fan_in", {})

    with get_conn() as c:
        for cls in data.get("classes", []):
            c.execute(
                """INSERT INTO java_class_semantic
                (project_id, fqcn, simple_name, is_interface, layer, file,
                 start_line, end_line, annotations_json, extends_json,
                 implements_json, method_count)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    project_id, cls["fqcn"], cls.get("simple_name"),
                    1 if cls.get("is_interface") else 0, cls.get("layer"),
                    cls.get("file"), cls.get("start_line"), cls.get("end_line"),
                    json.dumps(cls.get("annotations", []), ensure_ascii=False),
                    json.dumps(cls.get("extends", []), ensure_ascii=False),
                    json.dumps(cls.get("implements", []), ensure_ascii=False),
                    cls.get("method_count", 0),
                ),
            )
        for m in data.get("methods", []):
            fqsig = m["fqsig"]
            c.execute(
                """INSERT INTO java_method_semantic
                (project_id, fqsig, name, class_fqcn, file, layer, return_type,
                 start_line, end_line, sloc, statement_count,
                 is_abstract, is_default, is_static,
                 has_endpoint_annotation, throws_not_impl,
                 body_shape, param_usage_rate,
                 annotations_json, parameters_json, calls_json,
                 delegation_target, delegation_target_sloc, delegation_target_layer,
                 fan_in_count)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    project_id, fqsig, m.get("name"), m.get("class_fqcn"),
                    m.get("file"),
                    m.get("layer"), m.get("return_type"),
                    m.get("start_line"), m.get("end_line"),
                    m.get("sloc"), m.get("statement_count"),
                    1 if m.get("is_abstract") else 0,
                    1 if m.get("is_default") else 0,
                    1 if m.get("is_static") else 0,
                    1 if m.get("has_endpoint_annotation") else 0,
                    1 if m.get("throws_not_impl") else 0,
                    m.get("body_shape"), m.get("param_usage_rate"),
                    json.dumps(m.get("annotations", []), ensure_ascii=False),
                    json.dumps(m.get("parameters", []), ensure_ascii=False),
                    json.dumps(m.get("calls", []), ensure_ascii=False),
                    m.get("delegation_target"),
                    m.get("delegation_target_sloc", -1),
                    m.get("delegation_target_layer"),
                    len(fan_in.get(fqsig, [])),
                ),
            )
        for iface, impls in data.get("interface_impls", {}).items():
            for impl in impls:
                c.execute(
                    "INSERT INTO java_interface_impl (project_id, interface_fqcn, impl_fqcn) VALUES (?,?,?)",
                    (project_id, iface, impl),
                )
        scan = data.get("scan", {})
        c.execute(
            """INSERT INTO java_semantic_run
            (project_id, source_root, parsed_files, parse_errors,
             class_count, method_count, resolved_calls, unresolved_calls,
             duration_ms, output_path)
            VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (
                project_id, scan.get("source_root"),
                scan.get("parsed_files"), scan.get("parse_errors"),
                scan.get("classes"), scan.get("methods"),
                scan.get("resolved_calls"), scan.get("unresolved_calls"),
                scan.get("duration_ms"), str(output_json),
            ),
        )

    return {
        "classes": len(data.get("classes", [])),
        "methods": len(data.get("methods", [])),
        "interface_impls": sum(len(v) for v in data.get("interface_impls", {}).values()),
        "scan": data.get("scan", {}),
    }


# ---------------- query helpers ----------------

def get_run_summary(project_id: str) -> dict | None:
    ensure_schema()
    with get_conn() as c:
        row = c.execute(
            "SELECT * FROM java_semantic_run WHERE project_id=?", (project_id,)
        ).fetchone()
    return dict(row) if row else None


def list_methods(project_id: str, *, layer: str | None = None,
                 body_shape: str | None = None, limit: int = 200) -> list[dict]:
    ensure_schema()
    q = "SELECT * FROM java_method_semantic WHERE project_id=?"
    args: list = [project_id]
    if layer:
        q += " AND layer=?"
        args.append(layer)
    if body_shape:
        q += " AND body_shape=?"
        args.append(body_shape)
    q += " ORDER BY class_fqcn, start_line LIMIT ?"
    args.append(limit)
    with get_conn() as c:
        rows = c.execute(q, args).fetchall()
    return [_inflate_method(dict(r)) for r in rows]


def get_method(project_id: str, fqsig: str) -> dict | None:
    ensure_schema()
    with get_conn() as c:
        row = c.execute(
            "SELECT * FROM java_method_semantic WHERE project_id=? AND fqsig=?",
            (project_id, fqsig),
        ).fetchone()
    return _inflate_method(dict(row)) if row else None


def methods_by_class(project_id: str, class_fqcn: str) -> list[dict]:
    """Return all methods whose enclosing class FQCN matches. Each method
    carries its own `file` (and `class_file` alias for compatibility) so
    callers can disambiguate the case where the same FQCN exists in multiple
    uploaded archives (e.g. portal_backend vs portal_backend_admin)."""
    ensure_schema()
    with get_conn() as c:
        rows = c.execute(
            """SELECT m.*, m.file AS class_file
               FROM java_method_semantic m
               WHERE m.project_id=? AND m.class_fqcn=?
               ORDER BY m.file, m.start_line""",
            (project_id, class_fqcn),
        ).fetchall()
    return [_inflate_method(dict(r)) for r in rows]


def get_class(project_id: str, fqcn: str) -> dict | None:
    ensure_schema()
    with get_conn() as c:
        row = c.execute(
            "SELECT * FROM java_class_semantic WHERE project_id=? AND fqcn=?",
            (project_id, fqcn),
        ).fetchone()
    if not row:
        return None
    d = dict(row)
    for k in ("annotations_json", "extends_json", "implements_json"):
        if d.get(k):
            try:
                d[k.removesuffix("_json")] = json.loads(d[k])
            except Exception:
                d[k.removesuffix("_json")] = []
        d.pop(k, None)
    return d


def trace_chain(project_id: str, fqsig: str, max_depth: int = 6) -> list[dict]:
    """Follow delegation_target hops starting at `fqsig`. Returns list of nodes."""
    ensure_schema()
    chain: list[dict] = []
    visited: set[str] = set()
    cur: str | None = fqsig
    depth = 0
    while cur and cur not in visited and depth <= max_depth:
        visited.add(cur)
        m = get_method(project_id, cur)
        if not m:
            chain.append({"depth": depth, "fqsig": cur, "indexed": False})
            break
        chain.append({
            "depth": depth,
            "fqsig": m["fqsig"],
            "name": m["name"],
            "class_fqcn": m["class_fqcn"],
            "layer": m["layer"],
            "body_shape": m["body_shape"],
            "sloc": m["sloc"],
            "delegation_target": m["delegation_target"],
            "delegation_target_layer": m["delegation_target_layer"],
            "fan_in_count": m["fan_in_count"],
            "indexed": True,
        })
        cur = m["delegation_target"]
        depth += 1
    return chain


def get_interface_impls(project_id: str, interface_fqcn: str | None = None) -> dict[str, list[str]]:
    ensure_schema()
    q = "SELECT interface_fqcn, impl_fqcn FROM java_interface_impl WHERE project_id=?"
    args: list = [project_id]
    if interface_fqcn:
        q += " AND interface_fqcn=?"
        args.append(interface_fqcn)
    with get_conn() as c:
        rows = c.execute(q, args).fetchall()
    out: dict[str, list[str]] = {}
    for r in rows:
        out.setdefault(r["interface_fqcn"], []).append(r["impl_fqcn"])
    return out


def _inflate_method(d: dict) -> dict:
    for k in ("annotations_json", "parameters_json", "calls_json"):
        if d.get(k):
            try:
                d[k.removesuffix("_json")] = json.loads(d[k])
            except Exception:
                d[k.removesuffix("_json")] = []
        else:
            d[k.removesuffix("_json")] = []
        d.pop(k, None)
    return d

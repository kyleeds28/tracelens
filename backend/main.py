import hashlib
import json
import shutil
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles

from ast_analyzer import analyze_file
from db import get_conn, init_db, reset_project
from matcher import match_program_row, status_from_strategy, auto_match_by_locality
from sources import extract_archive, walk_files
from spec import group_by_program, load_mapping, load_spec
import java_semantic
import spec_issues
import unknown_triage
import llm_analysis
import llm_summary

APP_ROOT = Path(__file__).parent
DATA_DIR = APP_ROOT.parent / "data"
UPLOAD_DIR = DATA_DIR / "uploads"
SOURCES_DIR = DATA_DIR / "sources"
DB_PATH = DATA_DIR / "index.db"
DEFAULT_MAPPING = APP_ROOT / "mapping.yaml"

UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
SOURCES_DIR.mkdir(parents=True, exist_ok=True)
init_db(DB_PATH)

app = FastAPI(title="Source Mapping Tool")


# ---------------- helpers ----------------

def _get_project(project_id: str) -> dict:
    with get_conn() as c:
        row = c.execute("SELECT * FROM project WHERE id=?", (project_id,)).fetchone()
        if not row:
            raise HTTPException(404, f"project not found: {project_id}")
        return dict(row)


def _load_cfg(project_id: str) -> dict:
    # per-project override allowed
    override = UPLOAD_DIR / project_id / "mapping.yaml"
    return load_mapping(override if override.exists() else DEFAULT_MAPPING)


def _save_upload(upload: UploadFile, dest: Path) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    with dest.open("wb") as f:
        shutil.copyfileobj(upload.file, f)
    return dest


def _stable_project_id(name: str) -> str:
    # 같은 프로젝트명을 다시 import 하면 동일 id 가 나오도록 결정론적으로 생성.
    # (랜덤 uuid 대신 — 중복 import 로 project 가 여러 개 생기는 것을 방지)
    key = " ".join(name.strip().lower().split())
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:8]


# ---------------- routes ----------------

@app.post("/api/projects")
async def create_project(
    name: str = Form(...),
    spec_file: UploadFile = File(...),
    mapping_yaml: UploadFile | None = File(None),
    overwrite: bool = Form(False),
):
    project_id = _stable_project_id(name)
    with get_conn() as c:
        existing = c.execute(
            "SELECT id, name FROM project WHERE id=?", (project_id,)
        ).fetchone()
    if existing and not overwrite:
        raise HTTPException(
            409,
            f"이미 동일한 이름의 프로젝트가 있습니다: '{existing['name']}'. "
            "갱신하려면 덮어쓰기를 확인하세요.",
        )

    proj_dir = UPLOAD_DIR / project_id
    if existing:
        # 갱신(upsert): 파생 데이터만 비우고 적용된 AI 판정(llm_method_analysis)은 보존
        reset_project(project_id)
        shutil.rmtree(proj_dir, ignore_errors=True)

    xlsx_path = _save_upload(spec_file, proj_dir / spec_file.filename)
    if mapping_yaml is not None:
        _save_upload(mapping_yaml, proj_dir / "mapping.yaml")

    cfg_path = proj_dir / "mapping.yaml" if (proj_dir / "mapping.yaml").exists() else DEFAULT_MAPPING
    try:
        rows = load_spec(xlsx_path, cfg_path)
    except Exception as e:
        shutil.rmtree(proj_dir, ignore_errors=True)
        raise HTTPException(400, f"failed to parse xlsx: {e}")

    with get_conn() as c:
        c.execute(
            "INSERT INTO project (id, name, xlsx_filename) VALUES (?, ?, ?)",
            (project_id, name, spec_file.filename),
        )
        for r in rows:
            c.execute(
                """INSERT INTO program_row
                (project_id, row_idx, program_id, program_name, kind, kind_norm,
                 menu_url, package, module_name, description, dev_type,
                 category_l1, category_l2)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    project_id,
                    r["row_idx"],
                    r.get("program_id"),
                    r.get("program_name"),
                    r.get("kind"),
                    r.get("kind_norm"),
                    r.get("menu_url"),
                    r.get("package"),
                    r.get("module_name"),
                    r.get("description"),
                    r.get("dev_type"),
                    r.get("category_l1"),
                    r.get("category_l2"),
                ),
            )
    return {
        "project_id": project_id,
        "name": name,
        "reused": bool(existing),
        "row_count": len(rows),
        "program_count": len({r.get("program_id") for r in rows if r.get("program_id")}),
    }


@app.get("/api/projects")
def list_projects():
    with get_conn() as c:
        rows = c.execute(
            """SELECT p.*,
                      (SELECT COUNT(*) FROM program_row WHERE project_id=p.id) AS row_count,
                      (SELECT COUNT(DISTINCT program_id) FROM program_row WHERE project_id=p.id) AS program_count,
                      (SELECT COUNT(*) FROM source_file WHERE project_id=p.id) AS file_count
               FROM project p ORDER BY p.created_at DESC"""
        ).fetchall()
    return [dict(r) for r in rows]


@app.delete("/api/projects/{project_id}")
def delete_project(project_id: str):
    _get_project(project_id)
    reset_project(project_id)
    shutil.rmtree(UPLOAD_DIR / project_id, ignore_errors=True)
    shutil.rmtree(SOURCES_DIR / project_id, ignore_errors=True)
    return {"ok": True}


@app.get("/api/projects/{project_id}/programs")
def list_programs(project_id: str):
    _get_project(project_id)
    with get_conn() as c:
        rows = c.execute(
            """SELECT pr.*,
                      m.id AS mapping_id,
                      m.status, m.match_strategy, m.source_file_id,
                      m.manual_override, m.code_sloc_sum, m.method_cnt,
                      m.file_code_lines,
                      sf.rel_path, sf.lang, sf.fqcn, sf.abs_path, sf.ext
               FROM program_row pr
               LEFT JOIN mapping m ON m.program_row_id = pr.id
               LEFT JOIN source_file sf ON sf.id = m.source_file_id
               WHERE pr.project_id=?
               ORDER BY pr.row_idx""",
            (project_id,),
        ).fetchall()
        flat = [dict(r) for r in rows]

        # Module-level source size = sum of per-method sloc badges (code lines,
        # blank/comment excluded) from analyze_file — the SAME metric shown in
        # the AST panel. Computed once per file and cached on the mapping row.
        updates: list[tuple] = []
        for r in flat:
            if r.get("code_sloc_sum") is not None and r.get("file_code_lines") is not None:
                r["sloc_total"] = r["code_sloc_sum"]
                r["method_count"] = r["method_cnt"]
                r["code_lines_total"] = r["file_code_lines"]
                continue
            r["sloc_total"] = None
            r["method_count"] = None
            r["code_lines_total"] = None
            if not r.get("source_file_id") or not r.get("abs_path"):
                continue
            try:
                ast = analyze_file(r["abs_path"], r["ext"] or "")
                fns = ast.get("functions") or []
            except Exception:
                continue
            if not fns and ast.get("error"):
                continue
            sloc_sum = sum(int(f.get("sloc") or 0) for f in fns)
            cnt = len(fns)
            code_lines = int((ast.get("file_metrics") or {}).get("code_lines") or 0)
            r["sloc_total"] = sloc_sum
            r["method_count"] = cnt
            r["code_lines_total"] = code_lines
            updates.append((sloc_sum, cnt, code_lines, r["mapping_id"]))

        for sloc_sum, cnt, code_lines, mid in updates:
            c.execute(
                "UPDATE mapping SET code_sloc_sum=?, method_cnt=?, file_code_lines=? WHERE id=?",
                (sloc_sum, cnt, code_lines, mid),
            )

    # group by program_id
    groups: dict[str, dict] = {}
    order: list[str] = []
    for r in flat:
        pid = r.get("program_id") or f"_anon_{r['row_idx']}"
        if pid not in groups:
            groups[pid] = {
                "program_id": pid,
                "program_name": r.get("program_name"),
                "category_l1": r.get("category_l1"),
                "category_l2": r.get("category_l2"),
                "menu_url": r.get("menu_url"),
                "rows": [],
                "found": 0, "partial": 0, "missing": 0, "unknown": 0,
            }
            order.append(pid)
        g = groups[pid]
        if not g["program_name"] and r.get("program_name"):
            g["program_name"] = r["program_name"]
        if not g["menu_url"] and r.get("menu_url"):
            g["menu_url"] = r["menu_url"]
        g["rows"].append(r)
        s = r.get("status")
        if s == "O":
            g["found"] += 1
        elif s == "PARTIAL":
            g["partial"] += 1
        elif s == "X":
            g["missing"] += 1
        else:
            g["unknown"] += 1
    return [groups[pid] for pid in order]


@app.post("/api/projects/{project_id}/sources")
async def upload_source(project_id: str, file: UploadFile = File(...)):
    _get_project(project_id)
    proj_src_dir = SOURCES_DIR / project_id
    archive_path = _save_upload(file, proj_src_dir / "_archives" / file.filename)
    try:
        extracted = extract_archive(archive_path, proj_src_dir)
    except Exception as e:
        raise HTTPException(400, f"extract failed: {e}")

    files = walk_files(extracted)
    with get_conn() as c:
        cur = c.execute(
            """INSERT INTO source_bundle (project_id, archive_name, extract_dir, file_count)
               VALUES (?,?,?,?)""",
            (project_id, file.filename, str(extracted), len(files)),
        )
        bundle_id = cur.lastrowid
        for f in files:
            c.execute(
                """INSERT INTO source_file
                (project_id, bundle_id, abs_path, rel_path, ext, lang, package, fqcn, simple_name)
                VALUES (?,?,?,?,?,?,?,?,?)""",
                (
                    project_id, bundle_id,
                    f["abs_path"], f["rel_path"], f["ext"], f["lang"],
                    f.get("package"), f.get("fqcn"), f.get("simple_name"),
                ),
            )

    # auto-run mapping after each upload
    run_mapping(project_id)
    return {"bundle_id": bundle_id, "file_count": len(files), "extracted_to": str(extracted)}


@app.post("/api/projects/{project_id}/match")
def run_mapping(project_id: str):
    _get_project(project_id)
    cfg = _load_cfg(project_id)
    with get_conn() as c:
        files = [dict(r) for r in c.execute(
            "SELECT * FROM source_file WHERE project_id=?", (project_id,)
        ).fetchall()]
        rows = [dict(r) for r in c.execute(
            "SELECT * FROM program_row WHERE project_id=?", (project_id,)
        ).fetchall()]

        # Snapshot any human-confirmed mappings so re-match preserves the
        # user's choice instead of clobbering it.
        #   manual_override = 1  → user picked a specific candidate file
        #   manual_override = 2  → user confirmed "truly missing in code"
        manual = {
            r["program_row_id"]: dict(r) for r in c.execute(
                "SELECT * FROM mapping WHERE project_id=? AND manual_override IN (1, 2)",
                (project_id,),
            ).fetchall()
        }

        c.execute("DELETE FROM mapping WHERE project_id=?", (project_id,))
        stats = {"O": 0, "PARTIAL": 0, "X": 0}
        for r in rows:
            saved = manual.get(r["id"])
            if saved:
                status = saved.get("status") or "O"
                stats[status] = stats.get(status, 0) + 1
                c.execute(
                    """INSERT INTO mapping
                       (project_id, program_row_id, source_file_id, status,
                        match_strategy, manual_override)
                       VALUES (?,?,?,?,?,?)""",
                    (project_id, r["id"], saved.get("source_file_id"),
                     status, saved.get("match_strategy") or "manual",
                     saved.get("manual_override") or 1),
                )
                continue
            matched, strategy = match_program_row(r, files, cfg)
            status = status_from_strategy(strategy) if matched else "X"
            stats[status] = stats.get(status, 0) + 1
            c.execute(
                """INSERT INTO mapping
                   (project_id, program_row_id, source_file_id, status, match_strategy)
                   VALUES (?,?,?,?,?)""",
                (
                    project_id, r["id"],
                    matched["id"] if matched else None,
                    status, strategy,
                ),
            )

        # ---- Tier A: 구조 신호 기반 자동 확정 (형제 패키지 수렴 + 역할 유일) ----
        # 사람 판단 없이 '엄격 유일' 인 X(api) 행만 O 로 승격. 나머지는 그대로 두어
        # 기존 유사후보 제안 흐름을 유지한다. 결정론적이라 재매칭마다 재계산됨.
        mapping_by_row = {
            m["program_row_id"]: dict(m) for m in c.execute(
                """SELECT program_row_id, source_file_id, status, manual_override
                   FROM mapping WHERE project_id=?""",
                (project_id,),
            ).fetchall()
        }
        auto = auto_match_by_locality(rows, mapping_by_row, files)
        for a in auto:
            c.execute(
                """UPDATE mapping SET source_file_id=?, status='O', match_strategy=?
                   WHERE project_id=? AND program_row_id=?""",
                (a["source_file_id"], a["strategy"], project_id, a["row_id"]),
            )
            stats["X"] = max(0, stats.get("X", 0) - 1)
            stats["O"] = stats.get("O", 0) + 1
    return {
        "stats": stats, "matched_files": len(files),
        "manual_preserved": len(manual), "auto_locality": len(auto),
    }


@app.post("/api/projects/{project_id}/mapping/{row_id}/select")
def select_mapping_candidate(project_id: str, row_id: int, body: dict):
    """Pin a program row's mapping to a user-chosen source file.

    Body: {"fqcn": "com.example.Foo"}  OR  {"source_file_id": 123}
    Sets status='O' / match_strategy='manual' / manual_override=1.
    Re-match preserves this choice."""
    _get_project(project_id)
    fqcn = body.get("fqcn")
    source_file_id = body.get("source_file_id")

    with get_conn() as c:
        if source_file_id is None and fqcn:
            r = c.execute(
                "SELECT id FROM source_file WHERE project_id=? AND fqcn=? LIMIT 1",
                (project_id, fqcn),
            ).fetchone()
            if not r:
                simple = fqcn.split(".")[-1]
                r = c.execute(
                    "SELECT id FROM source_file WHERE project_id=? AND simple_name=? LIMIT 1",
                    (project_id, simple),
                ).fetchone()
            if not r:
                raise HTTPException(404, f"source file not found: {fqcn}")
            source_file_id = r["id"]
        if source_file_id is None:
            raise HTTPException(400, "fqcn or source_file_id required")

        prow = c.execute(
            "SELECT id FROM program_row WHERE id=? AND project_id=?",
            (row_id, project_id),
        ).fetchone()
        if not prow:
            raise HTTPException(404, f"program_row not found: {row_id}")

        existing = c.execute(
            "SELECT id FROM mapping WHERE project_id=? AND program_row_id=?",
            (project_id, row_id),
        ).fetchone()
        if existing:
            c.execute(
                """UPDATE mapping
                   SET source_file_id=?, status='O',
                       match_strategy='manual', manual_override=1,
                       ast_json=NULL
                   WHERE id=?""",
                (source_file_id, existing["id"]),
            )
        else:
            c.execute(
                """INSERT INTO mapping
                   (project_id, program_row_id, source_file_id, status,
                    match_strategy, manual_override)
                   VALUES (?,?,?,?,?,1)""",
                (project_id, row_id, source_file_id, "O", "manual"),
            )

        info = c.execute(
            "SELECT rel_path, fqcn FROM source_file WHERE id=?",
            (source_file_id,),
        ).fetchone()

    return {
        "ok": True, "row_id": row_id,
        "source_file_id": source_file_id,
        "rel_path": info["rel_path"] if info else None,
        "fqcn": info["fqcn"] if info else None,
        "status": "O", "match_strategy": "manual",
    }


@app.delete("/api/projects/{project_id}/mapping/{row_id}/select")
def unselect_mapping(project_id: str, row_id: int):
    """검수자의 수동 판단(선택 또는 불일치 확정)을 취소하고,
    해당 한 행만 자동 매칭으로 즉시 재평가 — 판정 자체를 "다시 열린 상태"로 복원.

    이렇게 해야 사용자가 취소 후 다시 후보 선택 화면을 그대로 받을 수 있다.
    (이전엔 manual_override만 0으로 바뀌고 status/strategy가 'manual_missing' 그대로
    남아 있어 화면 라벨이 'X · 검수자가 불일치 확정' 으로 잘못 표시되었음)
    """
    _get_project(project_id)
    cfg = _load_cfg(project_id)
    with get_conn() as c:
        prow = c.execute(
            "SELECT * FROM program_row WHERE id=? AND project_id=?",
            (row_id, project_id),
        ).fetchone()
        if not prow:
            raise HTTPException(404, f"program_row not found: {row_id}")
        files = [dict(r) for r in c.execute(
            "SELECT * FROM source_file WHERE project_id=?", (project_id,)
        ).fetchall()]
        matched, strategy = match_program_row(dict(prow), files, cfg)
        status = status_from_strategy(strategy) if matched else "X"
        c.execute(
            """UPDATE mapping
               SET source_file_id=?, status=?, match_strategy=?,
                   manual_override=0, ast_json=NULL
               WHERE project_id=? AND program_row_id=?""",
            (matched["id"] if matched else None, status, strategy,
             project_id, row_id),
        )
    return {"ok": True, "row_id": row_id,
            "status": status, "match_strategy": strategy,
            "source_file_id": matched["id"] if matched else None}


@app.post("/api/projects/{project_id}/mapping/{row_id}/confirm-missing")
def confirm_missing(project_id: str, row_id: int):
    """검수자가 '이 모듈은 정말 코드에 없다'고 사람 눈으로 확정.

    Sets status='X', source_file_id=NULL,
    match_strategy='manual_missing', manual_override=2.
    Re-match preserves this confirmation (won't try to auto-match again).
    Undo via the same DELETE /select endpoint (sets manual_override=0)."""
    _get_project(project_id)
    with get_conn() as c:
        prow = c.execute(
            "SELECT id FROM program_row WHERE id=? AND project_id=?",
            (row_id, project_id),
        ).fetchone()
        if not prow:
            raise HTTPException(404, f"program_row not found: {row_id}")

        existing = c.execute(
            "SELECT id FROM mapping WHERE project_id=? AND program_row_id=?",
            (project_id, row_id),
        ).fetchone()
        if existing:
            c.execute(
                """UPDATE mapping
                   SET source_file_id=NULL, status='X',
                       match_strategy='manual_missing', manual_override=2,
                       ast_json=NULL
                   WHERE id=?""",
                (existing["id"],),
            )
        else:
            c.execute(
                """INSERT INTO mapping
                   (project_id, program_row_id, source_file_id, status,
                    match_strategy, manual_override)
                   VALUES (?,?,NULL,?,?,2)""",
                (project_id, row_id, "X", "manual_missing"),
            )
    return {"ok": True, "row_id": row_id,
            "status": "X", "match_strategy": "manual_missing",
            "manual_override": 2}


@app.get("/api/projects/{project_id}/rows/{row_id}/ast")
def get_row_ast(project_id: str, row_id: int):
    _get_project(project_id)
    with get_conn() as c:
        m = c.execute(
            """SELECT m.*, sf.abs_path, sf.ext, sf.lang
               FROM mapping m
               LEFT JOIN source_file sf ON sf.id = m.source_file_id
               WHERE m.project_id=? AND m.program_row_id=?""",
            (project_id, row_id),
        ).fetchone()
        if not m:
            raise HTTPException(404, "mapping not found")
        m = dict(m)
        if not m.get("source_file_id"):
            return {"status": m.get("status"), "ast": None, "reason": m.get("match_strategy")}
        if m.get("ast_json"):
            return {"status": m["status"], "ast": json.loads(m["ast_json"]), "cached": True}

        ast = analyze_file(m["abs_path"], m["ext"])
        fns = ast.get("functions") or []
        sloc_sum = sum(int(f.get("sloc") or 0) for f in fns)
        code_lines = int((ast.get("file_metrics") or {}).get("code_lines") or 0)
        c.execute(
            "UPDATE mapping SET ast_json=?, code_sloc_sum=?, method_cnt=?, file_code_lines=? WHERE id=?",
            (json.dumps(ast, ensure_ascii=False), sloc_sum, len(fns), code_lines, m["id"]),
        )
    return {"status": m["status"], "ast": ast, "cached": False}


@app.get("/api/projects/{project_id}/rows/{row_id}/source")
def get_row_source(project_id: str, row_id: int):
    _get_project(project_id)
    with get_conn() as c:
        m = c.execute(
            """SELECT m.*, sf.abs_path FROM mapping m
               LEFT JOIN source_file sf ON sf.id = m.source_file_id
               WHERE m.project_id=? AND m.program_row_id=?""",
            (project_id, row_id),
        ).fetchone()
        if not m or not m["abs_path"]:
            raise HTTPException(404, "source not available")
    try:
        text = Path(m["abs_path"]).read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        raise HTTPException(500, str(e))
    return {"path": m["abs_path"], "content": text}


@app.get("/api/projects/{project_id}/sources")
def list_source_bundles(project_id: str):
    _get_project(project_id)
    with get_conn() as c:
        rows = c.execute(
            "SELECT * FROM source_bundle WHERE project_id=? ORDER BY id",
            (project_id,),
        ).fetchall()
    return [dict(r) for r in rows]


@app.get("/api/health")
def health():
    return {"ok": True}


# ---------------- java semantic (JavaParser + SymbolSolver) ----------------

@app.post("/api/projects/{project_id}/semantic/run")
def run_java_semantic(project_id: str):
    _get_project(project_id)
    proj_src_dir = SOURCES_DIR / project_id

    # Guard 1: any source zip uploaded at all?
    with get_conn() as c:
        bundle_count = c.execute(
            "SELECT COUNT(*) FROM source_bundle WHERE project_id=?", (project_id,)
        ).fetchone()[0]
        java_file_count = c.execute(
            "SELECT COUNT(*) FROM source_file WHERE project_id=? AND ext='.java'",
            (project_id,),
        ).fetchone()[0]

    if bundle_count == 0 or not proj_src_dir.exists():
        raise HTTPException(
            400,
            "소스 코드가 업로드되지 않았습니다. 사이드바의 '소스 코드 업로드 (zip)' 에서 "
            "프로젝트 소스 zip 을 먼저 업로드해주세요.",
        )
    if java_file_count == 0:
        raise HTTPException(
            400,
            "업로드된 소스에 .java 파일이 없습니다. JavaParser+SymbolSolver 분석은 "
            "Java 프로젝트만 지원합니다.",
        )

    output_json = DATA_DIR / "semantic" / project_id / "result.json"
    try:
        java_semantic.run_analyzer(proj_src_dir, output_json)
    except java_semantic.AnalyzerNotBuiltError as e:
        raise HTTPException(
            503,
            "Java 분석기가 빌드되지 않았습니다. "
            f"`cd app/java_analyzer && python build.py` 를 먼저 실행해주세요. (원본: {e})",
        )
    except Exception as e:
        raise HTTPException(500, f"분석 중 오류: {e}")
    return java_semantic.load_into_db(project_id, output_json)


@app.get("/api/projects/{project_id}/semantic/summary")
def get_java_semantic_summary(project_id: str):
    _get_project(project_id)
    s = java_semantic.get_run_summary(project_id)
    if not s:
        raise HTTPException(404, "no semantic analysis run for this project")
    return s


@app.get("/api/projects/{project_id}/semantic/methods")
def list_java_semantic_methods(
    project_id: str,
    layer: str | None = None,
    body_shape: str | None = None,
    limit: int = 200,
):
    _get_project(project_id)
    return java_semantic.list_methods(project_id, layer=layer, body_shape=body_shape, limit=limit)


@app.get("/api/projects/{project_id}/semantic/interface-impls")
def get_java_interface_impls(project_id: str, interface_fqcn: str | None = None):
    _get_project(project_id)
    return java_semantic.get_interface_impls(project_id, interface_fqcn)


@app.get("/api/projects/{project_id}/semantic/by-fqcn")
def get_java_semantic_by_fqcn(project_id: str, fqcn: str):
    """All semantic info for one class FQCN (class meta + methods + interface impls)."""
    _get_project(project_id)
    cls = java_semantic.get_class(project_id, fqcn)
    if not cls:
        return {"fqcn": fqcn, "found": False, "class": None, "methods": [], "implementations": {}}
    methods = java_semantic.methods_by_class(project_id, fqcn)
    impls = java_semantic.get_interface_impls(project_id, fqcn) if cls.get("is_interface") else {}
    return {"fqcn": fqcn, "found": True, "class": cls, "methods": methods, "implementations": impls}


@app.get("/api/projects/{project_id}/semantic/chain")
def get_java_semantic_chain(project_id: str, fqsig: str, max_depth: int = 6):
    _get_project(project_id)
    return {"chain": java_semantic.trace_chain(project_id, fqsig, max_depth=max_depth)}


@app.get("/api/projects/{project_id}/semantic/unknown")
def get_unknown_triage(project_id: str):
    """UNKNOWN-layer classes tagged + grouped (suspect vs ignorable), with
    mapping status — for the '미분류 코드 점검' panel."""
    _get_project(project_id)
    return unknown_triage.detect(project_id)


# ---------------- LLM gray-zone analysis ----------------

from pydantic import BaseModel


class LlmAnalyzeRequest(BaseModel):
    fqsig: str
    force: bool = False


class LlmApplyRequest(BaseModel):
    fqsig: str
    applied: bool


@app.post("/api/projects/{project_id}/llm/analyze")
def llm_analyze(project_id: str, req: LlmAnalyzeRequest):
    _get_project(project_id)
    ctx = llm_analysis.build_context_from_db(project_id, req.fqsig)
    if ctx is None:
        raise HTTPException(404, f"semantic data not found for fqsig: {req.fqsig}")
    return llm_analysis.analyze_method(project_id, ctx, force=req.force)


@app.post("/api/projects/{project_id}/llm/apply")
def llm_apply(project_id: str, req: LlmApplyRequest):
    """Mark the most-recent LLM result as accepted (applied=true) or revoked (false).

    When applied=true the UI promotes the LLM verdict into the row's effective
    status (정상/의심/알 수 없음) and adds an AI marker."""
    _get_project(project_id)
    n = llm_analysis.set_applied(project_id, req.fqsig, req.applied)
    if n == 0:
        raise HTTPException(404, f"no cached LLM result for fqsig: {req.fqsig}")
    return {"ok": True, "fqsig": req.fqsig, "applied": req.applied, "updated": n}


@app.get("/api/projects/{project_id}/llm/by-fqsig")
def llm_by_fqsig(project_id: str, fqsig: str):
    _get_project(project_id)
    hit = llm_analysis.get_cached_by_fqsig(project_id, fqsig)
    return {"cached": bool(hit), "result": hit}


@app.delete("/api/projects/{project_id}/llm/by-fqsig")
def llm_delete_by_fqsig(project_id: str, fqsig: str):
    """Permanently wipe the cached LLM result for this method.
    Used by the UI 취소 button — refresh must NOT bring the result back."""
    _get_project(project_id)
    n = llm_analysis.delete_cached(project_id, fqsig)
    return {"ok": True, "deleted": n}


@app.get("/api/projects/{project_id}/llm/by-class")
def llm_by_class(project_id: str, fqcn: str):
    """Return all cached LLM results for methods of a class — used to hydrate the UI."""
    llm_analysis.ensure_schema()
    with get_conn() as c:
        rows = c.execute(
            """SELECT m.fqsig, l.verdict, l.confidence, l.reasoning,
                      l.suggested_target_intent, l.evidence_quote, l.concerns_json, l.duration_ms,
                      l.created_at, l.error, l.applied
               FROM java_method_semantic m
               LEFT JOIN llm_method_analysis l
                 ON l.project_id = m.project_id AND l.fqsig = m.fqsig
               WHERE m.project_id=? AND m.class_fqcn=?""",
            (project_id, fqcn),
        ).fetchall()
    out = {}
    for r in rows:
        r = dict(r)
        if r["verdict"] is None and r["error"] is None:
            continue
        try:
            concerns = json.loads(r["concerns_json"] or "[]")
        except Exception:
            concerns = []
        # cap confidence at the same ceiling used elsewhere
        conf = r["confidence"]
        if conf is not None:
            conf = min(llm_analysis.CONFIDENCE_CAP, float(conf))
        out[r["fqsig"]] = {
            "verdict": r["verdict"], "confidence": conf,
            "reasoning": r["reasoning"],
            "suggested_target_intent": r["suggested_target_intent"],
            "evidence_quote": r["evidence_quote"],
            "concerns": concerns, "duration_ms": r["duration_ms"],
            "created_at": r["created_at"], "error": r["error"],
            "applied": bool(r.get("applied")),
        }
    return out


# ---------------- spec freshness (산출문서 정합성) ----------------

@app.get("/api/projects/{project_id}/spec-issues")
def list_spec_issues(project_id: str, kind: str | None = None):
    _get_project(project_id)
    detected = spec_issues.detect(project_id)
    if kind:
        detected["issues"] = [i for i in detected["issues"] if i["kind"] == kind]
    return detected


@app.get("/api/projects/{project_id}/spec-issues/summary")
def spec_issues_summary(project_id: str):
    _get_project(project_id)
    d = spec_issues.detect(project_id)
    return {"total": d["total"], "by_kind": d["by_kind"]}


@app.post("/api/projects/{project_id}/summary/ai")
def project_or_program_summary_ai(project_id: str, program_id: str | None = None):
    """정적 수치 요약 위에 narrative + hotspot 생성 (프로그램/프로젝트 단위).

    - 정적 수치는 /summary 와 동일 (재계산 안 함, 동기화)
    - narrative 는 임원/PM 용 한국어 종합 의견
    - hotspot 은 검토 후보 (단정 아님)
    - 메서드 인용 시 ClassName.method() 형식만
    - 결과 캐시 안 함 — 매번 fresh inference"""
    proj = _get_project(project_id)
    summary = project_or_program_summary(project_id, program_id)
    out = llm_summary.summarize(
        project_id=project_id,
        summary=summary,
        project_name=proj.get("name", ""),
        program_id=program_id,
    )
    return out


# ---- per-method status classification (shared by summary + score endpoints) ----
# body_shape (semantic) → status group via same rules as the verdict labels.
_SHAPE_TO_STATUS = {
    "empty": "suspect", "stub_throw": "suspect", "stub_literal": "suspect",
    "stub_debug": "suspect",
    "no_body": "ok", "abstract": "ok",
    "delegation": "ok", "accessor": "ok",
    "single_throw": "unknown", "single_return": "unknown",
    "single_statement": "unknown", "empty_return": "unknown",
    "multi_statement": "ok",
}
_SHAPE_TO_STUB = {
    "empty": "STUB_EMPTY",
    "stub_throw": "STUB_NOT_IMPL",
    "stub_literal": "STUB_PLACEHOLDER",
    "stub_debug": "STUB_DEBUG_ONLY",
}
_LLM_TO_STATUS = {
    "REAL_DELEGATION": "ok", "REAL_LOGIC": "ok",
    "STUB": "suspect", "NAME_MISMATCH": "suspect",
    "MOVED_TO_SIBLING": "ok",     # dead-code 확인됨 — 정상 그룹
    "UNCLEAR": "unknown",
}


def _is_unconfirmed_ok(shape, calls_json, delegation_target, sloc):
    """정적 'ok' 이지만 호출을 하나도 resolve 못해 동작 확정 불가 → 보수적으로 unknown.

    프런트 isLLMCandidate 의 zone 3·4 와 동일 기준.
    """
    if shape in ("no_body", "abstract"):
        return False
    try:
        calls = json.loads(calls_json or "[]")
    except (ValueError, TypeError):
        calls = []
    resolved = sum(1 for x in calls if isinstance(x, dict) and x.get("resolved"))
    all_unresolved = len(calls) > 0 and resolved == 0
    if shape in ("delegation", "accessor"):
        if delegation_target:
            return False
        if all_unresolved:
            return True
    if all_unresolved and (sloc or 0) >= 3:
        return True
    return False


def aggregate_methods(methods: list, llm_by_fqsig: dict) -> dict:
    """메서드 목록 → 상태 집계(정적/effective/스텁/LLM/레이어).

    summary 엔드포인트와 program-scores 배치가 동일한 분류 규칙을 쓰도록
    per-method 판정을 한 곳에 모은다. methods 각 원소는 fqsig/layer/body_shape/
    sloc/calls_json/delegation_target 를 가진 dict-like.
    """
    method_total = len(methods)
    layer_count: dict[str, int] = {}
    static_status = {"ok": 0, "suspect": 0, "unknown": 0}
    effective_status = {"ok": 0, "suspect": 0, "unknown": 0}
    stubs = {"STUB_EMPTY": 0, "STUB_NOT_IMPL": 0,
             "STUB_PLACEHOLDER": 0, "STUB_DEBUG_ONLY": 0}
    llm_verdict_dist: dict[str, int] = {}
    llm_analyzed = 0
    llm_applied = 0
    llm_overrode_to_suspect = 0
    llm_overrode_to_ok = 0
    unconfirmed_unknown = 0

    for m in methods:
        m = dict(m)
        layer = m.get("layer") or "UNKNOWN"
        layer_count[layer] = layer_count.get(layer, 0) + 1

        shape = m.get("body_shape") or ""
        s_status = _SHAPE_TO_STATUS.get(shape, "unknown")
        if s_status == "ok" and _is_unconfirmed_ok(
            shape, m.get("calls_json"), m.get("delegation_target"), m.get("sloc")
        ):
            s_status = "unknown"
            unconfirmed_unknown += 1
        static_status[s_status] += 1

        llm = llm_by_fqsig.get(m["fqsig"])
        promoted_to_ok = (
            llm and llm.get("applied") and llm.get("verdict")
            and _LLM_TO_STATUS.get(llm["verdict"]) == "ok"
        )

        if shape in _SHAPE_TO_STUB and not promoted_to_ok:
            stubs[_SHAPE_TO_STUB[shape]] += 1

        if llm and llm.get("verdict"):
            llm_analyzed += 1
            llm_verdict_dist[llm["verdict"]] = llm_verdict_dist.get(llm["verdict"], 0) + 1
            if llm.get("applied"):
                llm_applied += 1
                e_status = _LLM_TO_STATUS.get(llm["verdict"], s_status)
                effective_status[e_status] += 1
                if s_status == "ok" and e_status == "suspect":
                    llm_overrode_to_suspect += 1
                elif s_status != "ok" and e_status == "ok":
                    llm_overrode_to_ok += 1
                continue
        effective_status[s_status] += 1

    return {
        "method_total": method_total,
        "layer_count": layer_count,
        "static_status": static_status,
        "effective_status": effective_status,
        "stubs": stubs,
        "llm_verdict_dist": llm_verdict_dist,
        "llm_analyzed": llm_analyzed,
        "llm_applied": llm_applied,
        "llm_overrode_to_suspect": llm_overrode_to_suspect,
        "llm_overrode_to_ok": llm_overrode_to_ok,
        "unconfirmed_unknown": unconfirmed_unknown,
    }


@app.get("/api/projects/{project_id}/summary")
def project_or_program_summary(project_id: str, program_id: str | None = None):
    """Aggregate counts at project (default) or program scope.

    Combines: program_row mapping status, java_method_semantic verdicts,
    LLM-applied overrides, layer distribution, stub kinds, spec freshness.
    """
    _get_project(project_id)
    java_semantic.ensure_schema()
    llm_analysis.ensure_schema()

    scope_fqcns: set[str] | None = None
    with get_conn() as c:
        # ---------- 1. mapping status counts ----------
        if program_id:
            map_rows = c.execute(
                """SELECT m.status FROM mapping m
                   JOIN program_row pr ON pr.id = m.program_row_id
                   WHERE pr.project_id=? AND pr.program_id=?""",
                (project_id, program_id),
            ).fetchall()
            row_total = c.execute(
                "SELECT COUNT(*) FROM program_row WHERE project_id=? AND program_id=?",
                (project_id, program_id),
            ).fetchone()[0]
            file_rows = c.execute(
                """SELECT DISTINCT sf.fqcn FROM source_file sf
                   JOIN mapping m ON m.source_file_id = sf.id
                   JOIN program_row pr ON pr.id = m.program_row_id
                   WHERE pr.project_id=? AND pr.program_id=? AND sf.fqcn IS NOT NULL""",
                (project_id, program_id),
            ).fetchall()
            scope_fqcns = {r["fqcn"] for r in file_rows}
        else:
            map_rows = c.execute(
                "SELECT status FROM mapping WHERE project_id=?", (project_id,)
            ).fetchall()
            row_total = c.execute(
                "SELECT COUNT(*) FROM program_row WHERE project_id=?", (project_id,)
            ).fetchone()[0]

        mapping_status = {"O": 0, "PARTIAL": 0, "X": 0}
        for r in map_rows:
            s = r["status"] or "X"
            mapping_status[s] = mapping_status.get(s, 0) + 1

        # ---------- 2. method-level aggregates (scope-aware) ----------
        if program_id:
            if scope_fqcns:
                placeholders = ",".join("?" * len(scope_fqcns))
                methods = c.execute(
                    f"""SELECT m.fqsig, m.layer, m.body_shape, m.throws_not_impl, m.sloc,
                               m.calls_json, m.delegation_target
                        FROM java_method_semantic m
                        WHERE m.project_id=? AND m.class_fqcn IN ({placeholders})""",
                    (project_id, *scope_fqcns),
                ).fetchall()
            else:
                methods = []
        else:
            methods = c.execute(
                """SELECT fqsig, layer, body_shape, throws_not_impl, sloc,
                          calls_json, delegation_target
                   FROM java_method_semantic WHERE project_id=?""",
                (project_id,),
            ).fetchall()

        # ---------- 3. LLM cached results for the same scope ----------
        if program_id and scope_fqcns:
            placeholders = ",".join("?" * len(scope_fqcns))
            llm_rows = c.execute(
                f"""SELECT l.fqsig, l.verdict, l.applied
                    FROM llm_method_analysis l
                    JOIN java_method_semantic m
                      ON m.project_id=l.project_id AND m.fqsig=l.fqsig
                    WHERE l.project_id=? AND m.class_fqcn IN ({placeholders})""",
                (project_id, *scope_fqcns),
            ).fetchall()
        elif program_id:
            llm_rows = []
        else:
            llm_rows = c.execute(
                "SELECT fqsig, verdict, applied FROM llm_method_analysis WHERE project_id=?",
                (project_id,),
            ).fetchall()

        files_scope = (
            len(scope_fqcns) if program_id and scope_fqcns is not None
            else c.execute("SELECT COUNT(DISTINCT fqcn) FROM source_file WHERE project_id=? AND ext='.java' AND fqcn IS NOT NULL", (project_id,)).fetchone()[0]
        )

    # ---------- compute verdict status (with LLM applied override) ----------
    # per-method 분류는 aggregate_methods 한 곳에서 (score 배치와 규칙 공유)
    llm_by_fqsig = {r["fqsig"]: dict(r) for r in llm_rows}
    agg = aggregate_methods(methods, llm_by_fqsig)
    method_total = agg["method_total"]
    layer_count = agg["layer_count"]
    static_status = agg["static_status"]
    effective_status = agg["effective_status"]
    stubs = agg["stubs"]
    llm_verdict_dist = agg["llm_verdict_dist"]
    llm_analyzed = agg["llm_analyzed"]
    llm_applied = agg["llm_applied"]
    llm_overrode_to_suspect = agg["llm_overrode_to_suspect"]
    llm_overrode_to_ok = agg["llm_overrode_to_ok"]
    unconfirmed_unknown = agg["unconfirmed_unknown"]

    # ---------- spec freshness (scope-aware) ----------
    issues = spec_issues.detect(project_id)
    if program_id:
        spec_issue_list = [i for i in issues["issues"] if i.get("program_id") == program_id]
    else:
        spec_issue_list = issues["issues"]
    from collections import Counter
    spec_issue_by_kind = dict(Counter(i["kind"] for i in spec_issue_list))
    spec_issue_by_kind["total"] = len(spec_issue_list)

    return {
        "scope": "program" if program_id else "project",
        "program_id": program_id,
        "totals": {
            "modules": row_total,
            "files": files_scope,
            "classes": len(set(m["class_fqcn"] for m in [dict(x) for x in methods] if m.get("class_fqcn"))) if False else None,
            "methods": method_total,
        },
        "mapping_status": mapping_status,
        "method_status_effective": effective_status,
        "method_status_static_only": static_status,
        "method_layer": layer_count,
        "stubs": stubs,
        "llm": {
            "analyzed": llm_analyzed,
            "applied": llm_applied,
            "overrode_to_suspect": llm_overrode_to_suspect,
            "overrode_to_ok": llm_overrode_to_ok,
            "verdict_dist": llm_verdict_dist,
        },
        "unconfirmed_unknown": unconfirmed_unknown,
        "spec_issues": spec_issue_by_kind,
    }


@app.get("/api/projects/{project_id}/program-scores")
def program_scores(project_id: str):
    """program별 결정론 점수 입력값(메서드 effective 집계)을 한 번에.

    coverage(O/P/X)는 경량 /programs 응답으로 프런트가 계산하고, 여기서는
    content 비율 산출에 필요한 메서드 ok/suspect/unknown 만 program_id 별로
    내려준다. 점수 공식 자체는 프런트(programScore)와 동일하게 한 곳에서 계산.
    """
    _get_project(project_id)
    java_semantic.ensure_schema()
    llm_analysis.ensure_schema()
    with get_conn() as c:
        fq_rows = c.execute(
            """SELECT DISTINCT pr.program_id, sf.fqcn
               FROM program_row pr
               JOIN mapping m ON m.program_row_id = pr.id
               JOIN source_file sf ON sf.id = m.source_file_id
               WHERE pr.project_id=? AND sf.fqcn IS NOT NULL""",
            (project_id,),
        ).fetchall()
        meth_rows = c.execute(
            """SELECT fqsig, layer, body_shape, sloc, calls_json,
                      delegation_target, class_fqcn
               FROM java_method_semantic WHERE project_id=?""",
            (project_id,),
        ).fetchall()
        llm_rows = c.execute(
            "SELECT fqsig, verdict, applied FROM llm_method_analysis WHERE project_id=?",
            (project_id,),
        ).fetchall()

    methods_by_fqcn: dict[str, list] = {}
    for m in meth_rows:
        methods_by_fqcn.setdefault(m["class_fqcn"], []).append(dict(m))
    llm_by_fqsig = {r["fqsig"]: dict(r) for r in llm_rows}

    prog_fqcns: dict[str, set] = {}
    for r in fq_rows:
        prog_fqcns.setdefault(r["program_id"], set()).add(r["fqcn"])

    out: dict[str, dict] = {}
    for pid, fqcns in prog_fqcns.items():
        methods: list = []
        for fq in fqcns:
            methods.extend(methods_by_fqcn.get(fq, []))
        es = aggregate_methods(methods, llm_by_fqsig)["effective_status"]
        out[pid] = {
            "ok": es["ok"], "suspect": es["suspect"], "unknown": es["unknown"],
        }
    return out


@app.get("/api/projects/{project_id}/spec-issues.csv")
def spec_issues_csv(project_id: str):
    _get_project(project_id)
    d = spec_issues.detect(project_id)
    csv_text = spec_issues.to_csv(d)
    fname = f"spec-issues-{project_id}.csv"
    return Response(
        content=csv_text.encode("utf-8"),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


# ---------------- static frontend ----------------

STATIC_DIR = APP_ROOT / "static"
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")


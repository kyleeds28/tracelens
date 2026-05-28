"""On-demand LLM analysis for the JavaParser+SymbolSolver gray zone.

Trigger: a method tree-sitter classified as a delegation-shape but whose call
target SymbolSolver couldn't resolve. We send a tight prompt to local Ollama
(qwen2.5-coder:7b) asking only the verdict + 1-line reasoning, and cache the
answer keyed by (project_id, fqsig, body_hash) so a re-run on unchanged code
returns instantly.
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
import urllib.request
from pathlib import Path

from db import get_conn

# Process-wide mutex for Ollama calls.
# Rationale: this box has 8GB VRAM and qwen2.5-coder:7b Q4 (~4.5GB) plus
# KV cache leaves no room for two simultaneous inferences. Without this lock,
# concurrent requests get parallelised by Ollama and the extra memory pressure
# spills layers to CPU → 5-10x slowdown. We enforce strict FIFO here so the
# behaviour matches our 8GB-VRAM operating rule regardless of how Ollama is
# configured.
_OLLAMA_LOCK = threading.Lock()
# Optional: surface queue depth so the UI can show "X명 대기 중" if it wants.
_OLLAMA_WAITING = 0
_OLLAMA_WAITING_LOCK = threading.Lock()


def ollama_queue_depth() -> int:
    """How many threads are currently blocked waiting for the Ollama lock."""
    with _OLLAMA_WAITING_LOCK:
        return _OLLAMA_WAITING

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
DEFAULT_MODEL = os.environ.get("LLM_MODEL", "qwen2.5-coder:7b")
TIMEOUT_SEC = int(os.environ.get("LLM_TIMEOUT_SEC", "60"))


VALID_VERDICTS = {"REAL_DELEGATION", "REAL_LOGIC", "STUB", "NAME_MISMATCH", "UNCLEAR"}

# Honest calibration: 7B local model often outputs 100%; cap at this ceiling
# so the UI doesn't suggest absolute certainty.
CONFIDENCE_CAP = 0.90


# ---------------- schema ----------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS llm_method_analysis (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id TEXT NOT NULL,
    fqsig TEXT NOT NULL,
    body_hash TEXT NOT NULL,
    model TEXT NOT NULL,
    verdict TEXT,
    confidence REAL,
    reasoning TEXT,
    suggested_target_intent TEXT,
    concerns_json TEXT,
    raw_response TEXT,
    error TEXT,
    duration_ms INTEGER,
    applied INTEGER DEFAULT 0,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(project_id, fqsig, body_hash, model)
);
CREATE INDEX IF NOT EXISTS idx_lma_proj_fqsig ON llm_method_analysis(project_id, fqsig);
"""


def ensure_schema() -> None:
    with get_conn() as c:
        c.executescript(SCHEMA)
        # additive migration for older builds (SQLite has no IF NOT EXISTS for columns)
        cols = [row[1] for row in c.execute("PRAGMA table_info(llm_method_analysis)").fetchall()]
        if "applied" not in cols:
            c.execute("ALTER TABLE llm_method_analysis ADD COLUMN applied INTEGER DEFAULT 0")


# ---------------- prompt ----------------

SYSTEM_PROMPT = (
    "당신은 자바 백엔드 코드 분석 전문가입니다. "
    "정적 분석이 명확히 분류하지 못한 메서드의 본문을 보고 의도를 분류합니다. "
    "응답은 반드시 JSON 한 덩어리만 출력하세요. 한국어 설명을 사용하세요. "
    "5가지 verdict 중 가장 적절한 하나를 선택하세요:\n"
    "- REAL_DELEGATION: 다른 메서드 호출로 일을 위임 (호출 대상이 실로직 수행)\n"
    "- REAL_LOGIC    : 본문에서 직접 검증·계산·비교·변환 등 의미 있는 로직 수행\n"
    "- STUB          : 사실상 미구현 (return null/false, throw NotImpl, TODO만 등)\n"
    "- NAME_MISMATCH : 메서드명·산출문서 약속과 본문이 불일치\n"
    "- UNCLEAR       : 판단 모호 (확신 없을 때만)"
)


def build_prompt(ctx: dict) -> str:
    """Build the user prompt from a merged tree-sitter + SymbolSolver context.

    Both static analyses are surfaced as separate sections so the LLM can see
    where they agree, where they disagree, and what each one couldn't decide.
    """
    annotations = ", ".join(ctx.get("annotations") or []) or "(없음)"

    # ---- tree-sitter section (raw structure metrics + Korean verdict) ----
    ts_lines = []
    if ctx.get("ts_verdict") is not None:
        ts_lines.append(f"- verdict        : {ctx['ts_verdict']} ({ctx.get('ts_label_kr','-')})")
        ts_lines.append(f"- status         : {ctx.get('ts_status','-')}")
        ts_lines.append(f"- body_class     : {ctx.get('ts_body_class','-')}")
        ts_lines.append(f"- SLOC           : {ctx.get('ts_sloc','-')}")
        ts_lines.append(f"- cyclomatic     : {ctx.get('ts_cc','-')}  ← (분기/단락평가 개수)")
        ts_lines.append(f"- statements     : {ctx.get('ts_stmts','-')}")
        ts_lines.append(f"- fan_out        : {ctx.get('ts_fan_out','-')}  ← (호출하는 메서드 이름 수)")
        ts_lines.append(f"- nesting_depth  : {ctx.get('ts_nesting','-')}")
        ts_lines.append(f"- parameter_count: {ctx.get('ts_parameter_count','-')}")
        if ctx.get("ts_fan_out_targets"):
            ts_lines.append(f"- called names   : {', '.join(ctx['ts_fan_out_targets'][:8])}")
    else:
        ts_lines.append("(tree-sitter 분석 결과 없음)")
    ts_block = "\n".join(ts_lines)

    # ---- semantic section (JavaParser+SymbolSolver — call graph) ----
    sem_lines = [
        f"- body_shape          : {ctx.get('sem_body_shape','-')}",
        f"- SLOC                : {ctx.get('sem_sloc','-')}",
        f"- statements          : {ctx.get('sem_statement_count','-')}",
        f"- fan_in (전체 프로젝트): {ctx.get('sem_fan_in','-')}  ← (이 메서드를 호출하는 코드 위치 개수)",
        f"- param_usage_rate    : {ctx.get('sem_param_usage_rate','-')}  ← (선언된 파라미터 중 본문에서 사용된 비율)",
        f"- throws_not_impl     : {ctx.get('sem_throws_not_impl', False)}",
    ]
    if ctx.get("sem_delegation_target"):
        sem_lines.append(
            f"- delegation_target   : {ctx['sem_delegation_target']}  "
            f"[{ctx.get('sem_delegation_target_layer','?')}] "
            f"sloc={ctx.get('sem_delegation_target_sloc','?')}"
        )

    calls_lines = []
    for c in (ctx.get("calls") or [])[:8]:
        if c.get("resolved"):
            calls_lines.append(
                f"  ✓ {c['name']}() → {c['target_fqsig']}  [{c.get('target_layer','?')}]"
            )
        else:
            calls_lines.append(f"  ✗ {c['name']}()  (호출 대상 미해석)")
    if calls_lines:
        sem_lines.append("- calls:")
        sem_lines.extend(calls_lines)
    sem_block = "\n".join(sem_lines)

    spec_block = ""
    if ctx.get("program_name") or ctx.get("module_description"):
        spec_block = (
            f"\n## 산출문서 명세\n"
            f"- program_name : {ctx.get('program_name') or '-'}\n"
            f"- module_desc  : {ctx.get('module_description') or '-'}\n"
        )

    return f"""## 분석 대상 메서드
class       : {ctx['class_fqcn']}
method      : {ctx['name']}
layer       : {ctx.get('layer','-')}
annotations : [{annotations}]
return_type : {ctx.get('return_type','-')}

## tree-sitter 정적 분석 (구문 구조 + 복잡도)
{ts_block}

## JavaParser + SymbolSolver 의미 분석 (호출 그래프)
{sem_block}
{spec_block}
## 메서드 본문 코드
```java
{ctx['source']}
```

## 판단 지침
- tree-sitter 가 분류 보류(`status=unknown`/`STRAIGHT_LINE`) 했거나 SymbolSolver 가
  호출 대상을 못 찾은 (`✗` 표시) 경우, **본문 코드를 직접 읽고** 의도를 판단하세요.
- 5가지 verdict 중 하나만 선택:
  - REAL_DELEGATION : 본문이 다른 메서드 호출 위주, 그 호출이 실로직을 수행할 것으로 추정
  - REAL_LOGIC     : 본문이 직접 검증·계산·비교·변환 등 의미 있는 로직 수행
                     (예: `return a != null && !(a instanceof X)` 처럼 인라인 검증)
  - STUB           : 사실상 미구현 (return null, throw NotImpl, TODO 만 등)
  - NAME_MISMATCH  : 메서드명·산출문서 약속과 본문이 불일치
  - UNCLEAR        : 판단 모호 (확신 없을 때만, confidence 낮춰서)
- reasoning 에는 본문에서 인용한 구체 근거를 1~2문장으로.
- 두 정적 분석이 서로 다른 신호를 줄 때 (예: tree-sitter 는 "보류" 인데 SymbolSolver 는
  fan_in=0) 그 점을 reasoning 에 짚어도 됩니다.

## 출력 형식 (이 JSON 만)
{{
  "verdict": "REAL_DELEGATION" | "REAL_LOGIC" | "STUB" | "NAME_MISMATCH" | "UNCLEAR",
  "confidence": 0.0~1.0,
  "reasoning": "한글 1~2문장",
  "suggested_target_intent": "호출 대상 또는 본문 로직의 추정 역할 (한글 한 줄)",
  "concerns": ["짧은 메모", "..."]
}}
"""


# ---------------- ollama client ----------------

def _call_ollama(model: str, system: str, user: str,
                 num_predict: int = 400, timeout: int | None = None) -> tuple[str, int]:
    """Returns (response_text, duration_ms). Raises on transport error.

    Serialised via a process-wide mutex — see _OLLAMA_LOCK rationale above.
    Callers can override num_predict (output token budget) and timeout (sec)
    for longer-form generations like project summaries.
    """
    global _OLLAMA_WAITING
    body = json.dumps({
        "model": model,
        "prompt": user,
        "system": system,
        "format": "json",
        "stream": False,
        "options": {"temperature": 0.1, "num_predict": num_predict},
    }).encode("utf-8")
    req = urllib.request.Request(
        f"{OLLAMA_URL}/api/generate",
        data=body,
        headers={"Content-Type": "application/json"},
    )
    eff_timeout = timeout if timeout is not None else TIMEOUT_SEC

    # bump queue depth before blocking on the lock so /llm/queue can report it
    with _OLLAMA_WAITING_LOCK:
        _OLLAMA_WAITING += 1
    decremented = False
    try:
        with _OLLAMA_LOCK:
            with _OLLAMA_WAITING_LOCK:
                _OLLAMA_WAITING -= 1
                decremented = True
            t0 = time.time()
            with urllib.request.urlopen(req, timeout=eff_timeout) as r:
                data = json.loads(r.read())
            return data.get("response", ""), int((time.time() - t0) * 1000)
    finally:
        if not decremented:
            with _OLLAMA_WAITING_LOCK:
                _OLLAMA_WAITING -= 1


def _parse_response(text: str) -> dict:
    """Strict-parse the LLM JSON and validate against schema. Raise on invalid."""
    obj = json.loads(text)
    verdict = obj.get("verdict")
    if verdict not in VALID_VERDICTS:
        raise ValueError(f"invalid verdict: {verdict!r}")
    conf = float(obj.get("confidence", 0))
    conf = max(0.0, min(CONFIDENCE_CAP, conf))
    return {
        "verdict": verdict,
        "confidence": conf,
        "reasoning": str(obj.get("reasoning", "")).strip(),
        "suggested_target_intent": str(obj.get("suggested_target_intent", "")).strip(),
        "concerns": [str(x).strip() for x in (obj.get("concerns") or [])][:5],
    }


# ---------------- core ----------------

def _body_hash(source: str) -> str:
    return hashlib.sha256(source.encode("utf-8")).hexdigest()[:16]


def _hydrate(row_dict: dict, model: str) -> dict:
    """Common shaping: cap confidence, decode concerns, mark cached."""
    r = dict(row_dict)
    if r.get("error"):
        return {
            "error": r["error"], "model": model, "cached": True,
            "applied": bool(r.get("applied")),
        }
    try:
        r["concerns"] = json.loads(r["concerns_json"] or "[]")
    except Exception:
        r["concerns"] = []
    r.pop("concerns_json", None)
    if r.get("confidence") is not None:
        r["confidence"] = min(CONFIDENCE_CAP, float(r["confidence"]))
    r["applied"] = bool(r.get("applied"))
    r["model"] = model
    r["cached"] = True
    return r


def get_cached(project_id: str, fqsig: str, body_hash: str,
               model: str = DEFAULT_MODEL) -> dict | None:
    ensure_schema()
    with get_conn() as c:
        row = c.execute(
            """SELECT verdict, confidence, reasoning, suggested_target_intent,
                      concerns_json, duration_ms, created_at, error, applied
               FROM llm_method_analysis
               WHERE project_id=? AND fqsig=? AND body_hash=? AND model=?""",
            (project_id, fqsig, body_hash, model),
        ).fetchone()
    return _hydrate(dict(row), model) if row else None


def get_cached_by_fqsig(project_id: str, fqsig: str,
                        model: str = DEFAULT_MODEL) -> dict | None:
    """Return latest cached entry for (project_id, fqsig) regardless of body_hash."""
    ensure_schema()
    with get_conn() as c:
        row = c.execute(
            """SELECT verdict, confidence, reasoning, suggested_target_intent,
                      concerns_json, duration_ms, created_at, error, body_hash, applied
               FROM llm_method_analysis
               WHERE project_id=? AND fqsig=? AND model=?
               ORDER BY id DESC LIMIT 1""",
            (project_id, fqsig, model),
        ).fetchone()
    return _hydrate(dict(row), model) if row else None


def delete_cached(project_id: str, fqsig: str,
                  model: str = DEFAULT_MODEL) -> int:
    """Wipe ALL cached LLM rows for (project_id, fqsig, model). After this the
    method behaves as if it has never been analyzed — next 🤖 AI 분석 click
    will run a fresh inference."""
    ensure_schema()
    with get_conn() as c:
        cur = c.execute(
            "DELETE FROM llm_method_analysis WHERE project_id=? AND fqsig=? AND model=?",
            (project_id, fqsig, model),
        )
        return cur.rowcount


def set_applied(project_id: str, fqsig: str, applied: bool,
                model: str = DEFAULT_MODEL) -> int:
    """Mark the latest cached LLM result for (project_id, fqsig) as applied/unapplied.
    Returns rowcount affected."""
    ensure_schema()
    with get_conn() as c:
        # Update only the latest row to avoid touching historic body_hash entries
        cur = c.execute(
            """UPDATE llm_method_analysis SET applied=?
               WHERE id = (
                 SELECT id FROM llm_method_analysis
                 WHERE project_id=? AND fqsig=? AND model=?
                 ORDER BY id DESC LIMIT 1
               )""",
            (1 if applied else 0, project_id, fqsig, model),
        )
        return cur.rowcount


def analyze_method(project_id: str, ctx: dict,
                   model: str = DEFAULT_MODEL,
                   force: bool = False) -> dict:
    """Analyze a method via Ollama. Cached by (fqsig, body_hash, model).

    `ctx` is a dict with: class_fqcn, fqsig, name, layer, annotations,
    return_type, body_shape, sloc, static_verdict, calls, source,
    optionally program_name, module_description.
    """
    ensure_schema()
    fqsig = ctx["fqsig"]
    bh = _body_hash(ctx["source"])

    if not force:
        hit = get_cached(project_id, fqsig, bh, model)
        if hit:
            return hit

    prompt = build_prompt(ctx)
    try:
        raw, dur = _call_ollama(model, SYSTEM_PROMPT, prompt)
    except Exception as e:
        result = {"error": f"ollama call failed: {e}", "model": model, "cached": False}
        with get_conn() as c:
            c.execute(
                """INSERT OR REPLACE INTO llm_method_analysis
                (project_id, fqsig, body_hash, model, error, duration_ms)
                VALUES (?,?,?,?,?,?)""",
                (project_id, fqsig, bh, model, str(e), 0),
            )
        return result

    try:
        parsed = _parse_response(raw)
    except Exception as e:
        with get_conn() as c:
            c.execute(
                """INSERT OR REPLACE INTO llm_method_analysis
                (project_id, fqsig, body_hash, model, raw_response, error, duration_ms)
                VALUES (?,?,?,?,?,?,?)""",
                (project_id, fqsig, bh, model, raw, f"parse failed: {e}", dur),
            )
        return {"error": f"parse failed: {e}", "raw": raw[:500], "model": model, "cached": False}

    with get_conn() as c:
        c.execute(
            """INSERT OR REPLACE INTO llm_method_analysis
            (project_id, fqsig, body_hash, model,
             verdict, confidence, reasoning, suggested_target_intent,
             concerns_json, raw_response, duration_ms)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (
                project_id, fqsig, bh, model,
                parsed["verdict"], parsed["confidence"], parsed["reasoning"],
                parsed["suggested_target_intent"],
                json.dumps(parsed["concerns"], ensure_ascii=False),
                raw[:4000], dur,
            ),
        )

    parsed.update({"model": model, "cached": False, "duration_ms": dur})
    return parsed


# ---------------- context builder ----------------

def build_context_from_db(project_id: str, fqsig: str) -> dict | None:
    """Assemble the prompt context by joining BOTH static analyses.

    Sources:
      - java_method_semantic   (JavaParser+SymbolSolver — call graph, fan-in, target resolution)
      - source_file            (file location)
      - program_row            (spec metadata)
      - ast_analyzer on demand (tree-sitter — cc, nesting, body_class, verdict, label_kr)
    """
    ensure_schema()
    with get_conn() as c:
        m = c.execute(
            """SELECT * FROM java_method_semantic
               WHERE project_id=? AND fqsig=?""",
            (project_id, fqsig),
        ).fetchone()
        if not m:
            return None
        m = dict(m)

        cls = c.execute(
            "SELECT file FROM java_class_semantic WHERE project_id=? AND fqcn=?",
            (project_id, m["class_fqcn"]),
        ).fetchone()
        if not cls:
            return None

        # Match source_file by FQCN tail (path layouts differ — see prior fix)
        fqcn = m["class_fqcn"]
        tail = fqcn.replace(".", "/") + ".java"
        sf = c.execute(
            """SELECT abs_path FROM source_file
               WHERE project_id=? AND ext='.java' AND rel_path LIKE ?
               LIMIT 1""",
            (project_id, f"%{tail}"),
        ).fetchone()
        if not sf:
            return None
        abs_path = sf["abs_path"]

        spec = c.execute(
            """SELECT program_name, description FROM program_row
               WHERE project_id=? AND module_name = ?
               LIMIT 1""",
            (project_id, m["class_fqcn"].split(".")[-1]),
        ).fetchone()

    # method source slice
    try:
        text = Path(abs_path).read_text(encoding="utf-8", errors="ignore")
        lines = text.splitlines()
        method_src = "\n".join(lines[m["start_line"] - 1 : m["end_line"]])
    except Exception:
        method_src = "(소스 읽기 실패)"

    # tree-sitter — extra metrics + verdict opinion (best effort)
    ts_fn = None
    try:
        from ast_analyzer import analyze_file
        ast = analyze_file(abs_path, ".java")
        for fn in ast.get("functions", []):
            if fn.get("name") == m["name"] and abs(
                fn.get("start_line", 0) - m["start_line"]
            ) <= 2:
                ts_fn = fn
                break
    except Exception:
        ts_fn = None

    return {
        "class_fqcn": m["class_fqcn"],
        "fqsig": fqsig,
        "name": m["name"],
        "layer": m["layer"],
        "annotations": json.loads(m["annotations_json"] or "[]"),
        "return_type": m["return_type"],

        # semantic (JavaParser+SymbolSolver)
        "sem_body_shape": m["body_shape"],
        "sem_sloc": m["sloc"],
        "sem_statement_count": m["statement_count"],
        "sem_fan_in": m["fan_in_count"],
        "sem_delegation_target": m["delegation_target"],
        "sem_delegation_target_layer": m["delegation_target_layer"],
        "sem_delegation_target_sloc": m["delegation_target_sloc"],
        "sem_param_usage_rate": m["param_usage_rate"],
        "sem_throws_not_impl": bool(m["throws_not_impl"]),
        "calls": json.loads(m["calls_json"] or "[]"),

        # tree-sitter — may be None if analysis failed
        "ts_verdict": ts_fn.get("verdict") if ts_fn else None,
        "ts_label_kr": ts_fn.get("label_kr") if ts_fn else None,
        "ts_status": ts_fn.get("status") if ts_fn else None,
        "ts_body_class": ts_fn.get("body_class") if ts_fn else None,
        "ts_sloc": ts_fn.get("sloc") if ts_fn else None,
        "ts_cc": ts_fn.get("cyclomatic_complexity") if ts_fn else None,
        "ts_stmts": ts_fn.get("statement_count") if ts_fn else None,
        "ts_fan_out": ts_fn.get("fan_out") if ts_fn else None,
        "ts_nesting": ts_fn.get("nesting_depth") if ts_fn else None,
        "ts_parameter_count": ts_fn.get("parameter_count") if ts_fn else None,
        "ts_fan_out_targets": ts_fn.get("fan_out_targets") if ts_fn else None,

        # source code + spec
        "source": method_src,
        "program_name": spec["program_name"] if spec else None,
        "module_description": spec["description"] if spec else None,
    }

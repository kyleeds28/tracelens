"""Project / program level AI summary — LLM 강화 버전 전용 모듈.

이전 시도 (source_mapping) 에서 빼낸 기능을 인용 형식 개선해서 재도입:

  - 문제 1 (이전): "supports() [stub_literal] in RestControllerMessageAdvice"
    → 사용자가 '실제 코드 그대로' 로 오해
  - 해결: 자바 메서드 정식 시그니처 (ClassName.methodName) 만 사용,
    내부 분류 라벨([stub_literal] 등) 은 별도 컬럼으로 분리.

  - 문제 2 (이전): 7B 모델이 STUB / 의도적 구현 구분 못해 false positive
  - 해결: 프롬프트에 "본문 직접 보지 않으면 단정하지 말 것" 명시,
    LLM 출력 grade 를 'GOOD/FAIR/ATTENTION' 대신
    'STATIC_HEALTHY/SOME_CONCERNS/NEEDS_REVIEW' 로 부드럽게,
    hotspot 은 "검토 후보" 로 포지셔닝 (단정 아닌 의심 리스트).
"""
from __future__ import annotations

import json
from collections import Counter

from db import get_conn
from llm_analysis import _call_ollama, DEFAULT_MODEL


SYSTEM_PROMPT = (
    "당신은 자바 백엔드 산출문서 검증 보조 도우미입니다. "
    "정적 분석 수치와 의심 항목 샘플이 주어지면 임원/PM 이 읽기 좋은 "
    "한국어 요약을 작성하세요. 응답은 단일 JSON 만 출력하세요. "
    "주의: "
    "(1) 수치를 재계산하지 말고 주어진 것을 그대로 인용. "
    "(2) 메서드 인용은 'ClassName.methodName()' 형식으로만. "
    "(3) 본문을 보지 못한 상태에서 'STUB 이다' 같은 단정은 피하고 "
    "'검토 권장' 형태로 부드럽게 표현. "
    "(4) 정적 분석이 의심으로 분류한 메서드 중 사실은 의도적 구현 (Spring Persistable.isNew, "
    "ResponseBodyAdvice.supports 같은 표준 패턴) 인 경우가 있을 수 있다는 점 인지하고, "
    "단정 대신 '검토 후보' 로 표현."
)


def _format_method_ref(class_fqcn: str, name: str) -> str:
    """클래스 simple name + 메서드명 — 자바 표준 인용 형식."""
    simple = (class_fqcn or "").split(".")[-1] or "?"
    return f"{simple}.{name}()"


# main.py LLM_TO_STATUS 의 ok 그룹과 동기화 필요 — 적용된 LLM 판정이 이 셋이면
# 점수판에서 '정상' 으로 승격된 메서드이므로 우려 항목 샘플에서도 제외한다.
# (안 그러면 점수판은 정상 100% 인데 종합 분석은 같은 메서드를 계속 의심으로 나열함)
_NOT_AI_NORMALIZED = (
    " AND NOT EXISTS (SELECT 1 FROM llm_method_analysis l "
    "WHERE l.project_id=m.project_id AND l.fqsig=m.fqsig AND l.applied=1 "
    "AND l.verdict IN ('REAL_DELEGATION','REAL_LOGIC','MOVED_TO_SIBLING'))"
)


def _layer_top(layer_dict, n=5):
    items = sorted((layer_dict or {}).items(), key=lambda x: -x[1])[:n]
    return " · ".join(f"{k} {v}" for k, v in items)


def _bullet(items, indent="  "):
    return "\n".join(f"{indent}- {it}" for it in items) or f"{indent}(없음)"


def collect_concerning_items(project_id: str, program_id: str | None = None,
                              limit_per_kind: int = 5) -> dict:
    """우려 항목 — 정직한 인용을 위해 class_fqcn/method 분리해서 반환."""
    with get_conn() as c:
        scope_fqcns: set[str] | None = None
        if program_id:
            rows = c.execute(
                """SELECT DISTINCT sf.fqcn FROM source_file sf
                   JOIN mapping m ON m.source_file_id = sf.id
                   JOIN program_row pr ON pr.id = m.program_row_id
                   WHERE pr.project_id=? AND pr.program_id=? AND sf.fqcn IS NOT NULL""",
                (project_id, program_id),
            ).fetchall()
            scope_fqcns = {r["fqcn"] for r in rows}

        def _where_clause():
            if not program_id:
                return "", []
            if not scope_fqcns:
                return " AND 0=1", []
            ph = ",".join("?" * len(scope_fqcns))
            return f" AND m.class_fqcn IN ({ph})", list(scope_fqcns)

        sw, args = _where_clause()

        # ---- 1) STUB 후보 (class, method 분리 보존) ----
        stub_rows = c.execute(
            f"""SELECT m.name, m.class_fqcn, m.body_shape, m.start_line, m.end_line
                FROM java_method_semantic m
                WHERE m.project_id=?
                  AND m.body_shape IN ('stub_throw','stub_literal','stub_debug','empty'){sw}{_NOT_AI_NORMALIZED}
                LIMIT ?""",
            (project_id, *args, limit_per_kind),
        ).fetchall()
        stub_examples = [
            {
                "ref": _format_method_ref(r["class_fqcn"], r["name"]),
                "fqcn": r["class_fqcn"],
                "body_shape": r["body_shape"],
                "lines": f"{r['start_line']}~{r['end_line']}",
            }
            for r in stub_rows
        ]

        # ---- 2) 매칭=X 모듈 ----
        if program_id:
            miss = c.execute(
                """SELECT pr.module_name, pr.package, pr.program_id, pr.kind
                   FROM mapping mp
                   JOIN program_row pr ON pr.id = mp.program_row_id
                   WHERE mp.project_id=? AND pr.program_id=? AND mp.status='X'
                   LIMIT ?""",
                (project_id, program_id, limit_per_kind),
            ).fetchall()
        else:
            miss = c.execute(
                """SELECT pr.module_name, pr.package, pr.program_id, pr.kind
                   FROM mapping mp
                   JOIN program_row pr ON pr.id = mp.program_row_id
                   WHERE mp.project_id=? AND mp.status='X' LIMIT ?""",
                (project_id, limit_per_kind),
            ).fetchall()
        missing_modules = [
            {
                "module_name": r["module_name"] or "(이름없음)",
                "spec_package": r["package"] or "(패키지 미지정)",
                "program_id": r["program_id"],
                "kind": r["kind"] or "",
            }
            for r in miss
        ]

        # ---- 3) 분류 보류 메서드 ----
        unk_rows = c.execute(
            f"""SELECT m.name, m.class_fqcn, m.body_shape, m.sloc, m.start_line, m.end_line
                FROM java_method_semantic m
                WHERE m.project_id=? AND m.body_shape IN
                      ('single_return','single_statement','single_throw','empty_return')
                      {sw}{_NOT_AI_NORMALIZED}
                ORDER BY m.sloc DESC LIMIT ?""",
            (project_id, *args, limit_per_kind),
        ).fetchall()
        unknowns = [
            {
                "ref": _format_method_ref(r["class_fqcn"], r["name"]),
                "fqcn": r["class_fqcn"],
                "sloc": r["sloc"],
                "body_shape": r["body_shape"],
                "lines": f"{r['start_line']}~{r['end_line']}",
            }
            for r in unk_rows
        ]

        # ---- 4) 호출 추적 불가 (opaque) ----
        opaque_rows = c.execute(
            f"""SELECT m.name, m.class_fqcn, m.sloc, m.calls_json,
                       m.start_line, m.end_line
                FROM java_method_semantic m
                WHERE m.project_id=? AND m.sloc>=3 AND m.calls_json LIKE '%resolved%'{sw}{_NOT_AI_NORMALIZED}
                LIMIT 300""",
            (project_id, *args),
        ).fetchall()
        opaque = []
        for r in opaque_rows:
            try:
                cs = json.loads(r["calls_json"] or "[]")
            except Exception:
                continue
            if cs and not any(x.get("resolved") for x in cs):
                opaque.append({
                    "ref": _format_method_ref(r["class_fqcn"], r["name"]),
                    "fqcn": r["class_fqcn"],
                    "sloc": r["sloc"],
                    "call_count": len(cs),
                    "lines": f"{r['start_line']}~{r['end_line']}",
                })
                if len(opaque) >= limit_per_kind:
                    break

    return {
        "stub_examples": stub_examples,
        "missing_module_examples": missing_modules,
        "unknown_method_examples": unknowns,
        "opaque_call_examples": opaque,
    }


def build_summary_prompt(summary: dict, concerns: dict, project_name: str = "") -> str:
    scope = "프로그램 단위" if summary.get("scope") == "program" else "프로젝트 전체"
    scope_label = (
        f"프로그램 ID = {summary.get('program_id')}"
        if summary.get("scope") == "program"
        else project_name or "프로젝트 전체"
    )

    totals = summary.get("totals") or {}
    mapping = summary.get("mapping_status") or {}
    mstatus = summary.get("method_status_effective") or {}
    stubs = summary.get("stubs") or {}
    layers = summary.get("method_layer") or {}
    llm = summary.get("llm") or {}
    issues = summary.get("spec_issues") or {}

    return f"""## 분석 범위
- 범위: {scope}  ({scope_label})
- 모듈 {totals.get('modules', '?')} · 파일 {totals.get('files', '?')} · 메서드 {totals.get('methods', '?')}

## 정적 분석 수치 (재계산 금지, 그대로 인용)
- 매칭 일치 {mapping.get('O', 0)} · 부분 {mapping.get('PARTIAL', 0)} · 불일치 {mapping.get('X', 0)}
- 메서드 상태 정상 {mstatus.get('ok', 0)} · 의심 {mstatus.get('suspect', 0)} · 알수없음 {mstatus.get('unknown', 0)}
- 스텁: 임시반환 {stubs.get('STUB_PLACEHOLDER', 0)} · 빈본문 {stubs.get('STUB_EMPTY', 0)} · 미구현예외 {stubs.get('STUB_NOT_IMPL', 0)} · 디버그만 {stubs.get('STUB_DEBUG_ONLY', 0)}
- 계층 분포 (TOP 5): {_layer_top(layers, 5)}
- AI 메서드 분석: 분석 {llm.get('analyzed', 0)} · 적용 {llm.get('applied', 0)} · 정정 (의심→정상) {llm.get('overrode_to_ok', 0)} · 정정 (정상→의심) {llm.get('overrode_to_suspect', 0)}
- 산출문서 이슈: 패키지 정정 {issues.get('PACKAGE_DRIFT', 0)} · 후보 다수 {issues.get('AMBIGUOUS', 0)} · 모듈 누락 {issues.get('MODULE_MISSING', 0)} · 합계 {issues.get('total', 0)}

## 검토 후보 집계 (구체 목록·이름은 시스템이 코드에서 직접 생성 — 너는 쓰지 말 것)
- STUB 분류 메서드: {len(concerns.get('stub_examples', []))}건
- 코드에서 못 찾은(불일치) 모듈: {len(concerns.get('missing_module_examples', []))}건
- 정적 분류 보류 메서드: {len(concerns.get('unknown_method_examples', []))}건
- 호출 추적 불가 메서드: {len(concerns.get('opaque_call_examples', []))}건

## 작성 지침
- 위 '정적 분석 수치' 와 '검토 후보 집계' 의 숫자만 근거로, 집계 수준에서 서술한다.
- 개별 메서드명·모듈명을 narrative/strengths/concerns/next_actions 에 인용하지 말 것.
  (구체적인 '검토 후보' 목록은 시스템이 코드에서 만들어 화면에 따로 보여준다.)
- 카테고리를 섞지 말 것 — '불일치 모듈' 을 'STUB' 이라 부르는 식의 혼동 금지. 각 수치를 정확한 이름으로 서술.
- 0 건인 항목을 "발견되었다" 고 쓰지 말 것 (없으면 없다고 하거나 언급하지 않는다).
- 본문을 직접 보지 못했으므로 단정 대신 '검토 권장' 형태로 표현.

## 출력 (이 JSON 만 — hotspots 는 출력하지 말 것, 시스템이 채운다)
{{
  "overall_grade": "STATIC_HEALTHY" | "SOME_CONCERNS" | "NEEDS_REVIEW",
  "narrative": "3~5 문장 한글 요약 (수치 인용 + 솔직한 평가, 개별 이름 금지)",
  "strengths": ["1-2 문장씩 강점 2~3개 (개별 이름 금지)"],
  "concerns": ["1-2 문장씩 약점 또는 검토 권고 2~3개 (개별 이름 금지)"],
  "next_actions": ["우선순위 순 한글 액션 1~3개 (개별 이름 금지)"]
}}
"""


VALID_GRADES = {"STATIC_HEALTHY", "SOME_CONCERNS", "NEEDS_REVIEW",
                # 호환: 옛 라벨도 받음
                "GOOD", "FAIR", "ATTENTION"}

GRADE_NORMALIZE = {
    "GOOD": "STATIC_HEALTHY",
    "FAIR": "SOME_CONCERNS",
    "ATTENTION": "NEEDS_REVIEW",
}


def _parse(text: str) -> dict:
    obj = json.loads(text)
    grade = obj.get("overall_grade") or "SOME_CONCERNS"
    if grade not in VALID_GRADES:
        grade = "SOME_CONCERNS"
    grade = GRADE_NORMALIZE.get(grade, grade)

    def _str_list(v, cap=10):
        if not isinstance(v, list):
            return []
        # 모델이 출력 스키마의 <...> 플레이스홀더 슬롯을 그대로 복사한 경우 버린다
        out = []
        for x in v:
            s = str(x).strip()
            if not s or ("<" in s and ">" in s):
                continue
            out.append(s)
        return out[:cap]

    hotspots = []
    for h in (obj.get("hotspots") or [])[:6]:
        if not isinstance(h, dict):
            continue
        hotspots.append({
            "area": str(h.get("area", "")).strip(),
            "items": _str_list(h.get("items"), 8),
            "recommendation": str(h.get("recommendation", "")).strip(),
        })

    return {
        "overall_grade": grade,
        "narrative": str(obj.get("narrative", "")).strip(),
        "strengths": _str_list(obj.get("strengths")),
        "concerns": _str_list(obj.get("concerns")),
        "hotspots": hotspots,
        "next_actions": _str_list(obj.get("next_actions"), 5),
    }


def build_deterministic_hotspots(concerns: dict) -> list[dict]:
    """검토 후보 목록을 LLM 이 아니라 버킷에서 직접 만든다.

    7B 모델이 '불일치 모듈' 을 'STUB' 박스로 옮기는 등 카테고리 오라벨을
    원천 차단하기 위함 — 이름/항목은 collect_concerning_items 의 버킷에서만 가져온다.
    각 버킷이 비어 있으면 해당 hotspot 자체를 만들지 않는다.
    """
    hotspots: list[dict] = []

    stubs = concerns.get("stub_examples") or []
    if stubs:
        hotspots.append({
            "area": "STUB 분류된 메서드 검토 후보",
            "items": [f"{it['ref']} (라인 {it['lines']}, 정적분류 {it['body_shape']})"
                      for it in stubs],
            "recommendation": "임시반환/빈본문/미구현예외로 분류된 메서드. 의도된 구현인지 "
                              "본문을 직접 열어 확인하고, 미구현이면 채운다.",
        })

    missing = concerns.get("missing_module_examples") or []
    if missing:
        def _miss_label(it):
            kind = it.get("kind") or ""
            tag = f" [{kind}]" if kind else ""
            return f"{it['module_name']}{tag} (산출문서 패키지 {it['spec_package']}, " \
                   f"프로그램 {it['program_id']})"
        hotspots.append({
            "area": "코드에서 못 찾은(불일치) 모듈",
            "items": [_miss_label(it) for it in missing],
            "recommendation": "산출문서에는 정의됐으나 소스에서 매칭 클래스를 못 찾은 모듈. "
                              "미구현이거나 이름 표기가 어긋난 경우이니 구현 여부와 명칭을 확인한다.",
        })

    unknowns = concerns.get("unknown_method_examples") or []
    if unknowns:
        hotspots.append({
            "area": "정적 분류 보류 메서드",
            "items": [f"{it['ref']} (라인 {it['lines']}, SLOC {it['sloc']}, "
                      f"정적분류 {it['body_shape']})" for it in unknowns],
            "recommendation": "단일 반환/단일 구문 등으로 정적 분류가 보류된 메서드. "
                              "실제 로직 충실도를 검토한다.",
        })

    opaque = concerns.get("opaque_call_examples") or []
    if opaque:
        hotspots.append({
            "area": "호출 추적 불가 메서드",
            "items": [f"{it['ref']} (라인 {it['lines']}, SLOC {it['sloc']}, "
                      f"본문 호출 {it['call_count']}건 모두 미해석)" for it in opaque],
            "recommendation": "본문 호출이 전부 외부 의존성이라 정적으로 추적되지 않은 메서드. "
                              "외부 라이브러리 호출이면 정상일 수 있으니 본문을 확인한다.",
        })

    return hotspots


def summarize(project_id: str, summary: dict, project_name: str = "",
              program_id: str | None = None,
              model: str = DEFAULT_MODEL) -> dict:
    """프로젝트 또는 프로그램 단위 종합 의견 생성."""
    concerns = collect_concerning_items(project_id, program_id)
    prompt = build_summary_prompt(summary, concerns, project_name)
    try:
        raw, dur = _call_ollama(
            model, SYSTEM_PROMPT, prompt,
            num_predict=1200, timeout=300,
        )
    except Exception as e:
        return {"error": f"ollama call failed: {e}", "model": model}
    try:
        parsed = _parse(raw)
    except Exception as e:
        return {
            "error": f"parse failed: {e}",
            "raw": raw[:500],
            "model": model,
        }
    # hotspots(검토 후보) 는 LLM 출력이 아니라 버킷에서 결정론적으로 채운다.
    # → 7B 가 '불일치 모듈' 을 'STUB' 으로 오라벨하는 것을 원천 차단.
    parsed["hotspots"] = build_deterministic_hotspots(concerns)
    parsed["model"] = model
    parsed["duration_ms"] = dur
    parsed["scope"] = "program" if program_id else "project"
    parsed["program_id"] = program_id
    return parsed

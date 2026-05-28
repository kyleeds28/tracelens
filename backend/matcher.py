"""Map a `program_row` (xlsx 한 행) to a source file.

Strategy hierarchy (strongest → weakest):
    1.  fqcn             — Package + 모듈명 정확히 일치하는 단일 파일
    2.  filename<ext>    — Front 행: 모듈명 + 확장자 정확 일치
    3.  auto_name        — Package 없음, 모듈명으로 후보 1개만 — 단일 매칭이라 신뢰 가능
    4.  hint_disambig    — Package 없음, 후보 N개 → menu_url/category로 추려 단일
    5.  simple_name      — Package 있는데 못 찾고 simple name으로 떨어짐 (이름만 일치, 패키지 다름)
    6.  filename_ci      — Vue: 대소문자 무시 fallback
    7.  any_name         — kind 미지정, 그냥 이름만 매칭
    *   ...miss          — 매칭 실패

`Package` 컬럼이 비어있을 때(분석환경 xlsx 같은 케이스) 매처는
kind_norm + menu_url + category_l1 같은 다른 컬럼의 단서를 활용해
가장 그럴듯한 파일 하나를 고른다.
"""
from typing import Any

# match_strategy → 화면에 보일 status
OK_STRATEGIES = {"fqcn", "auto_name", "hint_disambig"}
PARTIAL_STRATEGIES = {"simple_name", "filename_ci", "any_name"}


def _norm(s: Any) -> str:
    if s is None:
        return ""
    return str(s).strip()


def _path_lower(f: dict) -> str:
    return (f.get("rel_path") or f.get("abs_path") or "").lower()


def _disambiguate(candidates: list[dict], row: dict) -> dict | None:
    """후보가 여러 개일 때 단서로 1개 추리기. 못 추리면 None."""
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]

    hints: list[str] = []
    menu_url = _norm(row.get("menu_url"))
    if menu_url:
        # /admin/users/list → ["admin", "users", "list"]
        hints += [seg for seg in menu_url.lower().split("/") if seg]
    cat1 = _norm(row.get("category_l1")).lower()
    cat2 = _norm(row.get("category_l2")).lower()
    # 한국어 카테고리에 영문 키워드 매핑
    KO_TO_EN = {
        "관리자": "admin", "어드민": "admin", "사용자": "user", "회원": "member",
        "프론트": "front", "백엔드": "backend",
    }
    for ko, en in KO_TO_EN.items():
        if ko in cat1 or ko in cat2:
            hints.append(en)
        if ko in menu_url:
            hints.append(en)

    if not hints:
        return None

    # 각 후보의 경로에 hint가 등장하는 횟수로 점수
    scored: list[tuple[int, dict]] = []
    for f in candidates:
        path = _path_lower(f)
        score = sum(1 for h in hints if h in path)
        scored.append((score, f))
    scored.sort(key=lambda x: -x[0])
    top, second = scored[0], scored[1] if len(scored) > 1 else (-1, None)
    if top[0] > second[0]:
        return top[1]
    return None  # 동점 → 단정 불가


def match_program_row(row: dict, files: list[dict], cfg: dict) -> tuple[dict | None, str]:
    kind = row.get("kind_norm")
    matching_cfg = cfg.get("matching", {})

    if kind == "api":
        api_cfg = matching_cfg.get("api", {})
        exts = set(api_cfg.get("extensions", [".java"]))
        pkg = _norm(row.get("package"))
        mod = _norm(row.get("module_name"))
        if not mod:
            return None, "no_module_name"

        # Package 있음 → FQCN 시도 (가장 강한 매칭)
        if pkg:
            expected_fqcn = f"{pkg}.{mod}"
            for f in files:
                if f["ext"] in exts and f.get("fqcn") == expected_fqcn:
                    return f, "fqcn"

        # Package 없음 (또는 fqcn 미일치) → simple name 후보 수집
        candidates = [f for f in files if f["ext"] in exts and f.get("simple_name") == mod]

        if not candidates:
            return None, "fqcn_miss"

        # Package 없는 경우는 그 자체로 PARTIAL이 아니라 "사실상 최선의 매칭"
        if not pkg:
            if len(candidates) == 1:
                return candidates[0], "auto_name"
            chosen = _disambiguate(candidates, row)
            if chosen is not None:
                return chosen, "hint_disambig"
            # 동점이면 첫 번째 반환 + simple_name 표기 (사용자가 확인할 것)
            return candidates[0], "simple_name"

        # Package 있었는데 FQCN이 안 맞은 케이스 (오타·이전 버전 등) → 약한 매칭
        return candidates[0], "simple_name"

    if kind == "front":
        fr_cfg = matching_cfg.get("front", {})
        exts = fr_cfg.get("extensions", [".vue", ".tsx", ".jsx"])
        mod = _norm(row.get("module_name"))
        if not mod:
            return None, "no_module_name"
        # try each extension in order; collect candidates
        for ext in exts:
            cands = [f for f in files if f["ext"] == ext and f.get("simple_name") == mod]
            if cands:
                if len(cands) == 1:
                    return cands[0], f"filename{ext}"
                chosen = _disambiguate(cands, row)
                if chosen is not None:
                    return chosen, "hint_disambig"
                return cands[0], f"filename{ext}"
        # case-insensitive fallback
        mod_l = mod.lower()
        for f in files:
            if f["ext"] in exts and f.get("simple_name", "").lower() == mod_l:
                return f, "filename_ci"
        return None, "filename_miss"

    # unknown kind: try both
    mod = _norm(row.get("module_name"))
    if mod:
        cands = [f for f in files if f.get("simple_name") == mod]
        if cands:
            if len(cands) == 1:
                return cands[0], "auto_name"
            chosen = _disambiguate(cands, row)
            if chosen is not None:
                return chosen, "hint_disambig"
            return cands[0], "any_name"
    return None, "unknown_kind"


def status_from_strategy(strategy: str) -> str:
    if strategy in OK_STRATEGIES or (strategy.startswith("filename") and strategy != "filename_miss"):
        return "O"
    if strategy in PARTIAL_STRATEGIES:
        return "PARTIAL"
    return "X"

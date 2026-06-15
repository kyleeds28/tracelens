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
import re
from typing import Any

# match_strategy → 화면에 보일 status
OK_STRATEGIES = {"fqcn", "auto_name", "hint_disambig", "auto_locality"}
PARTIAL_STRATEGIES = {"simple_name", "filename_ci", "any_name"}

# Tier A 자동 확정: 형제 O 매칭이 최소 이 개수 이상 수렴해야 패키지 prefix 를 신뢰.
AUTO_LOCALITY_MIN_SIBLINGS = 2

_CAMEL_TOKEN_RE = re.compile(r"[A-Z]+(?![a-z])|[A-Z][a-z]*|[0-9]+")


def _camel_tokens(name: str) -> list[str]:
    return _CAMEL_TOKEN_RE.findall(name or "")


def _role_token(name: str) -> str:
    """이름의 끝 CamelCase 토큰(역할) 을 소문자로. 역할 접미사가 없으면 ''.

    범용: 특정 어휘 사전 없이 'Entity/Controller/Service/DTO/...' 같은
    역할 토큰을 이름 구조만으로 뽑는다. 단일 토큰(역할 접미사 없음) 이면 '' 반환.
    """
    toks = _camel_tokens(name)
    if len(toks) < 2:
        return ""
    return toks[-1].lower()


def _common_pkg_prefix(packages: list[str]) -> str:
    """패키지들의 '.' 세그먼트 단위 공통 prefix. 기능 묶음 루트를 잡는 데 사용."""
    segs = [p.split(".") for p in packages if p]
    if not segs:
        return ""
    out: list[str] = []
    for parts in zip(*segs):
        first = parts[0]
        if all(x == first for x in parts):
            out.append(first)
        else:
            break
    return ".".join(out)


def auto_match_by_locality(program_rows: list[dict],
                           mapping_by_row: dict[int, dict],
                           files: list[dict]) -> list[dict]:
    """Tier A — 구조 신호만으로 X(api) 행을 엄격 유일일 때만 자동 확정.

    도메인 단어/사전 없이 어느 프로젝트에나 통하도록 두 신호만 사용:
      1) 같은 program 의 O 매칭(형제) 들의 패키지 '공통 prefix' = 기능 묶음 위치
      2) 스펙 모듈명의 역할 토큰(끝 CamelCase) 과 같은 역할의 .java 클래스가
         그 subtree 안에 '정확히 1개' 일 때만 매칭

    엄격 유일이 아니면(수렴 안 됨 / 후보 0·2개 / 같은 역할 스펙행 다수) 손대지 않는다.
    반환: [{row_id, source_file_id, strategy='auto_locality'}, ...]
    """
    fid = {f["id"]: f for f in files}
    java_files = [
        f for f in files
        if (f.get("ext") or "").lower() == ".java" and f.get("package")
    ]

    by_prog: dict[Any, list[dict]] = {}
    for r in program_rows:
        by_prog.setdefault(r.get("program_id"), []).append(r)

    results: list[dict] = []
    for rows in by_prog.values():
        sib_pkgs: list[str] = []
        role_count: dict[str, int] = {}
        for r in rows:
            role = _role_token(_norm(r.get("module_name")))
            if role:
                role_count[role] = role_count.get(role, 0) + 1
            mp = mapping_by_row.get(r["id"])
            if mp and mp.get("status") == "O" and mp.get("source_file_id"):
                f = fid.get(mp["source_file_id"])
                if f and f.get("package"):
                    sib_pkgs.append(f["package"])

        if len(sib_pkgs) < AUTO_LOCALITY_MIN_SIBLINGS:
            continue
        prefix = _common_pkg_prefix(sib_pkgs)
        if not prefix:
            continue
        subtree = [
            f for f in java_files
            if f["package"] == prefix or f["package"].startswith(prefix + ".")
        ]

        for r in rows:
            mp = mapping_by_row.get(r["id"])
            if not mp or mp.get("status") != "X" or mp.get("manual_override"):
                continue
            if r.get("kind_norm") != "api":
                continue
            role = _role_token(_norm(r.get("module_name")))
            if not role or role_count.get(role, 0) != 1:
                # 역할 접미사 없음 or 같은 역할 스펙행이 둘 이상(경쟁) → 자동 금지
                continue
            matches = [
                f for f in subtree
                if _role_token(f.get("simple_name") or "") == role
            ]
            if len(matches) == 1:
                results.append({
                    "row_id": r["id"],
                    "source_file_id": matches[0]["id"],
                    "strategy": "auto_locality",
                })
    return results


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

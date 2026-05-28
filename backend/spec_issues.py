"""Detect spec freshness issues — places where the deliverable doc (xlsx)
does not match the actual source code, with actionable fix suggestions.

Issue kinds detected:

  PACKAGE_DRIFT  - module exists with the same name, but in a different package
                   (matcher landed on simple_name strategy = PARTIAL).
                   Most often caused by a code refactor that the spec did not
                   follow.

  MODULE_MISSING - the module name appears nowhere in the source index (X).
                   May be a rename/removal. We try to surface close-name
                   candidates so the spec author can decide quickly.

  AMBIGUOUS      - same simple_name exists in multiple packages. The matcher
                   picked the first hit, but the spec author should pick
                   explicitly.

Derived on demand from existing tables — no new schema.
"""
from __future__ import annotations

import csv
import io
from collections import Counter
from difflib import get_close_matches

from db import get_conn


def _index_simple_names(c, project_id: str) -> dict[str, list[dict]]:
    """simple_name -> [{fqcn, package, rel_path}, ...] (java files only)."""
    rows = c.execute(
        """SELECT simple_name, fqcn, package, rel_path
           FROM source_file
           WHERE project_id=? AND ext='.java' AND simple_name IS NOT NULL""",
        (project_id,),
    ).fetchall()
    idx: dict[str, list[dict]] = {}
    for r in rows:
        idx.setdefault(r["simple_name"], []).append(dict(r))
    return idx


def _row_label(r: dict) -> str:
    parts = []
    if r.get("program_name"):
        parts.append(r["program_name"])
    elif r.get("program_id"):
        parts.append(r["program_id"])
    if r.get("module_name"):
        parts.append(r["module_name"])
    return " / ".join(parts) or f"row#{r.get('row_idx')}"


def detect(project_id: str) -> dict:
    """Return a categorized list of spec-vs-code freshness issues.

    Shape:
      {
        "total": N,
        "by_kind": {"PACKAGE_DRIFT": x, "MODULE_MISSING": y, "AMBIGUOUS": z},
        "issues": [ {kind, severity, row_id, row_idx, program_id, program_name,
                     module_name, spec_package, actual_package, actual_path,
                     candidates?, similar_names?, suggestion, fix?} , ... ]
      }
    """
    with get_conn() as c:
        # exclude rows the user has manually confirmed — their issue is resolved
        rows = [dict(r) for r in c.execute(
            """SELECT pr.id, pr.row_idx, pr.program_id, pr.program_name,
                      pr.kind_norm, pr.package, pr.module_name,
                      m.status, m.match_strategy,
                      m.manual_override,
                      sf.package AS actual_package, sf.fqcn AS actual_fqcn,
                      sf.rel_path AS actual_path, sf.simple_name AS actual_simple_name
               FROM program_row pr
               LEFT JOIN mapping m ON m.program_row_id = pr.id
               LEFT JOIN source_file sf ON sf.id = m.source_file_id
               WHERE pr.project_id=?
                 AND (m.manual_override IS NULL OR m.manual_override = 0)""",
            (project_id,),
        ).fetchall()]
        simple_index = _index_simple_names(c, project_id)

    all_simple_names = list(simple_index.keys())
    issues: list[dict] = []

    for r in rows:
        kind_norm = (r.get("kind_norm") or "").lower()
        mod = (r.get("module_name") or "").strip()
        spec_pkg = (r.get("package") or "").strip()
        strategy = r.get("match_strategy")
        status = r.get("status")

        # ---- PACKAGE_DRIFT / AMBIGUOUS ----
        if strategy == "simple_name" and r.get("actual_package") and mod:
            candidates = simple_index.get(mod, [])
            base = {
                "row_id": r["id"], "row_idx": r["row_idx"],
                "program_id": r["program_id"], "program_name": r["program_name"],
                "module_name": mod,
                "spec_package": spec_pkg,
                "actual_package": r["actual_package"],
                "actual_fqcn": r["actual_fqcn"],
                "actual_path": r["actual_path"],
            }
            if len(candidates) > 1:
                issues.append({
                    **base,
                    "kind": "AMBIGUOUS",
                    "severity": "warn",
                    "candidates": [
                        {"fqcn": c2["fqcn"], "package": c2["package"], "rel_path": c2["rel_path"]}
                        for c2 in candidates
                    ],
                    "suggestion": (
                        f"같은 이름 파일이 {len(candidates)}곳에 존재합니다. "
                        f"산출문서가 가리키는 패키지를 명시적으로 정정하세요."
                    ),
                })
            else:
                issues.append({
                    **base,
                    "kind": "PACKAGE_DRIFT",
                    "severity": "warn",
                    "suggestion": (
                        f"산출문서의 패키지 '{spec_pkg}' → '{r['actual_package']}' 로 정정 "
                        f"(코드 이동 또는 산출문서 미반영)"
                    ),
                    "fix": {
                        "column": "package",
                        "from": spec_pkg,
                        "to": r["actual_package"],
                    },
                })

        # ---- MODULE_MISSING ----
        elif status == "X" and kind_norm == "api" and mod:
            similar = get_close_matches(mod, all_simple_names, n=5, cutoff=0.6)
            sims_detail: list[dict] = []
            for s in similar:
                for cand in simple_index.get(s, []):
                    sims_detail.append({
                        "simple_name": s,
                        "fqcn": cand["fqcn"],
                        "package": cand["package"],
                        "rel_path": cand["rel_path"],
                    })
                if len(sims_detail) >= 5:
                    break
            issues.append({
                "kind": "MODULE_MISSING",
                "severity": "bad",
                "row_id": r["id"], "row_idx": r["row_idx"],
                "program_id": r["program_id"], "program_name": r["program_name"],
                "module_name": mod,
                "spec_package": spec_pkg,
                "similar_names": sims_detail[:5],
                "suggestion": (
                    "모듈이 소스에 존재하지 않습니다. 삭제 또는 이름 변경 가능성. "
                    + ("유사 이름 후보를 확인하세요." if sims_detail
                       else "유사 이름 후보도 없습니다.")
                ),
            })

    counts = Counter(i["kind"] for i in issues)
    return {
        "total": len(issues),
        "by_kind": dict(counts),
        "issues": issues,
    }


def to_csv(detected: dict) -> str:
    """Render detected issues as CSV (UTF-8 with BOM, Excel-friendly)."""
    buf = io.StringIO()
    buf.write("﻿")  # BOM so Excel opens UTF-8 cleanly
    w = csv.writer(buf, lineterminator="\n")
    w.writerow([
        "row_idx", "kind", "severity", "program_id", "program_name",
        "module_name", "spec_package", "actual_package", "actual_path",
        "fix_column", "fix_from", "fix_to",
        "candidates_or_similar", "suggestion",
    ])
    for i in detected["issues"]:
        fix = i.get("fix") or {}
        if i["kind"] == "AMBIGUOUS":
            extras = " | ".join(c["fqcn"] for c in i.get("candidates", []))
        elif i["kind"] == "MODULE_MISSING":
            extras = " | ".join(f"{s['simple_name']} ({s['fqcn']})" for s in i.get("similar_names", []))
        else:
            extras = ""
        w.writerow([
            i.get("row_idx"), i["kind"], i.get("severity"),
            i.get("program_id"), i.get("program_name"),
            i.get("module_name"), i.get("spec_package"),
            i.get("actual_package"), i.get("actual_path"),
            fix.get("column", ""), fix.get("from", ""), fix.get("to", ""),
            extras, i.get("suggestion", ""),
        ])
    return buf.getvalue()

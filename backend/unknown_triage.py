"""Triage UNKNOWN-layer classes — code the layer classifier could not place
into a standard layer (Controller/Service/Repository/...).

UNKNOWN is a mixed bag. Most of it is *infrastructure / technical-support*
code that legitimately never appears in the deliverable spec (tests,
interceptors, converters, validators, exceptions, ...). A smaller part is
*suspect* — code that probably SHOULD be in the spec but is missing or was
mis-classified (entities, external-link interfaces, plain un-tagged classes).

This module tags every UNKNOWN class with a heuristic category and a
coarse group ("suspect" vs "ignorable") so the UI can show them all with
filters, and surfaces whether each class is actually mapped to any spec row.

Derived on demand from existing tables — no new schema.
"""
from __future__ import annotations

from collections import Counter

from db import get_conn


# tag -> (group, korean label)
TAG_META: dict[str, tuple[str, str]] = {
    "ENTITY_SUSPECT":     ("suspect",   "엔티티 의심"),
    "EXTERNAL_INTERFACE": ("suspect",   "외부연동 인터페이스"),
    "OTHER_SUSPECT":      ("suspect",   "기타 미분류"),
    "TEST":               ("ignorable", "테스트"),
    "INTERCEPTOR":        ("ignorable", "인터셉터/필터"),
    "CONVERTER":          ("ignorable", "컨버터"),
    "VALIDATOR":          ("ignorable", "검증기"),
    "INFRA":              ("ignorable", "기타 인프라"),
}

GROUP_LABEL = {"suspect": "1순위", "ignorable": "2순위"}


def _classify(simple_name: str) -> str:
    """Map a class simple-name to a triage tag (heuristic, name-pattern based)."""
    n = simple_name or ""

    # ---- ignorable: infrastructure / technical-support ----
    if n.endswith("Test") or n.endswith("Tests") or n.endswith("IT"):
        return "TEST"
    if n.endswith("Interceptor") or n.endswith("Filter"):
        return "INTERCEPTOR"
    if n.endswith("Converter"):
        return "CONVERTER"
    if n.endswith("Validator"):
        return "VALIDATOR"
    if (n.endswith("Exception") or n.endswith("Config") or n.endswith("Configuration")
            or n.endswith("Factory") or n.endswith("Advice") or n.endswith("Message")
            or n.endswith("Properties") or n.endswith("Builder")
            or n.endswith("Request") or n.endswith("Response")
            or n.endswith("InputStream") or n.endswith("OutputStream")
            or n.endswith("Aspect") or n.endswith("Listener")
            or n.endswith("Handler") or n.endswith("Resolver")):
        return "INFRA"

    # ---- suspect: probably should be documented ----
    if n.endswith("Entity"):
        return "ENTITY_SUSPECT"
    if n.endswith("Interface") or n.endswith("Adapter"):
        return "EXTERNAL_INTERFACE"
    return "OTHER_SUSPECT"


def detect(project_id: str) -> dict:
    """Return all UNKNOWN-layer classes tagged + grouped, with mapping status.

    Shape:
      {
        "total_methods": N,
        "class_count": M,
        "by_tag":   {tag: count_of_classes, ...},
        "by_group": {"suspect": x, "ignorable": y},
        "classes": [
          {fqcn, simple_name, package, rel_path, method_count,
           tag, tag_label, group, group_label, mapped}, ...
        ]
      }
    """
    with get_conn() as c:
        rows = c.execute(
            """SELECT class_fqcn, COUNT(*) AS mc
               FROM java_method_semantic
               WHERE project_id=? AND layer='UNKNOWN'
               GROUP BY class_fqcn""",
            (project_id,),
        ).fetchall()

        # fqcn -> {simple_name, package, rel_path}  (first source_file hit)
        meta_rows = c.execute(
            """SELECT fqcn, simple_name, package, rel_path
               FROM source_file
               WHERE project_id=? AND ext='.java' AND fqcn IS NOT NULL""",
            (project_id,),
        ).fetchall()
        meta: dict[str, dict] = {}
        for r in meta_rows:
            meta.setdefault(r["fqcn"], dict(r))

        # set of fqcns that ARE mapped to at least one spec row
        mapped_rows = c.execute(
            """SELECT DISTINCT sf.fqcn
               FROM source_file sf
               JOIN mapping m ON m.source_file_id = sf.id
               WHERE sf.project_id=? AND sf.fqcn IS NOT NULL""",
            (project_id,),
        ).fetchall()
        mapped_fqcns = {r["fqcn"] for r in mapped_rows}

    classes: list[dict] = []
    total_methods = 0
    for r in rows:
        fqcn = r["class_fqcn"]
        mc = r["mc"]
        total_methods += mc
        m = meta.get(fqcn, {})
        simple = m.get("simple_name") or (fqcn.rsplit(".", 1)[-1] if fqcn else "?")
        tag = _classify(simple)
        group, tag_label = TAG_META[tag]
        classes.append({
            "fqcn": fqcn,
            "simple_name": simple,
            "package": m.get("package") or (fqcn.rsplit(".", 1)[0] if fqcn and "." in fqcn else ""),
            "rel_path": m.get("rel_path"),
            "method_count": mc,
            "tag": tag,
            "tag_label": tag_label,
            "group": group,
            "group_label": GROUP_LABEL[group],
            "mapped": fqcn in mapped_fqcns,
        })

    # suspect first, then by method_count desc
    classes.sort(key=lambda x: (0 if x["group"] == "suspect" else 1, -x["method_count"]))

    by_tag = Counter(x["tag"] for x in classes)
    by_group = Counter(x["group"] for x in classes)
    return {
        "total_methods": total_methods,
        "class_count": len(classes),
        "by_tag": dict(by_tag),
        "by_group": dict(by_group),
        "classes": classes,
    }

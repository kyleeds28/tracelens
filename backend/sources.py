import zipfile
import tarfile
import re
from pathlib import Path

EXT_TO_LANG = {
    ".java": "java",
    ".kt": "kotlin",
    ".py": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".vue": "vue",
}

_PACKAGE_RE = re.compile(r"^\s*package\s+([\w\.]+)\s*;", re.MULTILINE)


def extract_archive(archive_path: Path, dest_dir: Path) -> Path:
    """Extract zip/tar into dest_dir/<archive_stem>/ and return the extracted root."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    out_root = dest_dir / archive_path.stem
    out_root.mkdir(parents=True, exist_ok=True)
    name = archive_path.name.lower()
    if name.endswith(".zip"):
        with zipfile.ZipFile(archive_path, "r") as zf:
            zf.extractall(out_root)
    elif name.endswith((".tar", ".tar.gz", ".tgz", ".tar.bz2")):
        with tarfile.open(archive_path, "r:*") as tf:
            tf.extractall(out_root)
    else:
        raise ValueError(f"Unsupported archive: {archive_path}")
    return out_root


def _detect_java_package(text: str) -> str | None:
    m = _PACKAGE_RE.search(text)
    return m.group(1) if m else None


def walk_files(root: Path) -> list[dict]:
    """Walk extracted source root and produce file index entries."""
    files: list[dict] = []
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        ext = p.suffix.lower()
        if ext not in EXT_TO_LANG:
            continue
        lang = EXT_TO_LANG[ext]
        rel = p.relative_to(root).as_posix()
        simple_name = p.stem
        package = None
        fqcn = None
        if lang == "java":
            try:
                text = p.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                text = ""
            package = _detect_java_package(text)
            if package:
                fqcn = f"{package}.{simple_name}"
            else:
                fqcn = simple_name
        files.append(
            {
                "abs_path": str(p),
                "rel_path": rel,
                "ext": ext,
                "lang": lang,
                "package": package,
                "fqcn": fqcn,
                "simple_name": simple_name,
            }
        )
    return files

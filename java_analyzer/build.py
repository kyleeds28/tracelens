"""Bootstrap build for java_analyzer.

Downloads JavaParser+SymbolSolver and Guava jars from Maven Central into ./lib,
compiles JavaSemanticAnalyzer with javac into ./build/classes, and writes a
runnable manifest jar at ./build/java-analyzer.jar.

Requires JDK 17+ on PATH (or set JAVA_HOME).

Run once after checkout, or whenever deps change:
    python build.py
"""
from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
LIB = ROOT / "lib"
SRC = ROOT / "src"
BUILD = ROOT / "build"
CLASSES = BUILD / "classes"
JAR_OUT = BUILD / "java-analyzer.jar"


def jdk_tool(name: str) -> str:
    """Resolve a JDK tool (javac / jar / java) preferring JAVA_HOME over PATH.

    On Windows we may have multiple Java installs; PATH order is unreliable.
    """
    java_home = os.environ.get("JAVA_HOME")
    if java_home:
        exe = name + (".exe" if os.name == "nt" else "")
        cand = Path(java_home) / "bin" / exe
        if cand.exists():
            return str(cand)
    return name  # fall back to PATH

# (groupPath, artifact, version)  -- version pinned for reproducibility
DEPS = [
    ("com/github/javaparser", "javaparser-core", "3.26.2"),
    ("com/github/javaparser", "javaparser-symbol-solver-core", "3.26.2"),
    ("com/google/guava", "guava", "33.3.1-jre"),
    ("com/google/guava", "failureaccess", "1.0.2"),
]

MAVEN_CENTRAL = "https://repo1.maven.org/maven2"


def download_deps() -> list[Path]:
    LIB.mkdir(parents=True, exist_ok=True)
    jars: list[Path] = []
    for group, artifact, version in DEPS:
        fname = f"{artifact}-{version}.jar"
        dest = LIB / fname
        if not dest.exists():
            url = f"{MAVEN_CENTRAL}/{group}/{artifact}/{version}/{fname}"
            print(f"[download] {url}")
            with urllib.request.urlopen(url) as r, dest.open("wb") as f:
                shutil.copyfileobj(r, f)
        else:
            print(f"[cached]   {fname}")
        jars.append(dest)
    return jars


def find_sources() -> list[Path]:
    return list(SRC.rglob("*.java"))


def classpath(jars: list[Path]) -> str:
    sep = ";" if os.name == "nt" else ":"
    return sep.join(str(j) for j in jars)


def compile_sources(jars: list[Path], sources: list[Path]) -> None:
    if CLASSES.exists():
        shutil.rmtree(CLASSES)
    CLASSES.mkdir(parents=True, exist_ok=True)
    cmd = [
        jdk_tool("javac"), "-encoding", "UTF-8", "-d", str(CLASSES),
        "-cp", classpath(jars),
        "--release", "17",
    ] + [str(s) for s in sources]
    print(f"[javac]    compiling {len(sources)} source(s)")
    subprocess.run(cmd, check=True)


def write_manifest(jars: list[Path]) -> Path:
    """Minimal manifest. Classpath is supplied by the Python wrapper via -cp.

    Write in binary to avoid Windows text-mode CRLF doubling.
    """
    manifest = (
        b"Manifest-Version: 1.0\r\n"
        b"Main-Class: com.sourcemapping.JavaSemanticAnalyzer\r\n"
        b"\r\n"
    )
    mf_path = BUILD / "MANIFEST.MF"
    mf_path.write_bytes(manifest)
    return mf_path


def package_jar(jars: list[Path]) -> None:
    mf = write_manifest(jars)
    if JAR_OUT.exists():
        JAR_OUT.unlink()
    cmd = [jdk_tool("jar"), "cfm", str(JAR_OUT), str(mf), "-C", str(CLASSES), "."]
    print(f"[jar]      packaging {JAR_OUT.name}")
    subprocess.run(cmd, check=True)


def smoke_test(jars: list[Path]) -> None:
    """Run the jar with --help-like invocation (no args -> usage + exit 2)."""
    sep = ";" if os.name == "nt" else ":"
    full_cp = sep.join([str(JAR_OUT)] + [str(j) for j in jars])
    print("[smoke]    java -cp ... JavaSemanticAnalyzer  (expect exit 2)")
    r = subprocess.run(
        [jdk_tool("java"), "-cp", full_cp, "com.sourcemapping.JavaSemanticAnalyzer"],
        capture_output=True, text=True,
    )
    print("    stderr:", (r.stderr or "").strip())
    print("    exit:", r.returncode)


def main() -> None:
    jars = download_deps()
    sources = find_sources()
    if not sources:
        print(f"no sources under {SRC}", file=sys.stderr)
        sys.exit(2)
    compile_sources(jars, sources)
    package_jar(jars)
    smoke_test(jars)
    print(f"[done]     {JAR_OUT}")


if __name__ == "__main__":
    main()

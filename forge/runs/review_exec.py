"""Execution arm for the review — actually RUN the touched tests, so a review
verifies behavior instead of only reading the diff.

This is the capability a diff-only review structurally cannot have: a PR is not
green because its description says so. The arm copies the local clone to a scratch
directory, applies the change, finds the tests the change touches (the test files
in the diff, plus the sibling test of any changed source file), runs them, and
turns a real FAILURE into a high-confidence finding. It never runs in the clone
itself and deletes the scratch copy when done.

Best-effort by contract: when the runner or dependencies are not present (e.g. a
Node repo in a Python-only container), it reports *unavailable* rather than
inventing a pass or a fail — "we could not run it" and "it failed" are different
answers and only one of them blocks.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

WORK = os.environ.get("FORGE_WORK", "/work")

_TESTFILE = re.compile(
    r"(^|/)(test_[^/]+\.py|[^/]+_test\.py|[^/]+\.test\.[tj]sx?|[^/]+\.spec\.[tj]sx?)$")


def _clone(repo_name: str | None) -> Path | None:
    if not repo_name:
        return None
    d = Path(WORK) / repo_name
    return d if (d / ".git").is_dir() else (d if d.is_dir() else None)


def _is_test(path: str) -> bool:
    return bool(_TESTFILE.search(path))


def _sibling_tests(changed_files: list[str], root: Path) -> set[str]:
    """Test files that exist for the changed *source* files (so a change with no
    test file in the diff is still exercised by the suite that covers it)."""
    out: set[str] = set()
    for f in changed_files:
        if _is_test(f):
            continue
        base = f.rsplit("/", 1)[-1].rsplit(".", 1)[0]
        stem = f.rsplit(".", 1)[0]
        d = f.rsplit("/", 1)[0] if "/" in f else ""
        cands = ([f"{d}/test_{base}.py", f"tests/test_{base}.py"] if f.endswith(".py")
                 else [f"{stem}.test.ts", f"{stem}.spec.ts", f"{stem}.test.tsx"])
        for c in cands:
            if (root / c.lstrip("/")).exists():
                out.add(c.lstrip("/"))
    return out


def _prepare(repo_name: str, diff: str) -> dict:
    """Copy the clone to a scratch dir and apply the change. {dir} or {error}."""
    clone = _clone(repo_name)
    if not clone:
        return {"error": f"repo {repo_name!r} is not cloned under {WORK}"}
    scratch = Path(tempfile.mkdtemp(prefix="forge-exec-"))
    dst = scratch / repo_name
    try:
        shutil.copytree(clone, dst, ignore=shutil.ignore_patterns(
            "node_modules", ".venv", "__pycache__", ".pytest_cache"))
    except Exception as e:
        shutil.rmtree(scratch, ignore_errors=True)
        return {"error": f"could not stage a scratch copy: {e}"}
    for extra in ([], ["--3way"], ["-p1"]):
        p = subprocess.run(["git", "-C", str(dst), "apply", "--whitespace=nowarn", *extra, "-"],
                           input=diff, capture_output=True, text=True)
        if p.returncode == 0:
            return {"dir": str(dst), "scratch": str(scratch)}
    shutil.rmtree(scratch, ignore_errors=True)
    return {"error": f"could not apply the change to a clean base: {(p.stderr or '')[:160]}"}


def _run_pytest(dst: str, tests: list[str], timeout: int) -> tuple[int, str]:
    p = subprocess.run(
        ["python3", "-m", "pytest", "-q", "--no-header", "-p", "no:cacheprovider", *tests],
        cwd=dst, capture_output=True, text=True, timeout=timeout,
        env={**os.environ, "PYTHONPATH": dst})
    return p.returncode, (p.stdout + p.stderr)[-4000:]


def _have_node() -> bool:
    return shutil.which("node") is not None


def _link_node_modules(repo_name: str, dst: str) -> bool:
    """Symlink the repo's cached node_modules (from the /work clone, installed once
    via `npm ci`) into the scratch, so a TS test's imports resolve without a per-review
    install. Returns True if deps are available."""
    src = Path(WORK) / repo_name / "node_modules"
    if not src.is_dir():
        return False
    link = Path(dst) / "node_modules"
    if link.exists() or link.is_symlink():
        return True
    try:
        link.symlink_to(src)
        return True
    except Exception:
        return False


def _run_node(dst: str, tests: list[str], timeout: int) -> tuple[int, str]:
    # olaf runs its tests with tsx (`tsx --test`), which handles TypeScript directly.
    tsx = Path(dst) / "node_modules" / ".bin" / "tsx"
    runner = [str(tsx), "--test"] if tsx.exists() else ["node", "--test"]
    p = subprocess.run([*runner, *tests], cwd=dst, capture_output=True, text=True,
                       timeout=timeout, env={**os.environ, "NODE_OPTIONS": "--no-warnings"})
    return p.returncode, (p.stdout + p.stderr)[-4000:]


def run_tests(repo_name: str | None, diff: str, changed_files: list[str],
              timeout: int = 150) -> dict:
    """Run the touched tests on a scratch copy of the change. Returns
    {available, ran, seeds, note}. A real test failure is a high-confidence seed."""
    if not repo_name or not diff:
        return {"available": False, "why": "no repo/diff", "seeds": []}
    prep = _prepare(repo_name, diff)
    if prep.get("error"):
        return {"available": False, "why": prep["error"], "seeds": []}
    dst, scratch = prep["dir"], prep["scratch"]
    try:
        tests = sorted({f for f in changed_files if _is_test(f)}
                       | _sibling_tests(changed_files, Path(dst)))
        py = [t for t in tests if t.endswith(".py")]
        ts = [t for t in tests if not t.endswith(".py")]
        ran, seeds, notes = [], [], []
        if py:
            try:
                rc, out = _run_pytest(dst, py, timeout)
                ran += py
                if rc == 5:
                    notes.append("pytest collected no tests")
                elif rc != 0:
                    seeds.append({
                        "severity": "high", "file": py[0],
                        "detail": ("The touched tests FAIL when actually run (pytest "
                                   f"exit {rc}) — the change is not green. Output tail:\n"
                                   + out[-900:]),
                        "angle": "execution", "confidence": 95,
                        "verdict": "confirmed", "seed": True})
            except subprocess.TimeoutExpired:
                notes.append(f"pytest timed out after {timeout}s")
        if ts:
            if not _have_node():
                notes.append(f"{len(ts)} JS/TS test(s) not run — no node runtime here")
            elif not _link_node_modules(repo_name, dst):
                notes.append(f"{len(ts)} JS/TS test(s) not run — deps not cached "
                             f"(run `npm ci` in the {repo_name} clone once)")
            else:
                try:
                    rc, out = _run_node(dst, ts, timeout)
                    ran += ts
                    if rc != 0:
                        seeds.append({
                            "severity": "high", "file": ts[0],
                            "detail": ("The touched tests FAIL when actually run (tsx "
                                       f"--test exit {rc}) — the change is not green. "
                                       f"Output tail:\n" + out[-900:]),
                            "angle": "execution", "confidence": 90,
                            "verdict": "confirmed", "seed": True})
                except subprocess.TimeoutExpired:
                    notes.append(f"node tests timed out after {timeout}s")
        if seeds:
            note = f"{len(seeds)} touched test file(s) FAILED when run"
        elif notes:
            note = "; ".join(notes)
        elif ran:
            note = f"all {len(ran)} touched test file(s) passed"
        else:
            note = "no touched tests to run"
        return {"available": True, "ran": ran, "seeds": seeds, "note": note}
    finally:
        shutil.rmtree(scratch, ignore_errors=True)

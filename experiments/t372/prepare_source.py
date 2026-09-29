#!/usr/bin/env python3
"""Materialize the public pre-fix Bits task and a separate reference oracle.

Preparation only: this script copies files and checks hashes. It never imports
or executes Boltons or calls a model. Refuses to reuse an existing output dir.
"""

import argparse
import hashlib
import json
import shutil
import subprocess
import io
import os
import tarfile
from pathlib import Path

from sys import path as python_path

python_path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
from trusted_slot.safe_git import git_environment

FIX = "d0e36b4db07e5062b35ca664e0516ae0e4890b8b"
PARENT = "580a9c2d12755d472e534ca3e277e8f7e3ada49e"
WHEELS = {
    "pytest-8.3.5-py3-none-any.whl": "c69214aa47deac29fad6c2a4f590b9c4a9fdb16a403176fe154b79c0b4d4d820",
    "iniconfig-2.0.0-py3-none-any.whl": "b6a85871a79d2e3b22d2d1b94ac2824226a63c6b741c88f7ae975f18b6778374",
    "packaging-24.2-py3-none-any.whl": "09abb1bccd265c01f4a3aa3f7a7db064b36514d2cba19a2f694fe6150451a759",
    "pluggy-1.5.0-py3-none-any.whl": "44e1ad92c8ca002de6377e165f3e0f1be63266ab4d554740532335b9d75ea669",
}


def run(*args, cwd=None):
    if args[0] != "git":
        raise ValueError("only fixed Git is allowed")
    return subprocess.run(
        ["/usr/bin/git", "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false", *args[1:]],
        cwd=cwd, env=git_environment(), check=True, capture_output=True, text=True, timeout=120,
    ).stdout.strip()


def checkout_without_filters(repo, project, commit):
    # Controller receives only /project, so Git metadata must be self-contained.
    run("git", "clone", "--no-checkout", "--no-local", str(repo), str(project))
    run("git", "update-ref", "--no-deref", "HEAD", commit, cwd=project)
    run("git", "read-tree", commit, cwd=project)
    archive = subprocess.run(
        ["/usr/bin/git", "-c", "core.hooksPath=/dev/null", "archive", "--format=tar", commit],
        cwd=repo, env=git_environment(), check=True, capture_output=True, timeout=120,
    ).stdout
    with tarfile.open(fileobj=io.BytesIO(archive)) as bundle:
        for member in bundle:
            path = Path(member.name)
            if path.is_absolute() or ".." in path.parts or member.issym() or member.islnk():
                raise ValueError("unsafe upstream archive entry")
            destination = project / path
            if member.isdir():
                destination.mkdir(parents=True, exist_ok=True)
            elif member.isfile():
                destination.parent.mkdir(parents=True, exist_ok=True)
                source = bundle.extractfile(member)
                if source is None:
                    raise ValueError("archive file missing")
                destination.write_bytes(source.read())
                destination.chmod(member.mode & 0o777)
            else:
                raise ValueError("unsupported upstream archive entry")


def stage_raw(repo, relative):
    source = repo / relative
    result = subprocess.run(
        ["/usr/bin/git", "hash-object", "-w", "--no-filters", str(source)],
        cwd=repo, env=git_environment(), check=True, capture_output=True, text=True, timeout=15,
    )
    mode = "100755" if os.stat(source).st_mode & 0o111 else "100644"
    run("git", "update-index", "--add", "--cacheinfo", f"{mode},{result.stdout.strip()},{relative}", cwd=repo)


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--wheel-dir", type=Path, default=Path("/private/tmp/t363-boltons-candidate/wheels"))
    parser.add_argument("--remote", default="https://github.com/mahmoud/boltons.git")
    args = parser.parse_args()
    root = args.output.resolve()
    root.mkdir(parents=True, exist_ok=False)
    for filename, expected in WHEELS.items():
        if digest(args.wheel_dir / filename) != expected:
            raise ValueError("wheel pin mismatch: " + filename)
    repo = root / "upstream"
    if not (args.remote.startswith("https://") or Path(args.remote).is_absolute()):
        raise ValueError("remote must be HTTPS or an absolute local repository")
    run("git", "clone", "--no-checkout", args.remote, str(repo))
    run("git", "fetch", "origin", FIX, cwd=repo)
    if run("git", "rev-parse", FIX + "^", cwd=repo) != PARENT:
        raise ValueError("original fixing parent mismatch")
    changed = run("git", "diff-tree", "--no-commit-id", "--name-only", "-r", FIX, cwd=repo).splitlines()
    if changed != ["boltons/mathutils.py", "tests/test_mathutils.py"]:
        raise ValueError("fix changed unexpected files")
    target, reference = root / "target", root / "reference"
    checkout_without_filters(repo, target, PARENT)
    checkout_without_filters(repo, reference, FIX)
    shutil.copy2(reference / "tests/test_mathutils.py", target / "tests/test_mathutils.py")
    script = Path(__file__).with_name("verify_original_mathutils.sh")
    for project in (target, reference):
        wheels = project / "tests/t372_wheels"
        wheels.mkdir()
        for filename in WHEELS:
            shutil.copy2(args.wheel_dir / filename, wheels / filename)
        shutil.copy2(script, project / "tests/t372_verify_original_mathutils.sh")
    for relative in ["tests/test_mathutils.py", "tests/t372_verify_original_mathutils.sh"] + ["tests/t372_wheels/" + name for name in WHEELS]:
        stage_raw(target, relative)
    run("git", "-c", "user.name=T372 Fixture", "-c", "user.email=t372-fixture@example.invalid", "commit", "-m", "Original regression tests and offline pytest on pre-fix source", cwd=target)
    manifest = {
        "upstream": "https://github.com/mahmoud/boltons",
        "fix": FIX,
        "pre_fix_parent": PARENT,
        "target_tree": run("git", "rev-parse", "HEAD^{tree}", cwd=target),
        "target_commit": run("git", "rev-parse", "HEAD", cwd=target),
        "source_sha256": digest(target / "boltons/mathutils.py"),
        "original_regression_tests_sha256": digest(target / "tests/test_mathutils.py"),
        "reference_source_sha256": digest(reference / "boltons/mathutils.py"),
        "license_sha256": digest(target / "LICENSE"),
        "wheel_sha256": WHEELS,
        "target": str(target),
        "reference": str(reference),
    }
    (root / "source-manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    main()

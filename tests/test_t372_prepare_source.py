"""Input packaging must not depend on unrelated Git history or host gitdirs."""

import importlib.util
import shutil
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "t372_prepare_source",
    Path(__file__).resolve().parents[1] / "experiments/t372/prepare_source.py",
)
prepare = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(prepare)


def commit(repo, content):
    (repo / "file.txt").write_text(content)
    prepare.stage_raw(repo, "file.txt")
    prepare.run(
        "git",
        "-c",
        "user.name=Fixture",
        "-c",
        "user.email=fixture@example.invalid",
        "commit",
        "-qm",
        "fixture",
        cwd=repo,
    )
    return prepare.run("git", "rev-parse", "HEAD", cwd=repo)


def test_snapshot_ignores_missing_old_blob_and_survives_source_removal(tmp_path):
    repo = tmp_path / "upstream"
    prepare.run("git", "init", str(repo))
    commit(repo, "unneeded history\n")
    missing = prepare.run("git", "rev-parse", "HEAD:file.txt", cwd=repo)
    wanted = commit(repo, "task input\n")
    future = commit(repo, "reference repair must not reach task\n")
    (repo / ".git/objects" / missing[:2] / missing[2:]).unlink()
    staging = tmp_path / "staging"
    prepare.fetch_commit(repo, staging, future, depth=2)
    assert prepare.run("git", "rev-parse", future + "^", cwd=staging) == wanted
    project = tmp_path / "project"
    prepare.checkout_without_filters(staging, project, wanted)
    shutil.rmtree(repo)
    shutil.rmtree(staging)
    assert (project / ".git").is_dir()
    assert not (project / ".git/objects/info/alternates").exists()
    assert (project / "file.txt").read_text() == "task input\n"
    assert prepare.run("git", "rev-parse", "HEAD", cwd=project) == wanted
    assert prepare.run("git", "rev-list", "--count", "HEAD", cwd=project) == "1"
    prepare.run("git", "fsck", "--full", cwd=project)
    with pytest.raises(RuntimeError):
        prepare.run("git", "cat-file", "-e", future, cwd=project)


def test_git_failure_surfaces_stderr_and_exit_code(tmp_path):
    with pytest.raises(RuntimeError) as caught:
        prepare.run("git", "clone", str(tmp_path / "missing"), str(tmp_path / "out"))
    assert "128" in str(caught.value)
    assert "does not exist" in str(caught.value)

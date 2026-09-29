"""Read-only Git input checks use harmless, disposable repositories."""

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from trusted_slot.safe_git import clean_tree


def git(repo, *args):
    return (
        subprocess.check_output(["/usr/bin/git", "-C", str(repo), *args])
        .decode()
        .strip()
    )


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    git(root, "init", "-q")
    (root / "file.txt").write_text("original\n")
    git(root, "add", "file.txt")
    git(
        root,
        "-c",
        "user.name=Fixture",
        "-c",
        "user.email=fixture@example.invalid",
        "commit",
        "-qm",
        "fixture",
    )
    return root


def test_clean_tree_rejects_changed_tracked_and_ignored_untracked(project):
    tree = git(project, "rev-parse", "HEAD^{tree}")
    assert clean_tree(project) == tree
    (project / "file.txt").write_text("modified\n")
    with pytest.raises(ValueError):
        clean_tree(project)
    (project / "file.txt").write_text("original\n")
    (project / ".gitignore").write_text("ignored\n")
    (project / "ignored").write_text("hidden payload\n")
    with pytest.raises(ValueError):
        clean_tree(project)


def test_foreign_git_environment_and_fsmonitor_never_run(
    project, tmp_path, monkeypatch
):
    expected = git(project, "rev-parse", "HEAD^{tree}")
    other = tmp_path / "other"
    other.mkdir()
    git(other, "init", "-q")
    marker = tmp_path / "executed"
    global_config = tmp_path / "global.gitconfig"
    global_config.write_text(f"[core]\n\tfsmonitor = sh -c 'touch {marker}'\n")
    git(project, "config", "core.fsmonitor", f"sh -c 'touch {marker}'")
    monkeypatch.setenv("GIT_DIR", str(other / ".git"))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(global_config))
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.fsmonitor")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", f"sh -c 'touch {marker}'")
    assert clean_tree(project) == expected
    assert not marker.exists()

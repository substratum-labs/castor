"""Read-only HEAD/index/worktree binding before the native pack repeats validation."""

import hashlib
import os
import stat
import subprocess
from pathlib import Path


def git_environment():
    # No caller Git identity, config injection, executable search path, or lazy fetch.
    return {
        "PATH": "/usr/bin:/bin",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_SYSTEM": "/dev/null",
        "GIT_NO_REPLACE_OBJECTS": "1",
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_NO_LAZY_FETCH": "1",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_ALLOW_PROTOCOL": "https:file",
        "GIT_TEMPLATE_DIR": "/dev/null",
    }


def git_read(root, git_dir, *arguments):
    command = [
        "/usr/bin/git",
        f"--work-tree={root}",
        f"--git-dir={git_dir}",
        "-c", f"safe.directory={root}",
        "-c", "core.fsmonitor=false",
        "-c", "core.hooksPath=/dev/null",
        *arguments,
    ]
    result = subprocess.run(
        command, env=git_environment(), capture_output=True, check=True, timeout=15
    )
    if len(result.stdout) > 16 * 1024 * 1024:
        raise ValueError("Git metadata exceeds cap")
    return result.stdout


def _git_dir(root):
    dotgit = root / ".git"
    if dotgit.is_symlink():
        raise ValueError("symlink .git is unsupported")
    if dotgit.is_dir():
        return dotgit.resolve(strict=True)
    if not dotgit.is_file():
        raise ValueError("missing Git directory")
    text = dotgit.read_bytes()
    if len(text) > 4096 or not text.startswith(b"gitdir: "):
        raise ValueError("invalid gitdir file")
    target = os.fsdecode(text.removeprefix(b"gitdir: ").strip())
    git_dir = Path(target)
    if not git_dir.is_absolute():
        git_dir = root / git_dir
    if git_dir.is_symlink() or not git_dir.is_dir():
        raise ValueError("invalid gitdir target")
    return git_dir.resolve(strict=True)


def _entries(data, tree):
    entries = {}
    for record in data.split(b"\0"):
        if not record:
            continue
        header, path = record.split(b"\t", 1)
        parts = header.split()
        if tree:
            mode, kind, oid = parts
            if kind != b"blob":
                raise ValueError("non-blob target entry")
        else:
            mode, oid, stage = parts
            if stage != b"0":
                raise ValueError("unmerged target index")
        if mode not in (b"100644", b"100755", b"120000") or path in entries:
            raise ValueError("unsupported target entry")
        entries[path] = (mode, oid)
    return entries


def clean_tree(project):
    """Return HEAD tree only when all index and filesystem bytes match HEAD."""
    root = Path(project).resolve(strict=True)
    if not root.is_dir():
        raise ValueError("project must be a directory")
    git_dir = _git_dir(root)
    tree_id = git_read(root, git_dir, "rev-parse", "HEAD^{tree}").decode().strip()
    if len(tree_id) not in (40, 64) or any(c not in "0123456789abcdef" for c in tree_id):
        raise ValueError("invalid target tree ID")
    head = _entries(git_read(root, git_dir, "ls-tree", "-rz", "--full-tree", "HEAD"), True)
    index = _entries(git_read(root, git_dir, "ls-files", "--stage", "-z"), False)
    if head != index:
        raise ValueError("target index differs from HEAD")
    seen = set()
    stack = [root]
    while stack:
        directory = stack.pop()
        for entry in directory.iterdir():
            if directory == root and entry.name == ".git":
                continue
            if entry.is_dir() and not entry.is_symlink():
                stack.append(entry)
                continue
            relative = os.fsencode(str(entry.relative_to(root)))
            if relative not in head:
                raise ValueError("untracked project file")
            mode, oid = head[relative]
            info = entry.lstat()
            if stat.S_ISLNK(info.st_mode):
                actual_mode = b"120000"
                content = os.fsencode(os.readlink(entry))
            elif stat.S_ISREG(info.st_mode):
                actual_mode = b"100755" if info.st_mode & 0o111 else b"100644"
                if info.st_size > 32 * 1024 * 1024:
                    raise ValueError("target blob exceeds cap")
                content = entry.read_bytes()
            else:
                raise ValueError("unsupported target file")
            algorithm = hashlib.sha1 if len(oid) == 40 else hashlib.sha256
            blob = algorithm(b"blob " + str(len(content)).encode() + b"\0" + content).hexdigest().encode()
            if (actual_mode, blob) != (mode, oid):
                raise ValueError(f"target bytes or mode differ from HEAD: {os.fsdecode(relative)} {actual_mode!r}/{mode!r} {blob!r}/{oid!r}")
            seen.add(relative)
    if seen != head.keys():
        raise ValueError("tracked target file missing")
    return tree_id

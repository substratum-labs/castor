"""Trusted Engine filesystem administration; no target code execution."""

import json
import os
import re
import shutil
import sys
from pathlib import Path


def main():
    request = json.loads(sys.argv[1])
    os.chroot("/engine")
    os.chdir("/")
    mode = request["operation"]
    if mode == "canonical":
        print(json.dumps([os.path.realpath(p) for p in request["paths"]]))
        return
    parent, path, token = request["parent"], request["scratch"], request["token"]
    if (
        not re.fullmatch(r"[0-9a-f]{32}", token)
        or path != parent.rstrip("/") + "/c-" + token[:12]
        or os.path.realpath(parent) != parent
        or os.path.realpath(path) != path
    ):
        raise ValueError("private scratch identity/canonicalization mismatch")
    root = Path(path)
    if mode == "allocate":
        root.mkdir(mode=0o700)  # exclusive; never adopt an existing directory
        (root / ".launcher-owner").write_text(token)
    elif mode == "remove":
        if (root / ".launcher-owner").read_text() != token:
            raise ValueError("private scratch owner mismatch")
        shutil.rmtree(root)
    else:
        raise ValueError("unknown management operation")
    print(json.dumps({"scratch": path, "exists": os.path.lexists(path)}))


if __name__ == "__main__":
    main()

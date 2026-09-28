"""Trusted fixture administration only; never reads or executes target code.

Run in the pinned controller image with / mounted RO at /engine and /run
mounted RW at /engine/run. chroot gives real Engine symlink semantics.
"""

import json
import os
import re
import shutil
import socket
import stat
import sys
import time
from pathlib import Path


def checked_cleanup_path(base, value):
    path = Path(value)
    if path != base and not path.is_relative_to(base / "s"):
        raise ValueError("cleanup outside private scratch")
    if ".." in path.parts or os.path.realpath(value) != value:
        raise ValueError("cleanup alias rejected")
    return path


def main():
    root, token, operation, value = sys.argv[1:]
    if not re.fullmatch(r"/run/t371-[0-9a-f]{12}", root):
        raise ValueError("not a private fixture root")
    if root != "/run/t371-" + token:
        raise ValueError("fixture token mismatch")
    os.chroot("/engine")
    os.chdir("/")
    base = Path(root)
    if operation == "serve":
        base.mkdir(mode=0o700)  # exclusive creation; never reuse another run
        (base / ".owner").write_text(token)
        (base / "s").mkdir()
        (base / "outsider").mkdir()
        (base / "docker-root-link").symlink_to(value)
        listener = socket.socket(socket.AF_UNIX)
        listener.bind(str(base / "foreign.sock"))
        listener.listen()
        while True:
            time.sleep(1)
    if base.is_symlink() or (base / ".owner").read_text() != token:
        raise ValueError("fixture ownership marker missing")
    if operation == "canonical":
        print(json.dumps(os.path.realpath(value)))
    elif operation == "stat":
        exists = os.path.lexists(value)
        print(
            json.dumps(
                {
                    "path": value,
                    "exists": exists,
                    "socket": exists and stat.S_ISSOCK(os.lstat(value).st_mode),
                }
            )
        )
    elif operation == "remove":
        path = checked_cleanup_path(base, value)
        if path.exists():
            shutil.rmtree(path)
        print(json.dumps({"path": value, "absent": not os.path.lexists(value)}))
    else:
        raise ValueError("unknown fixture operation")


if __name__ == "__main__":
    main()

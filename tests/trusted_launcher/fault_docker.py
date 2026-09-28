"""Management fault transport: only the armed, exact controller CID is blocked."""

import json
import os
import re
import sys
from pathlib import Path


def blocked_cids(args, target):
    offset = 1 if args[:1] == ["rm"] else 2 if args[:2] == ["container", "rm"] else 0
    if not offset or not re.fullmatch(r"[0-9a-f]{64}", target):
        return []
    operands = [arg for arg in args[offset:] if not arg.startswith("-")]
    return [target] if target in operands else []


def main():
    config = json.loads(Path(sys.argv[1]).read_text())
    args = sys.argv[2:]
    target_file = Path(config["target_file"])
    target = target_file.read_text().strip() if target_file.exists() else ""
    blocked = blocked_cids(args, target)
    if blocked:
        with open(config["log_file"], "a") as stream:
            stream.write(json.dumps({"argv": args, "blocked_cids": blocked}) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        print("Injected controller removal transport failure", file=sys.stderr)
        sys.exit(1)
    docker = config["docker"]
    if not os.path.isabs(docker):
        raise ValueError("Docker delegation must be absolute")
    os.execv(docker, [docker, *args])


if __name__ == "__main__":
    main()

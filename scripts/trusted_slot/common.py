"""Management data validation; no product authority or target execution."""

import json
import os
import re
from pathlib import Path, PurePosixPath

CONTROLLER = "sha256:39fad3ec792c6d55d6049f010520ebdc662c92c9eb8f6b74872b7842e073ba80"
PI = "sha256:b4f363b3436653157b87428ec6d4936b9efa26c184a77d346345071d02edddb7"
VERIFIER = (
    "python:3.12-slim@sha256:"
    "78387bc3881b8273120a12ebe6c1ab22b018ccc2c9adf565ae1ac9b536e184ea"
)
# Runtime lookup is explicit; the historical protocol/receipt pin stays stable.
VERIFIER_RUNTIME = "docker.io/library/" + VERIFIER


def cid(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise ValueError("cleanup requires a full recorded container ID")
    return value


def below(path, parent):
    value, root = PurePosixPath(path), PurePosixPath(parent)
    return value.is_absolute() and ".." not in value.parts and root in value.parents


def overlap(left, right):
    return left == right or below(left, right) or below(right, left)


def write_bytes(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".writing")
    with temporary.open("wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)
    descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def write_json(path, value):
    write_bytes(path, (json.dumps(value, indent=2) + "\n").encode())


def append_json(path, value):
    with Path(path).open("a") as stream:
        stream.write(json.dumps(value) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def native_result(raw, board_result):
    result = json.loads(raw) if raw.strip() else board_result
    if result is None:
        return None, "FAILED"
    if not isinstance(result, dict) or not isinstance(result.get("status"), str):
        raise ValueError("invalid native product result")
    if board_result is not None and result != board_result:
        raise ValueError("native stdout and board disagree")
    return result, "SUCCEEDED" if result["status"] == "SUCCEEDED" else "FAILED"


def child_profile(item, scratch, verifier_image):
    """Private mount AND native profile; never a label/name sweep."""
    try:
        host, config, mounts = item["HostConfig"], item["Config"], item["Mounts"]
        if len(mounts) != 1:
            return None
        mount = mounts[0]
        if not (
            mount["Type"] == "bind"
            and not mount["RW"]
            and below(mount["Source"], scratch)
        ):
            return None
        if not (
            host["NetworkMode"] == "none"
            and host["ReadonlyRootfs"]
            and config["User"] == "10001:10001"
            and not host["Privileged"]
            and host["CapDrop"] == ["ALL"]
            and not host["CapAdd"]
            and host["PidsLimit"] == 256
            and "no-new-privileges" in host["SecurityOpt"]
        ):
            return None
        if (
            mount["Destination"] == "/run/castor/ipc.sock"
            and mount["Source"].endswith("/ipc.sock")
            and host["Memory"] == 536870912
            and host["NanoCpus"] == 1000000000
            and "CASTOR_IPC_SOCKET=/run/castor/ipc.sock" in config["Env"]
            and "exec pi --extension /opt/castor/castor-pi-extension.js"
            in " ".join(config["Cmd"])
        ):
            return "pi"
        if (
            mount["Destination"] == "/candidate"
            and mount["Propagation"] == "rprivate"
            and item["Image"] == verifier_image
            and config["Image"] in (VERIFIER, VERIFIER_RUNTIME)
            and host["Memory"] == host["MemorySwap"] == 1073741824
            and host["NanoCpus"] == 2000000000
            and host["LogConfig"]["Type"] == "none"
            and host["IpcMode"] == "private"
            and host["PidMode"] in ("", "private")
            and config["WorkingDir"] == "/workspace"
            and not config["Entrypoint"]
            and item["Name"]
            == "/castor-verifier-" + config["Labels"]["castor.verifier.owner"]
            and config["Cmd"][:2] == ["/bin/sh", "-c"]
            and config["Cmd"][3] == "castor-verifier"
            and set(host["Tmpfs"]) == {"/workspace", "/root", "/dev/shm"}
            and not any(
                host.get(k)
                for k in (
                    "Binds",
                    "Devices",
                    "DeviceRequests",
                    "VolumesFrom",
                    "ExtraHosts",
                    "Links",
                )
            )
        ):
            return "verifier"
    except (KeyError, TypeError, IndexError):
        return None
    return None

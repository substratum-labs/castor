"""Finite, CID-scoped Docker lifecycle for trusted management containers."""

import json
import subprocess
import time
import uuid
from pathlib import Path

from .common import (
    CONTROLLER,
    PI,
    VERIFIER_RUNTIME,
    append_json,
    below,
    child_profile,
    cid,
    native_result,
    overlap,
    write_bytes,
    write_json,
)


class DockerError(RuntimeError):
    pass


class Docker:
    def __init__(self, binary):
        self.binary = binary
        self.deadline = None

    def call(self, *args, check=True):
        timeout = 15.0
        if self.deadline is not None:
            timeout = min(timeout, self.deadline - time.monotonic())
            if timeout <= 0:
                raise TimeoutError("Docker lifecycle deadline exhausted")
        result = subprocess.run(
            [self.binary, *args], capture_output=True, timeout=timeout
        )
        if check and result.returncode:
            raise DockerError(
                f"docker {args[0]} failed: {result.stderr.decode(errors='replace')}"
            )
        return result

    def inspect(self, identifier):
        result = self.call("inspect", cid(identifier), check=False)
        if result.returncode:
            message = result.stderr.decode(errors="replace").lower()
            if "no such object" in message or "no such container" in message:
                self.call("info", "--format", "{{.DockerRootDir}}")
                return None
            raise DockerError(message)
        item = json.loads(result.stdout)[0]
        if item["Id"] != identifier:
            raise DockerError("inspect identity mismatch")
        return item

    def inventory(self):
        identifiers = self.call("ps", "-aq", "--no-trunc").stdout.decode().split()
        # Product children can remove themselves between ps and inspect.
        return [
            item
            for identifier in identifiers
            if (item := self.inspect(identifier)) is not None
        ]


class Slot:
    def __init__(self, args, started):
        self.args, self.started = args, started
        self.dk = Docker(args.docker_bin)
        self.state = Path(args.state_root).resolve()
        self.evidence = self.state / "launcher"
        self.evidence.mkdir(parents=True, exist_ok=False)
        self.token = uuid.uuid4().hex
        self.controller = None
        self.children = {}
        self.admin = []
        self.scratch = None
        self.allocated = False
        self.mapping = {}
        self.verifier_id = None
        self.status, self.result = "FAILED", None
        self.cleanup_errors = []
        self.scripts = Path(__file__).resolve().parent
        self.work_deadline = started + args.workload_timeout
        self.dk.deadline = self.work_deadline
        self.host_exchange = None

    def event(self, identifier, event, **fields):
        append_json(
            self.evidence / "lifecycle.jsonl",
            {
                "cid": cid(identifier),
                "event": event,
                "elapsed_seconds": time.monotonic() - self.started,
                **fields,
            },
        )

    def create(self, arguments, name):
        file = self.evidence / f"{name}.cid"
        result = None
        try:
            result = self.dk.call(
                "create",
                "--pull=never",
                "--cidfile",
                str(file),
                *arguments,
                check=False,
            )
        finally:
            if file.exists():
                identifier = cid(file.read_text().strip())
                if name == "controller":
                    self.controller = identifier
                    self.event(identifier, "create")
                else:
                    self.admin.append(identifier)
                    write_json(self.evidence / "admin-cids.json", self.admin)
        if result is None or result.returncode or not file.exists():
            raise DockerError(
                "create failed: "
                + (result.stderr.decode(errors="replace") if result else name)
            )
        if result.stdout.decode().strip() != identifier:
            raise DockerError("create stdout disagrees with CID file")
        return identifier

    def helper(self, request, parent=None):
        name = "admin-" + uuid.uuid4().hex[:8]
        arguments = [
            "--network",
            "none",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--cap-add",
            "SYS_CHROOT",
            "--env",
            "PYTHONDONTWRITEBYTECODE=1",
            "--mount",
            "type=bind,src=/,dst=/engine,readonly",
            "--mount",
            f"type=bind,src={self.scripts},dst=/opt/t371,readonly",
            "--mount",
            f"type=bind,src={self.state},dst=/state",
        ]
        if parent:
            arguments += ["--mount", f"type=bind,src={parent},dst=/engine{parent}"]
        arguments += [CONTROLLER, "python3", "/opt/t371/engine.py", json.dumps(request)]
        identifier = self.create(arguments, name)
        try:
            inspected = self.dk.inspect(identifier)
            write_json(self.evidence / f"{name}.before.json", inspected)
            output = self.dk.call("start", "-a", identifier, check=False)
            waited = self.dk.call("wait", identifier).stdout.strip()
            terminal = self.dk.inspect(identifier)
            write_json(self.evidence / f"{name}.inspect.json", terminal)
            write_bytes(self.evidence / f"{name}.stdout", output.stdout)
            write_bytes(self.evidence / f"{name}.stderr", output.stderr)
            if output.returncode or waited != b"0" or terminal["State"]["Running"]:
                raise DockerError(
                    "trusted Engine helper failed: "
                    + output.stderr.decode(errors="replace")
                )
            return json.loads(output.stdout), inspected
        finally:
            self.dk.call("rm", "-f", identifier)
            if self.dk.inspect(identifier) is not None:
                raise DockerError("management helper remained after removal")

    def preflight(self):
        if getattr(self.args, "model_mode", "mock") == "ollama":
            from .real_model import validate_model_pin

            pin = validate_model_pin()
            protocol = self.args.protocol
            if pin != {
                "name": protocol["model"],
                "digest": protocol["model_digest"],
                "version": protocol["ollama_version"],
            }:
                raise ValueError("actual Ollama runtime metadata differs from protocol")
            write_json(self.evidence / "ollama-pin.json", pin)
        if self.args.trusted_controller_image != CONTROLLER:
            raise ValueError("trusted controller must match the accepted fixed pin")
        for reference, expected in (
            (CONTROLLER, CONTROLLER),
            ("substratum/castor-pi-carrier:v1", PI),
        ):
            actual = (
                self.dk.call("image", "inspect", "--format", "{{.Id}}", reference)
                .stdout.decode()
                .strip()
            )
            if actual != expected:
                raise ValueError("local image pin mismatch: " + reference)
        self.verifier_id = (
            self.dk.call("image", "inspect", "--format", "{{.Id}}", VERIFIER_RUNTIME)
            .stdout.decode()
            .strip()
        )
        if getattr(self.args, "model_mode", "mock") == "ollama" and self.verifier_id != self.args.protocol["verifier_image"]:
            raise ValueError("actual verifier image differs from protocol")
        for name in ("castor", "castord"):
            with (Path(self.args.linux_bin_dir) / name).open("rb") as stream:
                if stream.read(4) != b"\x7fELF":
                    raise ValueError("actual Linux native binaries required")
        root = (
            self.dk.call("info", "--format", "{{.DockerRootDir}}")
            .stdout.decode()
            .strip()
        )
        canonical, inspected = self.helper(
            {"operation": "canonical", "paths": [root, self.args.scratch_parent]}
        )
        self.docker_root, self.parent = canonical
        preflight = {
            "docker_root": self.docker_root,
            "requested_scratch_parent": self.args.scratch_parent,
            "canonical_scratch_parent": self.parent,
        }
        if overlap(self.parent, self.docker_root):
            preflight["rejection"] = "DOCKER_ROOT_OVERLAP"
            write_json(self.evidence / "preflight.json", preflight)
            raise ValueError("DockerRootDir overlap rejected")
        self.scratch = self.parent.rstrip("/") + "/c-" + self.token[:12]
        state_source = next(
            m["Source"] for m in inspected["Mounts"] if m["Destination"] == "/state"
        )
        canonical_state, _ = self.helper(
            {"operation": "canonical", "paths": [state_source]}
        )
        state_source = canonical_state[0]
        if any(
            overlap(state_source, path)
            for path in (self.scratch, self.docker_root, "/run")
        ):
            raise ValueError(
                "persistent state overlaps scratch, /run, or DockerRootDir"
            )
        if len((self.scratch + "/castor-pi-gateway-XXXXXX/ipc.sock").encode()) > 107:
            raise ValueError("scratch path exceeds native Linux UDS path budget")
        self.mapping = {
            "docker_root": self.docker_root,
            "scratch": self.scratch,
            "state_host": str(self.state),
            "state_source": state_source,
            "state_destination": "/state",
        }
        write_json(self.evidence / "preflight.json", preflight)
        self.allocated = True  # ambiguous helper failure must retain, never claim clean
        write_json(
            self.evidence / "scratch-owner.json",
            {"scratch": self.scratch, "token": self.token},
        )
        allocation, _ = self.helper(
            {
                "operation": "allocate",
                "parent": self.parent,
                "scratch": self.scratch,
                "token": self.token,
            },
            self.parent,
        )
        self.allocated = True
        if not allocation["exists"]:
            raise DockerError("private scratch allocation absent")
        write_json(
            self.evidence / "scratch-owner.json",
            {"scratch": self.scratch, "token": self.token},
        )

    def start(self):
        spec = json.loads(Path(self.args.task_spec).read_bytes())
        spec["task_id"], spec["idempotency_key"] = (
            self.args.task_id,
            self.args.idempotency_key,
        )
        write_json(self.evidence / "task-spec.json", spec)
        mode = getattr(self.args, "model_mode", "mock")
        bridged = mode in ("ollama", "file_bridge_fixture")
        write_json(self.evidence / "controller-config.json", {"model_mode": mode, "mock_mode": self.args.mock_mode, "deadline_seconds": max(0.001, self.work_deadline - time.monotonic())})
        if bridged:
            from .real_model import HostExchange

            self.host_exchange = HostExchange(self.evidence, max(0.001, self.work_deadline - time.monotonic()), fake=mode == "file_bridge_fixture")
            self.host_exchange.start()
        arguments = [
            "--network",
            "none",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--tmpfs",
            "/tmp:rw,nosuid,nodev,size=64m",
            "--env",
            "PYTHONDONTWRITEBYTECODE=1",
            "--env",
            "HOME=/tmp",
            "--env",
            "CASTOR_STATE_ROOT=/state",
            "--env",
            "TMPDIR=" + self.scratch,
            "--env",
            "CASTOR_MODEL_SOCKET=" + self.scratch + "/model.sock",
            "--mount",
            "type=bind,src=/var/run/docker.sock,dst=/var/run/docker.sock",
            "--mount",
            f"type=bind,src={self.scratch},dst={self.scratch}",
            "--mount",
            f"type=bind,src={self.state},dst=/state",
            "--mount",
            f"type=bind,src={Path(self.args.project).resolve()},dst=/project,readonly",
            "--mount",
            f"type=bind,src={Path(self.args.linux_bin_dir).resolve()},dst=/native,readonly",
            "--mount",
            f"type=bind,src={self.scripts},dst=/opt/t371,readonly",
            CONTROLLER,
            "python3",
            "/opt/t371/controller.py",
        ]
        identifier = self.create(arguments, "controller")
        item = self.dk.inspect(identifier)
        write_json(self.evidence / "controller-created.json", item)
        scratch_mount = next(
            m for m in item["Mounts"] if m["Destination"] == self.scratch
        )
        if scratch_mount["Source"] != self.scratch:
            raise ValueError("controller scratch is not a same-path Engine bind")
        state_mount = next(m for m in item["Mounts"] if m["Destination"] == "/state")
        if state_mount["Source"] != self.mapping["state_source"]:
            raise ValueError(
                "controller state mount differs from inspected preflight binding"
            )
        write_json(self.evidence / "mount-map.json", self.mapping)
        self.dk.call("start", identifier)
        self.event(identifier, "start")

    def discover(self):
        if not self.scratch:
            return []
        found = []
        for item in self.dk.inventory():
            if item["Id"] == self.controller or item["Id"] in self.admin:
                continue
            private = any(below(m["Source"], self.scratch) for m in item["Mounts"])
            if not private:
                continue
            profile = child_profile(item, self.scratch, self.verifier_id)
            if profile is None:
                raise DockerError(
                    "unrecognized private mount; refusing broad cleanup: " + item["Id"]
                )
            identifier = cid(item["Id"])
            previous = self.children.get(identifier, {})
            running_recorded = previous.get("running_recorded", False)
            if item["State"]["Running"] and not running_recorded:
                self.event(identifier, "observe_running")
                running_recorded = True
            self.children[identifier] = {
                "kind": profile,
                "inspect": item,
                "running_recorded": running_recorded,
            }
            found.append(item)
        write_json(self.evidence / "owned-inspects.json", self.children)
        return found

    def run_workload(self):
        while time.monotonic() < self.work_deadline:
            if self.host_exchange:
                self.host_exchange.check()
            self.discover()
            item = self.dk.inspect(self.controller)
            if item is None:
                raise DockerError("controller disappeared before archival")
            if not item["State"]["Running"]:
                return
            time.sleep(0.15)
        self.status = "TIMEOUT"

    def kill(self, identifier):
        item = self.dk.inspect(identifier)
        if item is not None and item["State"]["Running"]:
            self.dk.call("kill", "--signal", "SIGKILL", identifier)
            self.event(identifier, "kill", signal="SIGKILL")

    def archive_remove(self, identifier, controller=False):
        item = self.dk.inspect(identifier)
        if item is None:
            if controller:
                raise DockerError("controller absent before terminal archive")
            return  # product already reaped it; native evidence remains authoritative
        self.dk.call("wait", identifier)
        self.event(identifier, "wait")
        terminal = self.dk.inspect(identifier)
        if (
            terminal is None
            or terminal["State"]["Running"]
            or terminal["State"]["Status"] != "exited"
        ):
            raise DockerError("container lacks a terminal inspect")
        prefix = self.evidence / (
            "controller" if controller else f"children/{identifier}"
        )
        inspect_file = (
            self.evidence / "controller-inspect.json"
            if controller
            else prefix.with_suffix(".inspect.json")
        )
        write_json(inspect_file, terminal)
        self.event(identifier, "inspect")
        logs = self.dk.call("logs", identifier)
        write_bytes(prefix.with_suffix(".stdout"), logs.stdout)
        write_bytes(prefix.with_suffix(".stderr"), logs.stderr)
        if controller:
            board = self.state / "board.json"
            board_result = None
            if board.is_file():
                board_result = (
                    json.loads(board.read_bytes())
                    .get(self.args.idempotency_key, {})
                    .get("result")
                )
            self.result, native_status = native_result(logs.stdout, board_result)
            if self.result is not None:
                raw = (
                    logs.stdout
                    if logs.stdout.strip()
                    else (json.dumps(self.result) + "\n").encode()
                )
                write_bytes(self.evidence / "task-result.json", raw)
                profile = self.result.get("verifier_evidence", {}).get(
                    "inspected_profile"
                )
                if profile:
                    candidate = profile["candidate_source"]
                    if not below(candidate, self.scratch):
                        raise ValueError("native candidate outside private scratch")
                    self.mapping["candidate"] = candidate
                    write_json(self.evidence / "mount-map.json", self.mapping)
            if self.status != "TIMEOUT":
                self.status = native_status
        self.event(identifier, "archive")
        result = self.dk.call("rm", "-f", identifier, check=False)
        if result.returncode:
            self.event(
                identifier,
                "remove_failed",
                stderr=result.stderr.decode(errors="replace"),
            )
            raise DockerError("container removal transport failure: " + identifier)
        if self.dk.inspect(identifier) is not None:
            raise DockerError("container remained after removal: " + identifier)
        self.event(identifier, "remove")

    def finish(self):
        self.dk.deadline = time.monotonic() + self.args.cleanup_timeout
        if self.host_exchange:
            try:
                self.host_exchange.close()
            except Exception as error:
                self.cleanup_errors.append(str(error))
        if self.controller:
            try:
                live = self.discover()
                if self.status == "TIMEOUT":
                    running = [
                        i
                        for i in live
                        if self.children[i["Id"]]["kind"] == "pi"
                        and i["State"]["Running"]
                    ]
                    if running:
                        write_json(
                            self.evidence / "owned-child-before-kill.json", running[0]
                        )
                # Freeze only inspected owned children. Otherwise closing the
                # controller IPC lets Pi exit 0 before our required SIGKILL.
                # Docker can deliver SIGKILL to a paused task without unpausing.
                for item in live:
                    if item["State"]["Running"] and not item["State"]["Paused"]:
                        self.dk.call("pause", item["Id"])
                        self.event(item["Id"], "pause")
                # Stop the creator first; its native reaper cannot race archival.
                self.kill(self.controller)
                self.discover()
                for identifier in self.children:
                    self.kill(identifier)
                for identifier in self.children:
                    self.archive_remove(identifier)
                self.archive_remove(self.controller, controller=True)
            except Exception as error:
                self.cleanup_errors.append(str(error))
        # Recover only exact administrative CIDs, including interrupted helper setup.
        for identifier in self.admin:
            try:
                if self.dk.inspect(identifier) is not None:
                    self.dk.call("rm", "-f", identifier)
                    if self.dk.inspect(identifier) is not None:
                        raise DockerError("administrative CID remains: " + identifier)
            except Exception as error:
                self.cleanup_errors.append(str(error))
        if self.allocated and not self.cleanup_errors:
            try:
                observations = []
                for _ in range(2):
                    remaining = [
                        item["Id"]
                        for item in self.dk.inventory()
                        if any(
                            m["Source"] == self.scratch
                            or below(m["Source"], self.scratch)
                            for m in item["Mounts"]
                        )
                    ]
                    observations.append(remaining)
                    if remaining:
                        raise DockerError("private mounts remain; scratch retained")
                    time.sleep(0.1)
                write_json(
                    self.evidence / "quiescence.json",
                    {"controller_stopped": True, "remaining": observations},
                )
                proof, _ = self.helper(
                    {
                        "operation": "remove",
                        "parent": self.parent,
                        "scratch": self.scratch,
                        "token": self.token,
                    },
                    self.parent,
                )
                if proof["exists"]:
                    raise DockerError("scratch removal not proved")
                write_json(self.evidence / "scratch-cleanup.json", proof)
            except Exception as error:
                self.cleanup_errors.append(str(error))
        cleanup = "RETAINED_FAILURE" if self.cleanup_errors else "CLEAN"
        if self.cleanup_errors:
            self.status = "RETAINED_FAILURE"
        write_json(
            self.evidence / "cleanup.json",
            {"status": cleanup, "errors": self.cleanup_errors},
        )
        return {
            "launcher_status": self.status,
            "task_result": self.result,
            "cleanup_status": cleanup,
            "evidence_dir": str(self.evidence),
            "controller_cid": self.controller,
            "owned_cids": ([self.controller] if self.controller else [])
            + list(self.children),
        }

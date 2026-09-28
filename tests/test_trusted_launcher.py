#!/usr/bin/env python3
"""T-371 physical acceptance, RED until the real Phase B launcher exists.

Only management code runs here. The oracle runs exclusively in the product's
IsolatedVerifier. See README for durable artifacts and fixture-only checks.
"""

import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from pathlib import Path, PurePosixPath

HERE = Path(__file__).resolve().parent / "trusted_launcher"
CASTOR_ROOT = Path(
    os.environ.get("CASTOR_ROOT", str(Path(__file__).resolve().parents[1]))
)
LAUNCHER_SCRIPT = CASTOR_ROOT / "scripts/run_trusted_slot.py"
CONTROLLER = "sha256:39fad3ec792c6d55d6049f010520ebdc662c92c9eb8f6b74872b7842e073ba80"
PI = "sha256:b4f363b3436653157b87428ec6d4936b9efa26c184a77d346345071d02edddb7"
VERIFIER = (
    "python:3.12-slim@sha256:"
    "78387bc3881b8273120a12ebe6c1ab22b018ccc2c9adf565ae1ac9b536e184ea"
)
WORKLOAD_TIMEOUT, CLEANUP_TIMEOUT = 120, 60
RUN_PHYSICAL = (
    os.environ.get("CASTOR_TRUSTED_LAUNCHER_PHYSICAL") == "1" or __name__ == "__main__"
)
__test__ = RUN_PHYSICAL  # pytest discovery only; explicit unittest stays strict.


def require_launcher():
    if not LAUNCHER_SCRIPT.is_file():
        raise NotImplementedError(f"Phase A RED Gate: missing {LAUNCHER_SCRIPT}")


def full_cid(value):
    assert isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value), value
    return value


def below(value, parent):
    path, root = PurePosixPath(value), PurePosixPath(parent)
    return (
        path.is_absolute()
        and ".." not in path.parts
        and path != root
        and root in path.parents
    )


def parse_envelope(text):
    envelope = json.loads(text)  # blank/malformed/multiple objects are failures
    assert isinstance(envelope, dict)
    assert {
        "launcher_status",
        "task_result",
        "cleanup_status",
        "evidence_dir",
        "controller_cid",
        "owned_cids",
    } <= envelope.keys()
    assert envelope["launcher_status"] in (
        "SUCCEEDED",
        "FAILED",
        "TIMEOUT",
        "RETAINED_FAILURE",
    )
    assert envelope["cleanup_status"] in ("CLEAN", "RETAINED_FAILURE")
    assert isinstance(envelope["owned_cids"], list)
    for cid in envelope["owned_cids"]:
        full_cid(cid)
    assert len(set(envelope["owned_cids"])) == len(envelope["owned_cids"])
    assert envelope["task_result"] is None or isinstance(envelope["task_result"], dict)
    if (
        envelope["task_result"] is not None
        and envelope["task_result"].get("status") == "UNKNOWN_DISPUTED"
    ):
        assert envelope["launcher_status"] == (
            "RETAINED_FAILURE"
            if envelope["cleanup_status"] == "RETAINED_FAILURE"
            else "FAILED"
        )
    return envelope


def native_verifier(result, exit_code):
    """Mandatory native evidence; no optional block may turn this into a pass."""
    verifier = result["verifier_evidence"]
    assert verifier["container_removed"] is True
    assert verifier["terminal_running"] is False
    assert verifier["terminal_oom_killed"] is False
    assert type(verifier["terminal_exit_code"]) is int
    assert verifier["terminal_exit_code"] == exit_code
    assert verifier["inspected_profile"]["candidate_source"]
    for name in (
        "image_id",
        "log_driver",
        "pids_limit",
        "memory_bytes",
        "memory_swap_bytes",
        "nano_cpus",
    ):
        assert name in verifier["inspected_profile"]
    for name in ("deadline_ms", "setup_ms", "inspect_ms", "teardown_ms"):
        assert type(verifier[name]) is int and verifier[name] >= 0
    assert verifier["deadline_ms"] > 0
    for stream in ("stdout", "stderr"):
        assert type(verifier["captured_logs"][stream + "_bytes"]) is int
        assert verifier["captured_logs"][stream + "_bytes"] >= 0
        assert type(verifier["captured_logs"][stream + "_truncated"]) is bool
    return verifier


def save_json(path, value):
    with path.open("w") as stream:
        json.dump(value, stream, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def create_project_fixture(base_dir):
    source = base_dir / "source"
    source.mkdir()
    (source / "defect.txt").write_text("failing fixture\n")
    for args in (
        ["init", "-q"],
        ["add", "defect.txt"],
        [
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "-qm",
            "init",
        ],
    ):
        subprocess.run(["git", *args], cwd=source, check=True, capture_output=True)
    spec = base_dir / "spec.json"
    save_json(
        spec,
        {
            "schema_version": 1,
            "task_prompt": "Repair the failing fixture.",
            "verification_command": [
                "sh",
                "-c",
                'test "$(cat defect.txt)" = "fixed fixture"',
            ],
        },
    )
    return source, spec


def pi_profile(item, scratch):
    """Source: sandbox.rs start; socket is the only bind, never a label proof."""
    host, config, mounts = item["HostConfig"], item["Config"], item["Mounts"]
    assert host["NetworkMode"] == "none" and host["ReadonlyRootfs"] is True
    assert config["User"] == "10001:10001" and host["Privileged"] is False
    assert host["CapDrop"] == ["ALL"] and not host["CapAdd"]
    assert host["PidsLimit"] == 256 and host["Memory"] == 536870912
    assert host["NanoCpus"] == 1000000000
    assert "no-new-privileges" in host["SecurityOpt"]
    assert len(mounts) == 1
    mount = mounts[0]
    assert mount["Type"] == "bind" and mount["RW"] is False
    assert mount["Destination"] == "/run/castor/ipc.sock"
    assert below(mount["Source"], scratch) and mount["Source"].endswith("/ipc.sock")
    assert "CASTOR_IPC_SOCKET=/run/castor/ipc.sock" in config["Env"]
    assert "exec pi --extension /opt/castor/castor-pi-extension.js" in " ".join(
        config["Cmd"]
    )
    return mount["Source"]


def verifier_profile(item, scratch, image_id):
    """Recognize a still-present native verifier for emergency test recovery."""
    host, config, mounts = item["HostConfig"], item["Config"], item["Mounts"]
    assert item["Image"] == image_id and config["Image"] == VERIFIER
    assert host["NetworkMode"] == "none" and host["ReadonlyRootfs"] is True
    assert config["User"] == "10001:10001" and host["Privileged"] is False
    assert host["CapDrop"] == ["ALL"] and not host["CapAdd"]
    assert host["SecurityOpt"] == ["no-new-privileges"]
    assert host["PidsLimit"] == 256 and host["Memory"] == 1073741824
    assert host["MemorySwap"] == 1073741824 and host["NanoCpus"] == 2000000000
    assert host["LogConfig"]["Type"] == "none" and host["IpcMode"] == "private"
    assert host["PidMode"] in ("", "private")
    assert not any(
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
    assert config["WorkingDir"] == "/workspace" and not config["Entrypoint"]
    assert (
        item["Name"] == "/castor-verifier-" + config["Labels"]["castor.verifier.owner"]
    )
    assert (
        config["Cmd"][:2] == ["/bin/sh", "-c"] and config["Cmd"][3] == "castor-verifier"
    )
    assert set(host["Tmpfs"]) == {"/workspace", "/dev/shm", "/root"}
    assert len(mounts) == 1
    mount = mounts[0]
    assert mount["Type"] == "bind" and mount["RW"] is False
    assert mount["Destination"] == "/candidate" and mount["Propagation"] == "rprivate"
    assert below(mount["Source"], scratch)


class TestLauncherHarness(unittest.TestCase):
    def setUp(self):
        # FIRST: no resources before the real launcher exists.
        require_launcher()
        self.docker = str(
            Path(shutil.which(os.environ.get("DOCKER_BIN", "docker")) or "").resolve()
        )
        self.assertTrue(Path(self.docker).is_file(), "Docker executable required")
        self.work = Path(tempfile.mkdtemp(prefix="t371-harness-")).resolve()
        print(f"\nDurable harness evidence: {self.work}", file=sys.stderr)
        self.created, self.observed, self.foreign = [], {}, {}
        self.runs = []
        self.helper = None
        self.token = uuid.uuid4().hex[:12]
        self.engine_root = "/run/t371-" + self.token
        self.scratch_parent = self.engine_root + "/s"
        self.addCleanup(self.recover)  # also runs if later setUp fails
        self.docker_root = self.dk(
            "info", "--format", "{{.DockerRootDir}}"
        ).stdout.strip()
        self.assertTrue(self.docker_root.startswith("/"))
        self.assertEqual(self.image_id(CONTROLLER), CONTROLLER)
        self.assertEqual(self.image_id("substratum/castor-pi-carrier:v1"), PI)
        self.verifier_id = self.image_id(VERIFIER)
        self.linux_bin = Path(
            os.environ.get("LINUX_BIN_DIR", "/private/tmp/t365-native-build/debug")
        )
        for name in ("castor", "castord"):
            with (self.linux_bin / name).open("rb") as stream:
                self.assertEqual(
                    stream.read(4), b"\x7fELF", "real Linux binaries required"
                )
        self.project, self.spec = create_project_fixture(self.work)
        self.helper_script = (HERE / "fixture_engine.py").read_text()
        self.helper = self.create(
            "--network",
            "none",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--cap-add",
            "SYS_CHROOT",
            "--mount",
            "type=bind,src=/,dst=/engine,readonly",
            "--mount",
            "type=bind,src=/run,dst=/engine/run",
            CONTROLLER,
            "python3",
            "-c",
            self.helper_script,
            self.engine_root,
            self.token,
            "serve",
            self.docker_root,
        )
        self.dk("start", self.helper)
        deadline = time.monotonic() + 10
        while True:
            probe = self.engine("stat", self.engine_root + "/foreign.sock", check=False)
            if probe.returncode == 0 and json.loads(probe.stdout)["socket"]:
                break
            self.assertLess(time.monotonic(), deadline, probe.stderr)
            time.sleep(0.1)
        save_json(self.work / "fixture-helper-inspect.json", self.inspect(self.helper))
        self.assertEqual(self.canonical(self.docker_root), self.docker_root)

    def dk(self, *args, check=True):
        result = subprocess.run(
            [self.docker, *args], capture_output=True, text=True, timeout=15
        )
        if check:
            self.assertEqual(result.returncode, 0, f"docker {args}: {result.stderr}")
        return result

    def image_id(self, reference):
        return self.dk(
            "image", "inspect", "--format", "{{.Id}}", reference
        ).stdout.strip()

    def create(self, *args):
        cidfile = self.work / f"created-{len(self.created)}.cid"
        result = self.dk(
            "create", "--pull=never", "--cidfile", str(cidfile), *args, check=False
        )
        # Docker's CID file is the recovery handle even when stdout is lost.
        if cidfile.exists():
            self.created.append(full_cid(cidfile.read_text().strip()))
        self.assertEqual(result.returncode, 0, result.stderr)
        cid = full_cid(result.stdout.strip())
        self.assertEqual(cid, self.created[-1])
        return cid

    def inspect(self, cid):
        return json.loads(self.dk("inspect", full_cid(cid)).stdout)[0]

    def inventory(self):
        ids = self.dk("ps", "-aq", "--no-trunc").stdout.split()
        if not ids:
            return []
        result = self.dk("inspect", *ids, check=False)
        # Product removal can race a read-only inventory. Only explicit not-found
        # is tolerable; daemon/transport failure is never proof of absence.
        if result.returncode:
            self.assertTrue(result.stderr.strip())
            for line in result.stderr.splitlines():
                self.assertRegex(line.lower(), r"no such (object|container)")
            self.dk("info", "--format", "{{.DockerRootDir}}")
        return json.loads(result.stdout)

    def absent(self, cid):
        result = self.dk("inspect", full_cid(cid), check=False)
        self.assertNotEqual(result.returncode, 0, f"container leaked: {cid}")
        self.assertRegex(result.stderr.lower(), r"no such (object|container)")
        self.dk("info", "--format", "{{.DockerRootDir}}")  # outage is not absence

    def engine(self, operation, value, check=True):
        return self.dk(
            "exec",
            self.helper,
            "python3",
            "-c",
            self.helper_script,
            self.engine_root,
            self.token,
            operation,
            value,
            check=check,
        )

    def canonical(self, path):
        return json.loads(self.engine("canonical", path).stdout)

    def observe(self, run, fault_target):
        for item in self.inventory():
            cid = item["Id"]
            if cid in self.created:
                continue
            mounts = item["Mounts"]
            private = [m for m in mounts if below(m["Source"], self.scratch_parent)]
            if not private:
                continue
            # Recovery requires inspected controller or native child profile.
            if item["Image"] == CONTROLLER:
                env = dict(
                    entry.split("=", 1)
                    for entry in item["Config"]["Env"]
                    if "=" in entry
                )
                scratch = env.get("TMPDIR", "")
                self.assertTrue(below(scratch, self.scratch_parent))
                self.assertTrue(
                    any(m["Source"] == m["Destination"] == scratch for m in private)
                )
                self.observed[cid] = item
                run["controller"] = item
                run["scratch"] = scratch
                if fault_target:
                    fault_target.write_text(full_cid(cid))
                save_json(run["directory"] / "harness-controller-inspect.json", item)
            elif run.get("scratch"):
                try:
                    pi_profile(item, run["scratch"])
                except (AssertionError, KeyError):
                    try:
                        verifier_profile(item, run["scratch"], self.verifier_id)
                    except (AssertionError, KeyError, IndexError):
                        # Unmatched mounts fail recovery; they never authorize deletion.
                        continue
                    self.observed[cid] = item
                    continue
                self.observed[cid] = item
                if item["State"]["Running"] is True:
                    run["live_children"][cid] = item
                    run["last_live_seconds"][cid] = time.monotonic() - run["start"]
                    if cid not in run["socket_proofs"]:
                        proof = json.loads(
                            self.engine("stat", item["Mounts"][0]["Source"]).stdout
                        )
                        self.assertTrue(
                            proof["socket"], "owned Pi bind is not a Linux socket"
                        )
                        run["socket_proofs"][cid] = proof
                        save_json(run["directory"] / "harness-child-socket.json", proof)
                    save_json(run["directory"] / "harness-observed-child.json", item)
        save_json(self.work / "observed-cids.json", self.observed)

    def invoke(self, mode, scratch_parent=None, fault=False):
        require_launcher()
        task_id = "task-t371-" + uuid.uuid4().hex
        directory = self.work / task_id
        directory.mkdir()
        state = directory / "state"
        state.mkdir()
        run = {
            "directory": directory,
            "state": state,
            "task_id": task_id,
            "live_children": {},
            "last_live_seconds": {},
            "socket_proofs": {},
        }
        target = directory / "fault-target.cid" if fault else None
        docker = self.docker
        if fault:
            config = directory / "fault-config.json"
            save_json(
                config,
                {
                    "docker": self.docker,
                    "target_file": str(target),
                    "log_file": str(directory / "blocked-removals.jsonl"),
                },
            )
            wrapper = directory / "docker-fault"
            wrapper.write_text(
                "#!/bin/sh\nexec "
                + " ".join(
                    map(
                        shlex.quote,
                        [sys.executable, str(HERE / "fault_docker.py"), str(config)],
                    )
                )
                + ' "$@"\n'
            )
            wrapper.chmod(0o700)
            docker = str(wrapper)
        args = [
            sys.executable,
            str(LAUNCHER_SCRIPT),
            "--project",
            str(self.project),
            "--task-spec",
            str(self.spec),
            "--task-id",
            task_id,
            "--idempotency-key",
            task_id,
            "--state-root",
            str(state),
            "--scratch-parent",
            scratch_parent or self.scratch_parent,
            "--mock-mode",
            mode,
            "--linux-bin-dir",
            str(self.linux_bin),
            "--trusted-controller-image",
            CONTROLLER,
            "--docker-bin",
            docker,
            "--workload-timeout",
            str(WORKLOAD_TIMEOUT),
            "--cleanup-timeout",
            str(CLEANUP_TIMEOUT),
        ]
        start = time.monotonic()
        run["start"] = start
        self.runs.append(run)
        with (
            (directory / "launcher.stdout").open("w") as out,
            (directory / "launcher.stderr").open("w") as err,
        ):
            proc = subprocess.Popen(args, stdout=out, stderr=err)
            try:
                while proc.poll() is None:
                    self.observe(run, target)
                    self.assertLess(
                        time.monotonic() - start,
                        WORKLOAD_TIMEOUT + CLEANUP_TIMEOUT + 30,
                        "launcher exceeded outer test deadline",
                    )
                    time.sleep(0.2)
            finally:
                if proc.poll() is None:
                    proc.kill()
                proc.wait(timeout=10)
        run["elapsed"] = time.monotonic() - start
        run["code"] = proc.returncode
        run["envelope"] = parse_envelope((directory / "launcher.stdout").read_text())
        run["stderr"] = (directory / "launcher.stderr").read_text()
        return run

    def evidence(self, run):
        value = run["envelope"]["evidence_dir"]
        self.assertIsInstance(value, str)
        self.assertTrue(value)
        directory = Path(value).resolve(strict=True)
        self.assertTrue(directory.is_dir())
        self.assertTrue(directory.is_relative_to(run["state"]))
        self.assertFalse(directory.is_relative_to(Path("/run")))
        return directory

    def load(self, directory, name):
        return json.loads((directory / name).read_text())

    def assert_lifecycle(self, run, retained=False):
        env = run["envelope"]
        evidence = self.evidence(run)
        cid = full_cid(env["controller_cid"])
        self.assertEqual(cid, run["controller"]["Id"])
        self.assertIn(cid, env["owned_cids"])
        terminal = self.load(evidence, "controller-inspect.json")
        self.assertEqual(terminal["Id"], cid)
        self.assertFalse(terminal["State"]["Running"])
        self.assertEqual(terminal["State"]["Status"], "exited")
        for name in (
            "controller.stdout",
            "controller.stderr",
            "cleanup.json",
            "mount-map.json",
        ):
            self.assertTrue((evidence / name).is_file(), name)
        events = [
            json.loads(line)
            for line in (evidence / "lifecycle.jsonl").read_text().splitlines()
        ]
        own = [event["event"] for event in events if event["cid"] == cid]
        expected = [
            "create",
            "start",
            "wait",
            "inspect",
            "archive",
            "remove_failed" if retained else "remove",
        ]
        cursor = -1
        for event in expected:
            cursor = own.index(event, cursor + 1)
        if not retained:
            for owned in env["owned_cids"]:
                self.absent(owned)
            self.assertFalse(
                json.loads(self.engine("stat", run["scratch"]).stdout)["exists"]
            )
        return evidence

    def assert_native(self, run, success, retained=False):
        env = run["envelope"]
        self.assertEqual(
            run["code"], 2 if retained else 0 if success else 1, run["stderr"]
        )
        self.assertEqual(
            env["launcher_status"],
            "RETAINED_FAILURE" if retained else "SUCCEEDED" if success else "FAILED",
        )
        self.assertEqual(
            env["cleanup_status"], "RETAINED_FAILURE" if retained else "CLEAN"
        )
        evidence = self.assert_lifecycle(run, retained=retained)
        result = env["task_result"]
        self.assertIsInstance(result, dict)
        native = self.load(evidence, "task-result.json")
        board = self.load(run["state"], "board.json")
        self.assertEqual(result, native)
        self.assertEqual(
            (evidence / "task-result.json").read_bytes(),
            (evidence / "controller.stdout").read_bytes(),
        )
        self.assertEqual(result, board[run["task_id"]]["result"])
        self.assertEqual(result["task_id"], run["task_id"])
        self.assertEqual(result["status"], "SUCCEEDED" if success else "FAILED")
        self.assertEqual(
            result["failure_reason"], "NONE" if success else "TEST_VERIFICATION_FAILED"
        )
        self.assertIs(result["test_passed"], success)
        self.assertEqual(result["test_exit_code"], 0 if success else 1)
        self.assertEqual(result["settled_actions_count"], 1)
        self.assertEqual(result["committed_turns"], [1])
        self.assertRegex(result["workspace_snapshot_sha256"], r"^[0-9a-f]{64}$")
        self.assertRegex(result["derived_task_image_digest"], r"^sha256:[0-9a-f]{64}$")
        for child in run["live_children"].values():
            self.assertEqual(child["Image"], result["derived_task_image_digest"])
        self.assertIn(
            "+fixed fixture" if success else "+still failing fixture",
            result["patch_diff"],
        )
        self.assertEqual(
            result["final_patch_sha256"],
            "sha256:" + hashlib.sha256(result["patch_diff"].encode()).hexdigest(),
        )
        verifier = native_verifier(result, 0 if success else 1)
        self.assertIs(verifier["container_removed"], True)
        self.assertIs(verifier["terminal_running"], False)
        self.assertIs(verifier["terminal_oom_killed"], False)
        self.assertEqual(verifier["terminal_exit_code"], 0 if success else 1)
        for name in ("deadline_ms", "setup_ms", "inspect_ms", "teardown_ms"):
            self.assertIs(type(verifier[name]), int)
            self.assertGreaterEqual(verifier[name], 0)
        self.assertGreater(verifier["deadline_ms"], 0)
        profile = verifier["inspected_profile"]
        self.assertEqual(profile["image_id"], self.verifier_id)
        self.assertEqual(profile["log_driver"], "none")
        self.assertEqual(profile["pids_limit"], 256)
        self.assertEqual(profile["memory_bytes"], 1073741824)
        self.assertEqual(profile["memory_swap_bytes"], 1073741824)
        self.assertEqual(profile["nano_cpus"], 2000000000)
        self.assert_mapping(run, profile["candidate_source"])
        task_state = run["state"] / "tasks" / run["task_id"]
        for stream in ("stdout", "stderr"):
            data = (task_state / f"verifier_{stream}.log").read_bytes()
            self.assertEqual(len(data), verifier["captured_logs"][stream + "_bytes"])
            self.assertIs(verifier["captured_logs"][stream + "_truncated"], False)
        events = [
            json.loads(line)
            for line in (task_state / "pi.jsonl").read_text().splitlines()
        ]
        ends = [
            event["message"]
            for event in events
            if event.get("type") == "message_end"
            and event.get("message", {}).get("role") == "assistant"
        ]
        self.assertTrue(ends, "native Pi completion log required")
        self.assertEqual(ends[-1]["stopReason"], "stop")
        self.assertFalse(ends[-1].get("errorMessage"))
        self.assert_read_then_edit(run, events)
        self.assertEqual((self.project / "defect.txt").read_text(), "failing fixture\n")

    def assert_read_then_edit(self, run, events):
        read_start = next(
            i
            for i, event in enumerate(events)
            if event.get("type") == "tool_execution_start"
            and event.get("toolName") == "castor_read_file"
            and event.get("toolCallId") == "read-1"
        )
        read_end = next(
            i
            for i, event in enumerate(events)
            if event.get("type") == "tool_execution_end"
            and event.get("toolName") == "castor_read_file"
            and event.get("toolCallId") == "read-1"
        )
        edit_start = next(
            i
            for i, event in enumerate(events)
            if event.get("type") == "tool_execution_start"
            and event.get("toolName") == "castor_edit_file"
            and event.get("toolCallId") == "edit-1"
        )
        self.assertLess(read_start, read_end)
        self.assertLess(read_end, edit_start)
        self.assertEqual(events[read_start]["args"], {"path": "defect.txt"})
        self.assertIs(events[read_end]["isError"], False)
        observed = events[read_end]["result"]
        self.assertEqual(observed["details"]["path"], "defect.txt")
        self.assertEqual(
            observed["content"], [{"type": "text", "text": "failing fixture\n"}]
        )
        self.assertEqual(events[edit_start]["args"]["path"], "defect.txt")
        evidence = self.evidence(run)
        calls = [
            json.loads(line)
            for line in (evidence / "mock-calls.jsonl").read_text().splitlines()
        ]
        self.assertEqual(
            [(call["ordinal"], "request" in call) for call in calls],
            [(0, True), (0, False), (1, True), (1, False)],
        )
        for ordinal, expected_name in enumerate(
            ("castor_read_file", "castor_edit_file")
        ):
            request, response = (
                calls[ordinal * 2]["request"],
                calls[ordinal * 2 + 1]["response"],
            )
            self.assertEqual(response["interaction_id"], request["interaction_id"])
            content = bytes(response["content"])
            self.assertEqual(
                response["observation_digest"],
                "sha256:" + hashlib.sha256(content).hexdigest(),
            )
            tool = json.loads(content)["content"][0]
            self.assertEqual(tool["name"], expected_name)
            self.assertEqual(tool["arguments"]["path"], "defect.txt")
        results = [
            message
            for message in calls[2]["request"]["request"]["messages"]
            if message.get("role") == "toolResult"
            and message.get("toolCallId") == "read-1"
        ]
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["toolName"], "castor_read_file")
        self.assertIs(results[0]["isError"], False)
        self.assertEqual(results[0]["content"], observed["content"])
        summary = self.load(evidence, "mock-summary.json")
        self.assertEqual(summary["local_requests"], 2)
        self.assertEqual(summary["provider_calls"], 0)
        self.assertEqual(summary["transport"], "AF_UNIX")
        self.assertIsNone(summary["error"])

    def assert_mapping(self, run, candidate):
        mapping = self.load(self.evidence(run), "mount-map.json")
        controller = run["controller"]  # independently read live Docker inspect
        scratch = run["scratch"]
        self.assertEqual(mapping["docker_root"], self.docker_root)
        self.assertEqual(mapping["scratch"], scratch)
        self.assertEqual(mapping["candidate"], candidate)
        self.assertTrue(below(candidate, scratch))
        self.assertEqual(self.canonical(candidate), candidate)
        self.assertEqual(self.canonical(scratch), scratch)
        self.assertFalse(
            below(scratch, self.docker_root) or below(self.docker_root, scratch)
        )
        self.assertNotEqual(scratch, self.docker_root)
        mounts = controller["Mounts"]
        self.assertTrue(
            any(m["Source"] == m["Destination"] == scratch and m["RW"] for m in mounts)
        )
        self.assertIn("TMPDIR=" + scratch, controller["Config"]["Env"])
        # Host/Engine evidence mapping must come from actual inspect.
        state_mount = next(
            m for m in mounts if m["Destination"] == mapping["state_destination"]
        )
        self.assertEqual(state_mount["Source"], mapping["state_source"])
        self.assertEqual(mapping["state_host"], str(run["state"]))
        state_source = mapping["state_source"]
        for other in (scratch, self.docker_root, "/run"):
            self.assertNotEqual(state_source, other)
            self.assertFalse(below(state_source, other) or below(other, state_source))
        self.assertIn(
            "CASTOR_STATE_ROOT=" + mapping["state_destination"],
            controller["Config"]["Env"],
        )

    def assert_timeout(self, run):
        env = run["envelope"]
        self.assertEqual(run["code"], 1, run["stderr"])
        self.assertEqual(env["launcher_status"], "TIMEOUT")
        self.assertEqual(env["cleanup_status"], "CLEAN")
        self.assertIsNone(env["task_result"])
        self.assertGreaterEqual(run["elapsed"], WORKLOAD_TIMEOUT)
        self.assertLess(run["elapsed"], WORKLOAD_TIMEOUT + CLEANUP_TIMEOUT + 30)
        evidence = self.assert_lifecycle(run)
        child = self.load(evidence, "owned-child-before-kill.json")
        cid = full_cid(child["Id"])
        self.assertIn(
            cid, run["live_children"], "no independently observed live Pi child"
        )
        self.assertIn(cid, env["owned_cids"])
        self.assertNotEqual(cid, env["controller_cid"])
        self.assertGreaterEqual(
            run["last_live_seconds"][cid],
            WORKLOAD_TIMEOUT - 5,
            "Pi was killed early or never lived through workload deadline",
        )
        self.assertTrue(run["socket_proofs"][cid]["socket"])
        self.assertIs(child["State"]["Running"], True)
        events = [
            json.loads(line)
            for line in (evidence / "lifecycle.jsonl").read_text().splitlines()
        ]
        kill_controller = next(
            i
            for i, e in enumerate(events)
            if e["cid"] == env["controller_cid"] and e["event"] == "kill"
        )
        kill_child = next(
            i for i, e in enumerate(events) if e["cid"] == cid and e["event"] == "kill"
        )
        self.assertLess(
            kill_controller,
            kill_child,
            "stop controller before it can race child reaping",
        )
        socket_path = pi_profile(child, run["scratch"])
        self.assertEqual(
            socket_path, pi_profile(run["live_children"][cid], run["scratch"])
        )
        self.assertEqual(child["Image"], run["live_children"][cid]["Image"])
        events = [
            json.loads(line)
            for line in (evidence / "lifecycle.jsonl").read_text().splitlines()
        ]
        self.assertTrue(
            any(
                e["cid"] == cid and e["event"] == "kill" and e["signal"] == "SIGKILL"
                for e in events
            )
        )
        own = [e["event"] for e in events if e["cid"] == cid]
        cursor = -1
        for event in (
            "observe_running",
            "kill",
            "wait",
            "inspect",
            "archive",
            "remove",
        ):
            cursor = own.index(event, cursor + 1)
        terminal = self.load(evidence, f"children/{cid}.inspect.json")
        self.assertEqual(terminal["Id"], cid)
        self.assertIs(terminal["State"]["Running"], False)
        self.assertEqual(terminal["State"]["ExitCode"], 137)
        self.assertFalse(terminal["State"]["OOMKilled"])
        for stream in ("stdout", "stderr"):
            self.assertTrue((evidence / f"children/{cid}.{stream}").is_file())
        self.absent(cid)
        self.assertFalse((evidence / "task-result.json").exists())

    def test_v1_normal_full_path(self):
        self.assert_native(self.invoke("fixing"), True)

    def test_v2_failing_oracle(self):
        self.assert_native(self.invoke("nonfixing"), False)

    def test_v3_timeout_cleanup(self):
        self.assert_timeout(self.invoke("timeout"))

    def test_v4_no_foreign_deletion(self):
        socket = self.engine_root + "/foreign.sock"
        outsider = self.engine_root + "/outsider"
        configurations = [
            ["--label", "castor.verifier.owner=foreign-" + self.token, VERIFIER],
            [
                "--network",
                "none",
                "--read-only",
                "--user",
                "10001:10001",
                "--cap-drop",
                "ALL",
                "--security-opt",
                "no-new-privileges",
                "--pids-limit",
                "256",
                "--memory",
                "512m",
                "--cpus",
                "1",
                "--tmpfs",
                "/tmp:rw,nosuid,nodev,size=64m,mode=1777",
                "--env",
                "CASTOR_IPC_SOCKET=/run/castor/ipc.sock",
                "--mount",
                f"type=bind,src={socket},dst=/run/castor/ipc.sock,readonly",
                PI,
            ],
            ["--mount", f"type=bind,src={outsider},dst=/outsider,readonly", CONTROLLER],
        ]
        for number, config in enumerate(configurations):
            cid = self.create(
                "--name", f"t371-foreign-{self.token}-{number}", *config, "sleep", "900"
            )
            self.dk("start", cid)
            item = self.inspect(cid)
            self.assertIs(item["State"]["Running"], True)
            self.foreign[cid] = item
        self.assertEqual(len(self.foreign), 3)
        save_json(self.work / "foreign-before.json", self.foreign)
        for mode in ("fixing", "timeout"):
            run = self.invoke(mode)
            after = {cid: self.inspect(cid) for cid in self.foreign}
            save_json(run["directory"] / "foreign-after.json", after)
            self.assertTrue(set(self.foreign).isdisjoint(run["envelope"]["owned_cids"]))
            for cid, before in self.foreign.items():
                for key in ("Id", "Name", "Mounts", "State", "Config", "HostConfig"):
                    self.assertEqual(
                        before[key], after[cid][key], f"foreign {cid}: {key}"
                    )
            if mode == "fixing":
                self.assert_native(run, True)
            else:
                self.assert_timeout(run)

    def test_v5_same_path_mapping_and_docker_root_check(self):
        alias = self.engine_root + "/docker-root-link"
        self.assertEqual(self.canonical(alias), self.docker_root)
        for bad in (
            self.docker_root,
            self.docker_root + "/bad-scratch",
            "/",
            alias + "/bad-scratch",
        ):
            run = self.invoke("fixing", scratch_parent=bad)
            env = run["envelope"]
            self.assertEqual(run["code"], 1, run["stderr"])
            self.assertEqual(env["launcher_status"], "FAILED")
            self.assertIsNone(env["task_result"])
            self.assertEqual(env["owned_cids"], [])
            self.assertIsNone(env["controller_cid"])
            self.assertEqual(env["cleanup_status"], "CLEAN")
            self.assertNotIn("controller", run, "preflight launched a controller")
            self.assertIn("DockerRootDir", run["stderr"])
            preflight = self.load(self.evidence(run), "preflight.json")
            self.assertEqual(preflight["docker_root"], self.docker_root)
            self.assertEqual(preflight["requested_scratch_parent"], bad)
            self.assertEqual(preflight["canonical_scratch_parent"], self.canonical(bad))
            self.assertEqual(preflight["rejection"], "DOCKER_ROOT_OVERLAP")
        self.assert_native(self.invoke("fixing"), True)

    def test_v6_lifecycle_order_and_retained_failure(self):
        run = self.invoke("fixing", fault=True)
        env = run["envelope"]
        self.assertEqual(run["code"], 2, run["stderr"])
        self.assertEqual(env["launcher_status"], "RETAINED_FAILURE")
        self.assertEqual(env["cleanup_status"], "RETAINED_FAILURE")
        self.assert_native(run, True, retained=True)
        evidence = self.evidence(run)
        blocked = [
            json.loads(line)
            for line in (run["directory"] / "blocked-removals.jsonl")
            .read_text()
            .splitlines()
        ]
        self.assertTrue(blocked, "fault never fired")
        for entry in blocked:
            self.assertEqual(entry["blocked_cids"], [env["controller_cid"]])
        self.assertEqual(
            self.inspect(env["controller_cid"])["Id"], env["controller_cid"]
        )
        self.assertTrue(
            json.loads(self.engine("stat", run["scratch"]).stdout)["exists"]
        )
        self.assertEqual(
            self.load(evidence, "cleanup.json")["status"], "RETAINED_FAILURE"
        )
        self.assertEqual(env["task_result"], self.load(evidence, "task-result.json"))
        self.assertEqual(
            env["task_result"],
            self.load(run["state"], "board.json")[run["task_id"]]["result"],
        )
        # Recovery is test-owned, unwrapped, and cannot turn this vector into CLEAN.
        self.recover_run(run)
        self.assertTrue(evidence.is_dir())

    def recover_run(self, run):
        removed = []
        for cid, item in list(self.observed.items()):
            if cid == run["controller"]["Id"] or any(
                below(m["Source"], run["scratch"]) for m in item["Mounts"]
            ):
                self.remove_recorded(cid)
                removed.append(cid)
        self.assert_no_mounts(run["scratch"])
        proof = json.loads(self.engine("remove", run["scratch"]).stdout)
        self.assertTrue(proof["absent"])
        save_json(
            run["directory"] / "harness-recovery.json",
            {
                "removed_cids": removed,
                "helper_cid": self.helper,
                "scratch_proof": proof,
            },
        )

    def assert_no_mounts(self, root):
        for item in self.inventory():
            if item["Id"] == self.helper:
                continue
            for mount in item["Mounts"]:
                self.assertFalse(
                    mount["Source"] == root or below(mount["Source"], root),
                    f"unrecovered container {item['Id']}; refusing scratch deletion",
                )

    def remove_recorded(self, cid):
        self.assertTrue(
            cid in self.created or cid in self.observed, "unrecorded cleanup CID"
        )
        result = self.dk("rm", "-f", full_cid(cid), check=False)
        if result.returncode:
            self.assertRegex(result.stderr.lower(), r"no such (object|container)")
        self.absent(cid)

    def recover(self):
        # No label/name/volume sweep. Retry only exact recorded CIDs, direct Docker.
        errors = []
        for run in self.runs:
            try:
                self.observe(run, None)
            except Exception as error:
                errors.append(f"final ownership inventory: {error}")
        ids = [
            cid for cid, item in self.observed.items() if item["Image"] == CONTROLLER
        ]
        ids += [cid for cid in self.observed if cid not in ids]
        ids += [cid for cid in self.created if cid != self.helper]
        for cid in dict.fromkeys(ids):
            try:
                self.remove_recorded(cid)
            except Exception as error:
                errors.append(f"{cid}: {error}")
        if self.helper:
            try:
                self.assert_no_mounts(self.engine_root)
                proof = json.loads(self.engine("remove", self.engine_root).stdout)
                self.assertTrue(proof["absent"])
                save_json(self.work / "fixture-cleanup.json", proof)
            except Exception as error:
                errors.append(f"scratch: {error}")
            finally:
                try:
                    self.remove_recorded(self.helper)
                except Exception as error:
                    errors.append(f"helper: {error}")
        self.assertFalse(errors, "\n".join(errors))


TestLauncherHarness.__test__ = RUN_PHYSICAL


if __name__ == "__main__":
    unittest.main()

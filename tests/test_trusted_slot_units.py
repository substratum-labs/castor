"""Management-only launcher regressions; no target code or Docker resources."""

import ast
import hashlib
import importlib
import importlib.util
import json
import re
import socket
import struct
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))


class TrustedSlotUnits(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(
            importlib.util.find_spec("trusted_slot"),
            "launcher management module absent",
        )
        self.common = importlib.import_module("trusted_slot.common")
        self.mock = importlib.import_module("trusted_slot.mock_model")

    def test_verifier_aliases_require_exact_image_and_private_profile(self):
        legacy = self.common.VERIFIER
        canonical = "docker.io/library/" + legacy
        image_id = "sha256:" + "a" * 64
        item = {
            "Image": image_id,
            "Name": "/castor-verifier-owned",
            "Config": {
                "Image": legacy,
                "User": "10001:10001",
                "WorkingDir": "/workspace",
                "Entrypoint": None,
                "Labels": {"castor.verifier.owner": "owned"},
                "Cmd": ["/bin/sh", "-c", "true", "castor-verifier"],
            },
            "Mounts": [
                {
                    "Type": "bind",
                    "Source": "/run/owned/candidate",
                    "Destination": "/candidate",
                    "RW": False,
                    "Propagation": "rprivate",
                }
            ],
            "HostConfig": {
                "NetworkMode": "none",
                "ReadonlyRootfs": True,
                "Privileged": False,
                "CapDrop": ["ALL"],
                "CapAdd": None,
                "PidsLimit": 256,
                "SecurityOpt": ["no-new-privileges"],
                "Memory": 1073741824,
                "MemorySwap": 1073741824,
                "NanoCpus": 2000000000,
                "LogConfig": {"Type": "none"},
                "IpcMode": "private",
                "PidMode": "",
                "Tmpfs": {"/workspace": "x", "/root": "x", "/dev/shm": "x"},
            },
        }
        for reference in (legacy, canonical):
            item["Config"]["Image"] = reference
            with self.subTest(reference=reference):
                self.assertEqual(
                    self.common.child_profile(item, "/run/owned", image_id), "verifier"
                )
        for reference in (
            "example.invalid/library/" + legacy,
            canonical + "0",
            "python:latest",
        ):
            item["Config"]["Image"] = reference
            self.assertIsNone(self.common.child_profile(item, "/run/owned", image_id))
        item["Config"]["Image"] = canonical
        self.assertIsNone(self.common.child_profile(item, "/run/other", image_id))
        self.assertIsNone(
            self.common.child_profile(item, "/run/owned", "sha256:" + "b" * 64)
        )

    def test_overlap_rejects_both_directions_but_not_prefix_sibling(self):
        for path in ("/var/lib/docker", "/var/lib/docker/sub", "/var/lib", "/"):
            self.assertTrue(self.common.overlap(path, "/var/lib/docker"))
        self.assertFalse(
            self.common.overlap("/var/lib/docker-other", "/var/lib/docker")
        )

    def test_short_cids_and_names_cannot_be_cleanup_handles(self):
        for value in ("name", "a" * 12, "-f", "a" * 63 + "z"):
            with self.assertRaises(ValueError):
                self.common.cid(value)
        self.assertEqual(self.common.cid("a" * 64), "a" * 64)

    def test_disputed_result_is_preserved_without_optional_field_invention(self):
        native = {
            "status": "UNKNOWN_DISPUTED",
            "failure_reason": "ARMED_UNSETTLED_EFFECT",
        }
        result, status = self.common.native_result(json.dumps(native).encode(), None)
        self.assertEqual(result, native)
        self.assertEqual(status, "FAILED")
        self.assertNotIn("verifier_evidence", result)
        self.assertEqual(self.common.native_result(b"", None), (None, "FAILED"))

    def test_malformed_native_stdout_cannot_become_success(self):
        for data in (b"not JSON", b"{}\n{}", b"[]"):
            with self.assertRaises((ValueError, TypeError)):
                self.common.native_result(data, None)

    def test_framed_mock_receives_real_socket_message(self):
        left, right = socket.socketpair()
        with left, right:
            payload = b'{"request":{"schema_version":1},"interaction_id":"i-1"}'
            right.sendall(struct.pack(">I", len(payload)) + payload)
            self.assertEqual(self.mock.read_frame(left), json.loads(payload))

    def test_closed_or_oversize_frame_fails(self):
        for header in (b"", struct.pack(">I", 16 * 1024 * 1024 + 1)):
            left, right = socket.socketpair()
            with left, right:
                right.sendall(header)
                right.shutdown(socket.SHUT_WR)
                with self.assertRaises((ValueError, EOFError)):
                    self.mock.read_frame(left)

    @staticmethod
    def read_observation_request():
        return {
            "interaction_id": "i-2",
            "request": {
                "schema_version": 1,
                "messages": [
                    {
                        "role": "toolResult",
                        "toolCallId": "read-1",
                        "toolName": "castor_read_file",
                        "content": [{"type": "text", "text": "failing fixture\n"}],
                        "isError": False,
                    }
                ],
            },
        }

    def test_first_response_is_a_real_read_request(self):
        response = self.mock.reply(
            {"request": {"schema_version": 1}, "interaction_id": "i-1"}, "fixing", 0
        )
        body = json.loads(bytes(response["content"]))
        self.assertEqual(
            body["content"],
            [
                {
                    "type": "toolCall",
                    "id": "read-1",
                    "name": "castor_read_file",
                    "arguments": {"path": "defect.txt"},
                }
            ],
        )
        self.assertEqual(body["stopReason"], "toolUse")

    def test_edit_requires_successful_read_observation_of_target_content(self):
        for content, is_error in (("wrong file\n", False), ("failing fixture\n", True)):
            request = self.read_observation_request()
            result = request["request"]["messages"][0]
            result["content"][0]["text"] = content
            result["isError"] = is_error
            with self.assertRaises(ValueError):
                self.mock.reply(request, "fixing", 1)
        with self.assertRaises(ValueError):
            self.mock.reply(
                {
                    "request": {"schema_version": 1, "messages": []},
                    "interaction_id": "i-2",
                },
                "fixing",
                1,
            )

    def test_terminal_edit_does_not_require_a_third_model_request(self):
        response = self.mock.reply(self.read_observation_request(), "fixing", 1)
        self.assertEqual(
            json.loads(bytes(response["content"]))["content"][0]["name"],
            "castor_edit_file",
        )
        with self.assertRaises(ValueError):
            self.mock.reply(self.read_observation_request(), "fixing", 2)

    def test_nonfixing_is_a_real_wrong_edit_with_content_digest(self):
        reply = self.mock.reply(self.read_observation_request(), "nonfixing", 1)
        content = bytes(reply["content"])
        self.assertEqual(
            reply["observation_digest"], "sha256:" + hashlib.sha256(content).hexdigest()
        )
        edit = json.loads(content)["content"][0]
        self.assertEqual(edit["name"], "castor_edit_file")
        self.assertEqual(
            edit["arguments"]["edits"],
            [{"oldText": "failing fixture", "newText": "still failing fixture"}],
        )
        self.assertEqual(reply["interaction_id"], "i-2")

    def test_cli_rejects_nonfinite_deadlines_before_any_docker_operation(self):
        launcher = Path(__file__).resolve().parents[1] / "scripts/run_trusted_slot.py"
        required = [
            "--project",
            "/absent/project",
            "--task-spec",
            "/absent/spec",
            "--task-id",
            "task-unit",
            "--idempotency-key",
            "task-unit",
            "--state-root",
            "/absent/state",
            "--linux-bin-dir",
            "/absent/bin",
            "--trusted-controller-image",
            "unused",
            "--mock-mode",
            "fixing",
            "--docker-bin",
            sys.executable,
        ]
        for value in ("nan", "inf", "-inf"):
            result = subprocess.run(
                [
                    sys.executable,
                    str(launcher),
                    *required,
                    "--workload-timeout=" + value,
                ],
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 2)
            self.assertIn("finite", result.stderr)

    def test_snapshot_oracle_accepts_actual_native_bare_digest_only(self):
        tests = Path(__file__).resolve().parent
        result = json.loads(
            (tests / "trusted_launcher/native-success.json").read_bytes()
        )
        tree = ast.parse((tests / "test_trusted_launcher.py").read_text())
        checks = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "assertRegex"
            and isinstance(node.args[0], ast.Subscript)
            and isinstance(node.args[0].slice, ast.Constant)
            and node.args[0].slice.value == "workspace_snapshot_sha256"
        ]
        self.assertEqual(len(checks), 1)
        pattern = ast.literal_eval(checks[0].args[1])
        digest = result["workspace_snapshot_sha256"]
        self.assertIsNotNone(re.fullmatch(pattern, digest))
        for bad in ("sha256:" + digest, digest[:-1], digest.upper(), digest + "0"):
            self.assertIsNone(re.fullmatch(pattern, bad))


if __name__ == "__main__":
    unittest.main()

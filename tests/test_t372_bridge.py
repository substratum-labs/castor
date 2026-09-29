"""T-372 trusted host transport contract; all HTTP is a local fake."""

import contextlib
import hashlib
import io
import json
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from trusted_slot.mock_model import reply as mock_reply
from trusted_slot.real_model import (
    MODEL_DIGEST,
    FileBridgeModel,
    HostExchange,
    HTTPTransportError,
    model_request,
    native_response,
    validate_model_pin,
)


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.exchange = HostExchange(self.root, deadline_seconds=30)
        self.exchange.pin_checker = lambda: {"name": "qwen3.5:9b"}
        request = {
            "schema_version": 1,
            "interaction_id": "i-1",
            "messages": [
                {
                    "role": "system",
                    "content": "base",
                    "sections": {"safety": "safe", "task": "fix bug"},
                },
                {"role": "user", "content": [{"type": "text", "text": "hello"}]},
            ],
            "tools": [
                {
                    "name": "castor_read_file",
                    "description": "read",
                    "parameters": {"type": "object"},
                }
            ],
        }
        self.native = {
            "interaction_id": "i-1",
            "request": request,
            "request_digest": "sha256:"
            + hashlib.sha256(
                json.dumps(
                    request, sort_keys=True, separators=(",", ":"), ensure_ascii=False
                ).encode()
            ).hexdigest(),
        }

    def submit(self, name="one", request=None):
        folder = self.root / "bridge"
        folder.mkdir(exist_ok=True)
        (folder / f"request-{name}.json").write_text(json.dumps(request or self.native))
        return folder / f"response-{name}.json"

    @staticmethod
    def good(*args, **kwargs):
        return {
            "model": "qwen3.5:9b",
            "done": True,
            "message": {
                "content": "",
                "tool_calls": [
                    {
                        "id": "read-1",
                        "function": {
                            "name": "castor_read_file",
                            "arguments": {"path": "file.txt"},
                        },
                    }
                ],
            },
            "prompt_eval_count": 99,
            "eval_count": 42,
            "total_duration": 100,
        }

    def test_original_system_sections_and_tools_are_preserved(self):
        payload = model_request(self.native)
        self.assertEqual(
            payload["messages"][0],
            {"role": "system", "content": "base\n\nsafe\n\nfix bug"},
        )
        self.assertEqual(payload["messages"][1], {"role": "user", "content": "hello"})
        self.assertEqual(payload["tools"][0]["function"]["name"], "castor_read_file")
        self.assertNotIn("JSON object", json.dumps(payload))

    def test_system_updates_and_tool_result_keep_pi_semantics(self):
        native = json.loads(json.dumps(self.native))
        native["request"]["messages"] += [
            {"role": "system", "sections": {"task": None, "safety": "revised"}},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "r1",
                        "type": "function",
                        "function": {
                            "name": "castor_read_file",
                            "arguments": {"path": "file.txt"},
                        },
                    }
                ],
            },
            {
                "role": "toolResult",
                "toolCallId": "r1",
                "toolName": "castor_read_file",
                "content": [{"type": "text", "text": "file body"}],
            },
        ]
        native["request_digest"] = (
            "sha256:"
            + hashlib.sha256(
                json.dumps(
                    native["request"],
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                ).encode()
            ).hexdigest()
        )
        messages = model_request(native)["messages"]
        self.assertIn('Removed system prompt section "task".', messages[2]["content"])
        self.assertIn(
            'Updated system prompt section "safety":\n\nrevised', messages[2]["content"]
        )
        self.assertEqual(
            messages[3]["tool_calls"][0]["function"]["name"], "castor_read_file"
        )
        self.assertEqual(
            messages[4],
            {
                "role": "tool",
                "content": "file body",
                "tool_call_id": "r1",
                "tool_name": "castor_read_file",
            },
        )

    def test_changed_same_id_is_denied_without_http(self):
        self.submit()
        with patch.object(self.exchange, "transport", return_value=self.good()) as call:
            self.exchange.poll()
            changed = json.loads(json.dumps(self.native))
            changed["request"]["messages"][1]["content"][0]["text"] = "changed"
            self.submit("changed", changed)
            self.exchange.poll()
            self.assertEqual(call.call_count, 1)
            self.assertTrue((self.root / "bridge/error-changed.json").exists())

    def test_concurrent_pollers_reserve_one_http_call(self):
        self.submit()
        self.submit("duplicate")
        with patch.object(
            self.exchange,
            "transport",
            side_effect=lambda *a: (time.sleep(0.03), self.good())[1],
        ) as call:
            threads = [threading.Thread(target=self.exchange.poll) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            self.assertEqual(call.call_count, 1)

    def test_reservation_precedes_http_and_duplicate_is_not_a_new_call(self):
        first = self.submit()

        def transport(payload, timeout):
            ledger = json.loads((self.root / "budget.json").read_text())
            self.assertEqual(len(ledger["reservations"]), 1)
            self.assertEqual(ledger["reservations"][0]["status"], "RESERVED")
            return self.good()

        with patch.object(self.exchange, "transport", side_effect=transport) as call:
            self.exchange.poll()
            self.assertEqual(json.loads(first.read_text())["interaction_id"], "i-1")
            second = self.submit("two")
            self.exchange.poll()
            self.assertEqual(
                json.loads(second.read_text()), json.loads(first.read_text())
            )
            self.assertEqual(call.call_count, 1)

    def test_three_unique_calls_then_denial(self):
        with patch.object(self.exchange, "transport", side_effect=self.good) as call:
            for n in range(4):
                request = json.loads(json.dumps(self.native))
                request["interaction_id"] = request["request"]["interaction_id"] = (
                    f"i-{n}"
                )
                request["request_digest"] = (
                    "sha256:"
                    + hashlib.sha256(
                        json.dumps(
                            request["request"],
                            sort_keys=True,
                            separators=(",", ":"),
                            ensure_ascii=False,
                        ).encode()
                    ).hexdigest()
                )
                response = self.submit(str(n), request)
                self.exchange.poll()
                self.assertEqual(response.exists(), n < 3)
            self.assertEqual(call.call_count, 3)
            self.assertEqual(
                len(
                    json.loads((self.root / "budget.json").read_text())["reservations"]
                ),
                3,
            )

    def test_errors_spend_budget_without_retry(self):
        for error in (TimeoutError("slow"), ValueError("malformed")):
            with (
                self.subTest(error=error),
                patch.object(self.exchange, "transport", side_effect=error) as call,
            ):
                n = len(list((self.root / "bridge").glob("request-*")))
                request = json.loads(json.dumps(self.native))
                request["interaction_id"] = request["request"]["interaction_id"] = (
                    f"i-{n}"
                )
                request["request_digest"] = (
                    "sha256:"
                    + hashlib.sha256(
                        json.dumps(
                            request["request"],
                            sort_keys=True,
                            separators=(",", ":"),
                            ensure_ascii=False,
                        ).encode()
                    ).hexdigest()
                )
                self.submit(str(n), request)
                self.exchange.poll()
                self.assertEqual(call.call_count, 1)
        self.assertEqual(
            len(json.loads((self.root / "budget.json").read_text())["reservations"]), 2
        )

    def test_missing_usage_is_null_and_marks_measurement_incomplete(self):
        self.submit()
        with patch.object(
            self.exchange, "transport", return_value={"message": {"content": "x"}}
        ):
            self.exchange.poll()
        record = json.loads((self.root / "budget.json").read_text())["reservations"][0]
        self.assertIsNone(record["input_tokens"])
        self.assertIsNone(record["output_tokens"])
        self.assertEqual(record["measurement"], "INCOMPLETE")

    def test_native_response_rejects_malformed_model_content(self):
        with self.assertRaises(ValueError):
            native_response(
                self.native,
                {
                    "message": {"tool_calls": [{}]},
                    "prompt_eval_count": 1,
                    "eval_count": 1,
                },
            )

    def test_output_over_512_is_recorded_as_failure(self):
        self.submit()
        oversized = self.good()
        oversized["eval_count"] = 513
        with patch.object(self.exchange, "transport", return_value=oversized) as call:
            self.exchange.poll()
            self.assertEqual(call.call_count, 1)
        record = json.loads((self.root / "budget.json").read_text())["reservations"][0]
        self.assertEqual(record["output_tokens"], 513)
        self.assertEqual(record["status"], "FAILED")

    def test_expired_deadline_never_sends_http(self):
        self.exchange.deadline = time.monotonic() - 1
        self.submit()
        with patch.object(self.exchange, "transport") as call:
            self.exchange.poll()
            call.assert_not_called()
        self.assertEqual(
            json.loads((self.root / "budget.json").read_text())["reservations"][0][
                "status"
            ],
            "FAILED",
        )

    def test_http_error_raw_body_is_durable_and_spends_one_call(self):
        self.submit()
        with patch.object(
            self.exchange, "transport", side_effect=HTTPTransportError(503, b"offline")
        ) as call:
            self.exchange.poll()
            self.assertEqual(call.call_count, 1)
        record = json.loads((self.root / "budget.json").read_text())["reservations"][0]
        self.assertEqual(
            (record["status"], record["http_status"], record["input_tokens"]),
            ("FAILED", 503, None),
        )
        self.assertEqual(
            (self.root / "bridge/http-response-1.raw").read_bytes(), b"offline"
        )

    def test_malformed_http_body_is_logged_without_retry(self):
        class Response:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def read(self, *args):
                return b"not-json"

        self.submit()
        with patch("trusted_slot.real_model.urllib.request.build_opener") as opener:
            opener.return_value.open.return_value = Response()
            self.exchange.poll()
            self.assertEqual(opener.return_value.open.call_count, 1)
        self.assertEqual(
            (self.root / "bridge/http-response-1.raw").read_bytes(), b"not-json"
        )
        self.assertEqual(
            json.loads((self.root / "budget.json").read_text())["reservations"][0][
                "status"
            ],
            "FAILED",
        )

    def test_model_pin_rejects_absent_or_changed_digest(self):
        class Response:
            def __init__(self, body):
                self.body = body

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def read(self, *args):
                return json.dumps(self.body).encode()

        def make_open(digest):
            return lambda request, timeout: Response(
                {"version": "0.34.1"}
                if request.full_url.endswith("/version")
                else {"models": [{"name": "qwen3.5:9b", "digest": digest}]}
            )

        with patch("trusted_slot.real_model.urllib.request.build_opener") as opener:
            opener.return_value.open.side_effect = make_open("wrong")
            with self.assertRaises(ValueError):
                validate_model_pin()
        with patch("trusted_slot.real_model.urllib.request.build_opener") as opener:
            opener.return_value.open.side_effect = make_open(MODEL_DIGEST)
            self.assertEqual(validate_model_pin()["digest"], MODEL_DIGEST)

    def test_controller_uds_forwards_through_host_exchange(self):
        path = str(self.root / "model.sock")
        bridge = FileBridgeModel(path, self.root)
        self.addCleanup(bridge.close)
        with patch.object(self.exchange, "transport", return_value=self.good()) as call:
            stream = socket.socket(socket.AF_UNIX)
            with stream:
                stream.connect(path)
                data = json.dumps(self.native).encode()
                stream.sendall(struct.pack(">I", len(data)) + data)
                deadline = time.monotonic() + 2
                while (
                    not list((self.root / "bridge").glob("request-*.json"))
                    and time.monotonic() < deadline
                ):
                    time.sleep(0.01)
                self.exchange.poll()
                size = struct.unpack(">I", stream.recv(4))[0]
                response = json.loads(stream.recv(size))
                self.assertEqual(response["interaction_id"], "i-1")
            self.assertEqual(call.call_count, 1)

    def test_background_exchange_does_not_block_watchdog_and_is_reaped(self):
        self.submit()
        with patch.object(
            self.exchange,
            "transport",
            side_effect=lambda *_: (time.sleep(2), self.good())[1],
        ):
            self.exchange.start()
            try:
                deadline = time.monotonic() + 1
                while (
                    time.monotonic() < deadline
                    and not json.loads((self.root / "budget.json").read_text())[
                        "reservations"
                    ]
                ):
                    time.sleep(0.01)
                self.assertTrue(
                    json.loads((self.root / "budget.json").read_text())["reservations"]
                )
                self.assertLess(time.monotonic(), deadline)
            finally:
                self.exchange.close()
        self.assertFalse(self.exchange.worker.is_alive())
        self.assertEqual(
            json.loads((self.root / "budget.json").read_text())["reservations"][0][
                "status"
            ],
            "FAILED",
        )

    def test_fixture_cannot_use_either_registered_live_identity(self):
        import run_trusted_slot

        arguments = ["launcher"]
        for name in ("project", "task-spec", "state-root", "linux-bin-dir"):
            arguments.extend(["--" + name, str(self.root)])
        arguments.extend(
            [
                "--trusted-controller-image",
                "pin",
                "--model-mode",
                "file_bridge_fixture",
            ]
        )
        for identity in ("task-t372-bits-r1", "task-t372-bits-r2"):
            for task_id, key in ((identity, "fixture"), ("fixture", identity)):
                with (
                    self.subTest(task=task_id, key=key),
                    patch.object(
                        sys,
                        "argv",
                        arguments
                        + [
                            "--task-id",
                            task_id,
                            "--idempotency-key",
                            key,
                        ],
                    ),
                    self.assertRaises(SystemExit),
                    contextlib.redirect_stderr(io.StringIO()),
                ):
                    run_trusted_slot.parse_args()

    def test_live_parse_gate_requires_exact_frozen_input_budget_and_release(self):
        import run_trusted_slot
        from trusted_slot.real_model import MODEL_DIGEST

        project = self.root / "project"
        (project / "boltons").mkdir(parents=True)
        (project / "tests").mkdir()
        source = project / "boltons/mathutils.py"
        tests = project / "tests/test_mathutils.py"
        source.write_text("historical source\n")
        tests.write_text("original tests\n")
        wheels = project / "tests/t372_wheels"
        wheels.mkdir()
        (wheels / "pytest.whl").write_bytes(b"wheel fixture")
        reference = self.root / "reference"
        (reference / "boltons").mkdir(parents=True)
        (reference / "boltons/mathutils.py").write_text("public repair\n")
        subprocess.run(["git", "init", "-q", str(project)], check=True)
        subprocess.run(["git", "-C", str(project), "add", "."], check=True)
        subprocess.run(
            [
                "git",
                "-C",
                str(project),
                "-c",
                "user.name=Test",
                "-c",
                "user.email=test@example.invalid",
                "commit",
                "-qm",
                "fixture",
            ],
            check=True,
        )
        tree = subprocess.check_output(
            ["git", "-C", str(project), "rev-parse", "HEAD^{tree}"], text=True
        ).strip()
        spec = self.root / "spec.json"
        spec.write_text("{}")
        binary = self.root / "bin"
        binary.mkdir()
        for name in ("castor", "castord"):
            (binary / name).write_bytes(b"ELF fixture")

        def hash_file(path):
            return hashlib.sha256(path.read_bytes()).hexdigest()

        protocol = {
            "task_id": "task-t372-bits-r2",
            "model": "qwen3.5:9b",
            "model_digest": MODEL_DIGEST,
            "ollama_version": "0.34.1",
            "think": False,
            "temperature": 0.2,
            "seed": 17,
            "num_ctx": 32768,
            "max_unique_http_calls": 3,
            "max_output_tokens_per_call": 512,
            "workload_timeout_seconds": 300,
            "cleanup_timeout_seconds": 60,
            "source_sha256": hash_file(source),
            "tests_sha256": hash_file(tests),
            "spec_sha256": hash_file(spec),
            "target_tree": tree,
            "reference_source_sha256": hash_file(reference / "boltons/mathutils.py"),
            "controller_image": run_trusted_slot.CONTROLLER,
            "pi_image": run_trusted_slot.PI,
            "verifier_image": "sha256:"
            + run_trusted_slot.VERIFIER.rsplit("sha256:", 1)[1],
            "wheel_sha256": {"pytest.whl": hash_file(wheels / "pytest.whl")},
            "runtime_source_sha256": {},
            "native_binaries_sha256": {
                name: hash_file(binary / name) for name in ("castor", "castord")
            },
        }
        protocol_file = self.root / "protocol.json"
        protocol_file.write_text(json.dumps(protocol))
        manifest_file = self.root / "source-manifest.json"
        manifest_file.write_text(
            json.dumps(
                {
                    "reference": str(reference),
                    "reference_source_sha256": hash_file(
                        reference / "boltons/mathutils.py"
                    ),
                    "target_tree": tree,
                    "source_sha256": hash_file(source),
                    "original_regression_tests_sha256": hash_file(tests),
                    "wheel_sha256": protocol["wheel_sha256"],
                }
            )
        )
        acceptance_file = self.root / "acceptance.json"
        acceptance_file.write_text(
            json.dumps(
                {
                    "accepted": True,
                    "scopes": ["model_bridge", "live_gate"],
                    "protocol_sha256": hash_file(protocol_file),
                }
            )
        )
        state = self.root / "state"
        release = {
            "task_id": "task-t372-bits-r2",
            "protocol_sha256": hash_file(protocol_file),
            "model_digest": MODEL_DIGEST,
            "target_tree": tree,
            "spec_sha256": hash_file(spec),
            "state_root": str(state.resolve()),
            "workload_timeout_seconds": 300,
            "cleanup_timeout_seconds": 60,
            "max_unique_http_calls": 3,
            "max_output_tokens_per_call": 512,
            "budget_approved": True,
            "root_released": True,
        }
        release_file = self.root / "release.json"
        release_file.write_text(json.dumps(release))
        arguments = [
            "launcher",
            "--project",
            str(project),
            "--task-spec",
            str(spec),
            "--task-id",
            "task-t372-bits-r2",
            "--idempotency-key",
            "task-t372-bits-r2",
            "--state-root",
            str(state),
            "--linux-bin-dir",
            str(binary),
            "--trusted-controller-image",
            "pin",
            "--model-mode",
            "ollama",
            "--protocol-file",
            str(protocol_file),
            "--release-file",
            str(release_file),
            "--source-manifest",
            str(manifest_file),
            "--acceptance-file",
            str(acceptance_file),
            "--workload-timeout",
            "300",
            "--cleanup-timeout",
            "60",
        ]
        reference_pin = hash_file(reference / "boltons/mathutils.py")
        with (
            patch.object(sys, "argv", arguments),
            patch.object(run_trusted_slot, "RUNTIME_FILES", ()),
            patch.object(run_trusted_slot, "REFERENCE_SHA256", reference_pin),
        ):
            self.assertEqual(run_trusted_slot.parse_args().model_mode, "ollama")
        # Even a newly matching approval/release hash cannot authorize altered
        # execution inputs, runtime bytes, images or budgets.
        original_protocol = protocol_file.read_bytes()
        original_acceptance = acceptance_file.read_bytes()
        original_release = release_file.read_bytes()
        retired_protocol = json.loads(original_protocol)
        retired_protocol["task_id"] = "task-t372-bits-r1"
        protocol_file.write_text(json.dumps(retired_protocol))
        for file, content in (
            (acceptance_file, original_acceptance),
            (release_file, original_release),
        ):
            receipt = json.loads(content)
            receipt["protocol_sha256"] = hash_file(protocol_file)
            if file == release_file:
                receipt["task_id"] = "task-t372-bits-r1"
            file.write_text(json.dumps(receipt))
        retired_arguments = [
            "task-t372-bits-r1" if value == "task-t372-bits-r2" else value
            for value in arguments
        ]
        with (
            patch.object(sys, "argv", retired_arguments),
            patch.object(run_trusted_slot, "RUNTIME_FILES", ()),
            patch.object(run_trusted_slot, "REFERENCE_SHA256", reference_pin),
            self.assertRaises(SystemExit),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            run_trusted_slot.parse_args()
        protocol_file.write_bytes(original_protocol)
        acceptance_file.write_bytes(original_acceptance)
        release_file.write_bytes(original_release)

        altered_pins = {
            "controller_image": "sha256:" + "0" * 64,
            "pi_image": "sha256:" + "0" * 64,
            "verifier_image": "sha256:" + "0" * 64,
            "wheel_sha256": {"pytest.whl": "0" * 64},
            "runtime_source_sha256": {"foreign.py": "0" * 64},
            "native_binaries_sha256": {"castor": "0" * 64},
            "reference_source_sha256": "0" * 55,
            "max_unique_http_calls": 4,
            "max_output_tokens_per_call": 513,
        }
        for key, altered in altered_pins.items():
            with self.subTest(pin=key):
                changed = json.loads(original_protocol)
                changed[key] = altered
                protocol_file.write_text(json.dumps(changed))
                for file, content in (
                    (acceptance_file, original_acceptance),
                    (release_file, original_release),
                ):
                    receipt = json.loads(content)
                    receipt["protocol_sha256"] = hash_file(protocol_file)
                    file.write_text(json.dumps(receipt))
                with (
                    patch.object(sys, "argv", arguments),
                    patch.object(run_trusted_slot, "RUNTIME_FILES", ()),
                    patch.object(run_trusted_slot, "REFERENCE_SHA256", reference_pin),
                    self.assertRaises(SystemExit),
                    contextlib.redirect_stderr(io.StringIO()),
                ):
                    run_trusted_slot.parse_args()
        protocol_file.write_bytes(original_protocol)
        acceptance_file.write_bytes(original_acceptance)
        release_file.write_bytes(original_release)

        with (
            patch.object(
                sys, "argv", arguments[:-3] + ["299", "--cleanup-timeout", "60"]
            ),
            patch.object(run_trusted_slot, "RUNTIME_FILES", ()),
            patch.object(run_trusted_slot, "REFERENCE_SHA256", reference_pin),
            self.assertRaises(SystemExit),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            run_trusted_slot.parse_args()
        source.write_text("changed source\n")
        with (
            patch.object(sys, "argv", arguments),
            patch.object(run_trusted_slot, "RUNTIME_FILES", ()),
            patch.object(run_trusted_slot, "REFERENCE_SHA256", reference_pin),
            self.assertRaises(SystemExit),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            run_trusted_slot.parse_args()

    def test_bits_mock_requires_real_source_read_before_edit(self):
        first = mock_reply(
            {"interaction_id": "i-1", "request": {"schema_version": 1}},
            "bits_fixture",
            0,
        )
        call = json.loads(bytes(first["content"]))["content"][0]
        self.assertEqual(
            (call["name"], call["arguments"]["path"]),
            ("castor_read_file", "boltons/mathutils.py"),
        )
        observation = {
            "role": "toolResult",
            "toolCallId": "read-1",
            "toolName": "castor_read_file",
            "isError": False,
            "content": [{"type": "text", "text": "wrong content"}],
        }
        with self.assertRaises(ValueError):
            mock_reply(
                {
                    "interaction_id": "i-2",
                    "request": {"schema_version": 1, "messages": [observation]},
                },
                "bits_fixture",
                1,
            )
        observation["content"][0]["text"] = (
            "class Bits:\n"
            "    def __getitem__(self, k):\n"
            "        if type(k) is int:\n"
            "            if k >= self.len:\n"
            "                raise IndexError(k)\n"
        )
        second = mock_reply(
            {
                "interaction_id": "i-2",
                "request": {"schema_version": 1, "messages": [observation]},
            },
            "bits_fixture",
            1,
        )
        edit = json.loads(bytes(second["content"]))["content"][0]
        self.assertEqual(
            (edit["name"], edit["arguments"]["path"]),
            ("castor_edit_file", "boltons/mathutils.py"),
        )


if __name__ == "__main__":
    unittest.main()

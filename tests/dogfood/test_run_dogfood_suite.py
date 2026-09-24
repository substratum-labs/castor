from __future__ import annotations

import copy
import json
import subprocess
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

import jsonschema
from tests.dogfood.run_dogfood_suite import (
    TRACE_SPECS,
    DogfoodSuiteRunner,
    LocalFilesystem,
    RunPins,
    SuiteError,
    SuitePhase,
    canonical_bundle_sha256,
    redact_secrets,
)

DOGFOOD_DIR = Path(__file__).resolve().parent
SCHEMA_PATH = DOGFOOD_DIR / "dogfood_bundle.schema.json"
FIXTURE_PATH = DOGFOOD_DIR / "fixtures" / "minimal_valid_bundle.json"


class FakeGit:
    def __init__(self, status: str = "", error: BaseException | None = None) -> None:
        self.status = status
        self.error = error
        self.created: list[tuple[str, Path, str]] = []

    def create_worktree(self, commit: str, path: Path, branch: str) -> None:
        self.created.append((commit, path, branch))
        if self.error is not None:
            raise self.error
        path.mkdir(parents=True)

    def status_porcelain(self, _path: Path) -> str:
        return self.status


class FakeProcess:
    def __init__(self, returncodes: tuple[int, ...] = ()) -> None:
        self.returncodes = list(returncodes)
        self.calls: list[tuple[tuple[str, ...], Path]] = []

    def run(self, argv: tuple[str, ...], cwd: Path) -> subprocess.CompletedProcess[str]:
        self.calls.append((argv, cwd))
        returncode = self.returncodes.pop(0) if self.returncodes else 0
        return subprocess.CompletedProcess(
            argv,
            returncode,
            stdout=f"stdout token sk-proj-{'x' * 24}",
            stderr="Authorization: Bearer abc.def.ghi",
        )


class FakeDocker:
    def __init__(self) -> None:
        self.stop_calls: list[str] = []

    def stop_exact(self, container_id: str) -> None:
        self.stop_calls.append(container_id)


class FakeClock:
    def now_iso8601(self) -> str:
        return "2026-09-10T12:34:56Z"


class FakeSocket:
    def probe(self, _path: Path) -> bool:
        return True


class RecordingFilesystem(LocalFilesystem):
    def __init__(self) -> None:
        self.writes: list[Path] = []

    def write_text_atomic(self, path: Path, text: str) -> None:
        self.writes.append(path)
        super().write_text_atomic(path, text)


class DogfoodBundleSchemaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        cls.bundle = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
        cls.validator_class = jsonschema.validators.validator_for(cls.schema)

    def test_schema_and_minimal_fixture_are_valid(self) -> None:
        self.validator_class.check_schema(self.schema)
        self.validator_class(self.schema).validate(self.bundle)
        self.assertEqual(
            self.bundle["bundle_sha256"],
            "6f8df8bf8495197123fb6625873915e45e6c608d3d250c620087e1837476ac08",
        )

    def test_unknown_fields_are_rejected_at_nested_boundaries(self) -> None:
        cases = (
            ((), "unexpected_top"),
            (("provenance",), "unexpected_provenance"),
            (("traces", "N1", "target_audit"), "unexpected_audit"),
        )
        for path, field in cases:
            with self.subTest(path=path):
                candidate = copy.deepcopy(self.bundle)
                target = candidate
                for key in path:
                    target = target[key]
                target[field] = True
                with self.assertRaises(jsonschema.ValidationError):
                    self.validator_class(self.schema).validate(candidate)

    def test_ring3_egress_and_out_of_scope_targets_are_rejected(self) -> None:
        for field, value in (
            ("network_egress_bytes", 1),
            ("target_files_touched", ["pyproject.toml"]),
        ):
            with self.subTest(field=field):
                candidate = copy.deepcopy(self.bundle)
                candidate["traces"]["H1"][field] = value
                with self.assertRaises(jsonschema.ValidationError):
                    self.validator_class(self.schema).validate(candidate)

    def test_trace_keys_are_bound_to_identity_and_frozen_fault_hook(self) -> None:
        cases = (
            ("C1", "trace_id", "C2"),
            ("C2", "fault_hook", "before_delivery_append"),
        )
        for trace_id, field, value in cases:
            with self.subTest(trace_id=trace_id, field=field):
                candidate = copy.deepcopy(self.bundle)
                if field == "fault_hook":
                    candidate["traces"][trace_id]["fault_proof"][field] = value
                else:
                    candidate["traces"][trace_id][field] = value
                with self.assertRaises(jsonschema.ValidationError):
                    self.validator_class(self.schema).validate(candidate)


class DogfoodSuiteRunnerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(dir="/tmp")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.git = FakeGit()
        self.process = FakeProcess()
        self.docker = FakeDocker()
        self.clock = FakeClock()
        self.socket = FakeSocket()
        self.filesystem = RecordingFilesystem()
        self.pins = RunPins(
            implementation_commit="1" * 64,
            castord_binary_sha256="2" * 64,
            agent_image_digest="sha256:" + "3" * 64,
            model_image_digest="sha256:" + "4" * 64,
            bundle_schema_sha256="5" * 64,
        )
        ids = iter(("run-a", "run-b", "run-c"))
        self.runner = DogfoodSuiteRunner(
            root=self.root,
            parent_commit="0" * 64,
            pins=self.pins,
            git=self.git,
            process=self.process,
            docker=self.docker,
            clock=self.clock,
            sockets=self.socket,
            filesystem=self.filesystem,
            id_factory=lambda: next(ids),
        )

    def test_prepare_creates_unique_disjoint_paths_and_markers(self) -> None:
        first = self.runner.prepare()
        second_runner = replace(self.runner, id_factory=lambda: "run-b")
        second = second_runner.prepare()

        self.assertNotEqual(first.harness_dir, second.harness_dir)
        self.assertNotEqual(first.target_dir, second.target_dir)
        self.assertNotEqual(first.harness_dir, first.target_dir)
        self.assertEqual((first.harness_dir / "run_id").read_text(), "run-a\n")
        self.assertEqual(
            (first.harness_dir / "parent_commit").read_text(), "0" * 64 + "\n"
        )
        self.assertEqual(self.git.created[0][2], "dogfood/run-a")
        self.assertTrue(self.filesystem.writes)

    def test_dirty_target_refusal_preserves_workspace(self) -> None:
        self.git.status = "?? hostile.txt\n"
        with self.assertRaisesRegex(SuiteError, "dirty"):
            self.runner.prepare()

        harness = self.root / "harness" / "run-a"
        target = self.root / "targets" / "run-a"
        self.assertTrue(harness.is_dir())
        self.assertTrue(target.is_dir())
        self.assertTrue((harness / "failure.json").is_file())
        self.assertEqual(self.docker.stop_calls, [])

    def test_worktree_creation_error_preserves_harness_failure_record(self) -> None:
        self.git.error = RuntimeError("git worktree failed")
        with self.assertRaisesRegex(SuiteError, "worktree"):
            self.runner.prepare()

        harness = self.root / "harness" / "run-a"
        failure = json.loads((harness / "failure.json").read_text())
        self.assertIn("git worktree failed", failure["reason"])
        self.assertTrue(failure["preserved"])

    def test_phase_transitions_are_ordered_and_persisted(self) -> None:
        paths = self.runner.prepare()
        self.assertEqual(self.runner.phase, SuitePhase.PREFLIGHT)
        with self.assertRaisesRegex(SuiteError, "phase"):
            self.runner.transition(SuitePhase.BASELINE_FROZEN)

        self.runner.transition(SuitePhase.PREREQUISITES)
        state = json.loads((paths.harness_dir / "state.json").read_text())
        self.assertEqual(state["phase"], "prerequisites")

    def test_prerequisite_failure_stops_and_preserves_redacted_logs(self) -> None:
        self.process.returncodes = [0, 9]
        paths = self.runner.prepare()
        with self.assertRaisesRegex(SuiteError, "prerequisite"):
            self.runner.run_prerequisites((("check-one",), ("check-two",)))

        self.assertEqual(len(self.process.calls), 2)
        evidence = (paths.harness_dir / "prerequisites.json").read_text()
        self.assertNotIn("sk-proj-", evidence)
        self.assertNotIn("abc.def.ghi", evidence)
        self.assertIn("[REDACTED]", evidence)
        self.assertEqual(self.runner.phase, SuitePhase.PREFLIGHT)
        self.assertEqual(self.docker.stop_calls, [])

    def test_exact_resource_tracking_drives_report_only_cleanup(self) -> None:
        paths = self.runner.prepare()
        self.runner.record_process_id(4312)
        self.runner.record_container_id("a" * 64)
        with self.assertRaises(SuiteError):
            self.runner.record_container_id("$(docker ps -q)")

        for phase in (
            SuitePhase.PREREQUISITES,
            SuitePhase.BASELINE_FROZEN,
            SuitePhase.MODEL_COMPLETE,
            SuitePhase.TRACES_COMPLETE,
            SuitePhase.BUNDLE_VALIDATED,
        ):
            self.runner.transition(phase)
        paths.evidence_bundle.write_text(FIXTURE_PATH.read_text(), encoding="utf-8")
        commands = self.runner.cleanup_commands()

        self.assertEqual(
            commands,
            (
                ("kill", "-TERM", "--", "4312"),
                ("docker", "stop", "a" * 64),
                ("docker", "rm", "a" * 64),
                ("git", "worktree", "remove", "--", str(paths.target_dir)),
            ),
        )
        self.assertEqual(self.docker.stop_calls, [])
        self.assertTrue(paths.harness_dir.exists())

    def test_cleanup_requires_matching_markers_and_preserved_evidence(self) -> None:
        paths = self.runner.prepare()
        for phase in (
            SuitePhase.PREREQUISITES,
            SuitePhase.BASELINE_FROZEN,
            SuitePhase.MODEL_COMPLETE,
            SuitePhase.TRACES_COMPLETE,
            SuitePhase.BUNDLE_VALIDATED,
        ):
            self.runner.transition(phase)
        with self.assertRaisesRegex(SuiteError, "evidence"):
            self.runner.cleanup_commands()

        paths.evidence_bundle.write_text("not JSON\n", encoding="utf-8")
        with self.assertRaisesRegex(SuiteError, "evidence"):
            self.runner.cleanup_commands()

        paths.evidence_bundle.write_text(FIXTURE_PATH.read_text(), encoding="utf-8")
        self.git.status = " M src/castor/ipc_client.py\n"
        with self.assertRaisesRegex(SuiteError, "dirty"):
            self.runner.cleanup_commands()

        self.git.status = ""
        (paths.harness_dir / "run_id").write_text("another-run\n", encoding="utf-8")
        with self.assertRaisesRegex(SuiteError, "marker"):
            self.runner.cleanup_commands()

    def test_resume_fails_closed_when_any_pin_changes(self) -> None:
        paths = self.runner.prepare()
        resumed = DogfoodSuiteRunner.resume(
            paths.harness_dir,
            pins=self.pins,
            git=self.git,
            process=self.process,
            docker=self.docker,
            clock=self.clock,
            sockets=self.socket,
            filesystem=self.filesystem,
        )
        self.assertEqual(resumed.phase, SuitePhase.PREFLIGHT)

        mismatched = replace(self.pins, castord_binary_sha256="9" * 64)
        with self.assertRaisesRegex(SuiteError, "pin"):
            DogfoodSuiteRunner.resume(
                paths.harness_dir,
                pins=mismatched,
                git=self.git,
                process=self.process,
                docker=self.docker,
                clock=self.clock,
                sockets=self.socket,
                filesystem=self.filesystem,
            )

    def test_resume_rejects_manifest_path_escape(self) -> None:
        paths = self.runner.prepare()
        manifest_path = paths.harness_dir / "run.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["target_dir"] = "/tmp/not-this-run"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

        with self.assertRaisesRegex(SuiteError, "path"):
            DogfoodSuiteRunner.resume(
                paths.harness_dir,
                pins=self.pins,
                git=self.git,
                process=self.process,
                docker=self.docker,
                clock=self.clock,
                sockets=self.socket,
                filesystem=self.filesystem,
            )

    def test_trace_orchestration_uses_frozen_order_and_fault_seams(self) -> None:
        self.runner.prepare()
        for phase in (
            SuitePhase.PREREQUISITES,
            SuitePhase.BASELINE_FROZEN,
            SuitePhase.MODEL_COMPLETE,
        ):
            self.runner.transition(phase)
        seen: list[tuple[str, str | None, int, int]] = []

        def execute(spec, _paths):
            seen.append(
                (
                    spec.trace_id,
                    spec.fault_hook,
                    spec.file_action_count,
                    spec.command_action_count,
                )
            )
            trace = copy.deepcopy(
                json.loads(FIXTURE_PATH.read_text())["traces"][spec.trace_id]
            )
            trace["fault_proof"]["fault_hook"] = spec.fault_hook
            return trace

        traces = self.runner.run_traces(execute)

        self.assertEqual(list(traces), ["N1", "C1", "C2", "C3", "D1", "H1"])
        self.assertEqual(
            seen,
            [
                ("N1", None, 2, 6),
                ("C1", "before_delivery_append", 1, 0),
                ("C2", "after_delivery_fsync_before_response", 1, 0),
                ("C3", "after_file_replace", 1, 0),
                ("D1", None, 1, 0),
                ("H1", None, 0, 0),
            ],
        )
        self.assertEqual(self.runner.phase, SuitePhase.TRACES_COMPLETE)
        hostile_prompt = TRACE_SPECS[-1].prompt_injection
        self.assertIsNotNone(hostile_prompt)
        self.assertIn("external IP", hostile_prompt)
        self.assertIn("pyproject.toml", hostile_prompt)
        self.assertIn("control.sock", hostile_prompt)

    def test_trace_fails_closed_when_fault_boundary_is_unproven(self) -> None:
        self.runner.prepare()
        for phase in (
            SuitePhase.PREREQUISITES,
            SuitePhase.BASELINE_FROZEN,
            SuitePhase.MODEL_COMPLETE,
        ):
            self.runner.transition(phase)

        def execute(spec, _paths):
            trace = copy.deepcopy(
                json.loads(FIXTURE_PATH.read_text())["traces"][spec.trace_id]
            )
            trace["fault_proof"]["fault_hook"] = spec.fault_hook
            if spec.trace_id == "C2":
                trace["fault_proof"]["boundary_reached"] = False
            return trace

        with self.assertRaisesRegex(SuiteError, "C2.*boundary"):
            self.runner.run_traces(execute)
        self.assertEqual(self.runner.phase, SuitePhase.MODEL_COMPLETE)

    def test_pass_bundle_refuses_missing_artifact_and_bad_predicate(self) -> None:
        bundle = json.loads(FIXTURE_PATH.read_text())
        missing = copy.deepcopy(bundle)
        missing["raw_artifacts"] = []
        with self.assertRaisesRegex(SuiteError, "artifact"):
            DogfoodSuiteRunner.validate_bundle_semantics(missing)

        failed_gate = copy.deepcopy(bundle)
        failed_gate["predicates"]["hostile"]["pass"] = False
        failed_gate["predicates"]["hostile"]["failed_predicates"] = ["zero_egress"]
        with self.assertRaisesRegex(SuiteError, "PASS"):
            DogfoodSuiteRunner.validate_bundle_semantics(failed_gate)

    def test_canonical_bundle_hash_omits_only_digest_field(self) -> None:
        bundle = json.loads(FIXTURE_PATH.read_text())
        self.assertEqual(
            canonical_bundle_sha256(bundle),
            "6f8df8bf8495197123fb6625873915e45e6c608d3d250c620087e1837476ac08",
        )
        changed_digest_only = dict(bundle, bundle_sha256="f" * 64)
        self.assertEqual(
            canonical_bundle_sha256(changed_digest_only), bundle["bundle_sha256"]
        )

    def test_secret_redaction_is_recursive_and_preserves_safe_values(self) -> None:
        value = {
            "OPENAI_API_KEY": "sk-proj-" + "x" * 24,
            "log": "Authorization: Bearer abc.def.ghi",
            "nested": ["safe", {"password": "hunter2"}],
            "input_tokens": 7,
        }
        redacted = redact_secrets(value)
        self.assertEqual(redacted["nested"][0], "safe")
        self.assertEqual(redacted["input_tokens"], 7)
        self.assertEqual(redacted["OPENAI_API_KEY"], "[REDACTED]")
        self.assertEqual(redacted["nested"][1]["password"], "[REDACTED]")
        self.assertNotIn("abc.def.ghi", redacted["log"])

    def test_bundle_redaction_precedes_hashing_and_persistence(self) -> None:
        paths = self.runner.prepare()
        for phase in (
            SuitePhase.PREREQUISITES,
            SuitePhase.BASELINE_FROZEN,
            SuitePhase.MODEL_COMPLETE,
            SuitePhase.TRACES_COMPLETE,
        ):
            self.runner.transition(phase)
        fixture = json.loads(FIXTURE_PATH.read_text())
        artifacts = copy.deepcopy(fixture["raw_artifacts"])
        artifacts[0]["name"] = "Authorization: Bearer abc.def.ghi"

        bundle = self.runner.assemble_and_validate_bundle(
            provenance=fixture["provenance"],
            model_run=fixture["model_run"],
            traces=fixture["traces"],
            predicates=fixture["predicates"],
            raw_artifacts=artifacts,
            schema_path=SCHEMA_PATH,
        )

        persisted = json.loads(paths.evidence_bundle.read_text())
        self.assertEqual(persisted, bundle)
        self.assertNotIn("abc.def.ghi", json.dumps(persisted))
        self.assertEqual(persisted["bundle_sha256"], canonical_bundle_sha256(persisted))
        DogfoodSuiteRunner.validate_bundle_semantics(persisted)

    def test_bundle_validation_error_is_recorded_and_preserved(self) -> None:
        paths = self.runner.prepare()
        for phase in (
            SuitePhase.PREREQUISITES,
            SuitePhase.BASELINE_FROZEN,
            SuitePhase.MODEL_COMPLETE,
            SuitePhase.TRACES_COMPLETE,
        ):
            self.runner.transition(phase)
        fixture = json.loads(FIXTURE_PATH.read_text())
        invalid_provenance = dict(fixture["provenance"], unknown=True)

        with self.assertRaisesRegex(SuiteError, "bundle validation"):
            self.runner.assemble_and_validate_bundle(
                provenance=invalid_provenance,
                model_run=fixture["model_run"],
                traces=fixture["traces"],
                predicates=fixture["predicates"],
                raw_artifacts=fixture["raw_artifacts"],
                schema_path=SCHEMA_PATH,
            )

        self.assertEqual(self.runner.phase, SuitePhase.TRACES_COMPLETE)
        self.assertTrue((paths.harness_dir / "failure.json").is_file())
        self.assertTrue(paths.harness_dir.is_dir())


if __name__ == "__main__":
    unittest.main()

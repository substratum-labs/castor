"""Build-only release archive contract; no installed Python dependency."""
import hashlib
import io
import json
import pathlib
import struct
import subprocess
import sys
import tarfile
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
PACKAGER = ROOT / "scripts/package_one_shot.py"


def fake_elf(machine=62):
    header = bytearray(64)
    header[:4] = b"\x7fELF"
    header[4] = 2
    header[5] = 1
    struct.pack_into("<H", header, 18, machine)
    return bytes(header)


def pin(name, char):
    return {"reference": f"localhost:5000/castor/{name}@sha256:{char * 64}"}


def pins():
    platform = {"controller": pin("controller", "a"),
                "carrier": pin("carrier", "b"),
                "verifier": pin("verifier", "c"),
                "carrier_tag": "substratum/castor-pi-carrier:one-shot-0.1.0"}
    return {"linux/amd64": platform, "linux/arm64": platform}


class PackageOneShotTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = pathlib.Path(self.tmp.name)
        self.binary = self.root / "castor"
        self.binary.write_bytes(fake_elf())
        self.pin_file = self.root / "pins.json"
        self.pin_file.write_text(json.dumps(pins()))
        self.archive = self.root / "castor-one-shot-0.1.0-linux-amd64.tar.gz"

    def run_packager(self, archive=None):
        return subprocess.run([sys.executable, str(PACKAGER),
                               "--version", "0.1.0", "--source-revision", "d" * 40,
                               "--host-bin", str(self.binary),
                               "--engine-pins", str(self.pin_file),
                               "--output", str(archive or self.archive)],
                              cwd=ROOT, capture_output=True, text=True)

    def test_archive_has_exact_members_hashes_and_repeatable_bytes(self):
        result = self.run_packager()
        self.assertEqual(result.returncode, 0, result.stderr)
        with tarfile.open(self.archive, "r:gz") as package:
            names = sorted(package.getnames())
            self.assertEqual(names, sorted(["bin/castor", "libexec/castor/model_pin.mjs",
                "libexec/castor/ollama_model_adapter.mjs", "share/castor/release.json",
                "README.md", "LICENSE"]))
            manifest = json.load(io.TextIOWrapper(package.extractfile("share/castor/release.json")))
            self.assertEqual(manifest["source_revision"], "d" * 40)
            self.assertEqual(manifest["host"]["os"], "linux")
            self.assertEqual(manifest["host"]["arch"], "amd64")
            self.assertEqual(manifest["host"]["castor_sha256"], hashlib.sha256(fake_elf()).hexdigest())
            self.assertTrue(all(member.mtime == 0 for member in package.getmembers()))
            self.assertFalse(any(name.startswith("kernel/") for name in names))
        first = self.archive.read_bytes()
        self.assertEqual(self.run_packager().returncode, 0)
        self.assertEqual(self.archive.read_bytes(), first)

    def test_rejects_wrong_platform_or_missing_binary(self):
        self.binary.write_bytes(fake_elf(183))
        self.assertNotEqual(self.run_packager().returncode, 0)
        self.binary.unlink()
        self.assertNotEqual(self.run_packager().returncode, 0)

    def test_rejects_duplicate_and_malformed_pins(self):
        body = self.pin_file.read_text()
        self.pin_file.write_text(body.replace('"linux/amd64":', '"linux/amd64":{},"linux/amd64":', 1))
        self.assertNotEqual(self.run_packager().returncode, 0)
        self.pin_file.write_text(json.dumps({"linux/amd64": pins()["linux/amd64"]}))
        self.assertNotEqual(self.run_packager().returncode, 0)

    def test_rejects_filename_version_mismatch(self):
        wrong = self.root / "castor-one-shot-0.2.0-linux-amd64.tar.gz"
        self.assertNotEqual(self.run_packager(wrong).returncode, 0)


if __name__ == "__main__":
    unittest.main()

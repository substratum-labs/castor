"""Replay patch variants on disposable copies; never edit the source fixture."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile

ROOT = Path(__file__).resolve().parents[2]
ARCHIVE = ROOT / "fixtures/t337e_duration/workspace_snapshot.tar.gz"
PROBE = Path(__file__).with_name("probe-results.json")


def command(args, cwd, text=None):
    result = subprocess.run(args, cwd=cwd, input=text, text=True, capture_output=True, check=False)
    return {"exit": result.returncode, "stdout": result.stdout, "stderr": result.stderr}


def replay(patch):
    with tempfile.TemporaryDirectory(prefix="castor-t337h-") as directory:
        root = Path(directory)
        with tarfile.open(ARCHIVE) as archive:
            for member in archive.getmembers():
                path = Path(member.name)
                assert not path.is_absolute() and ".." not in path.parts
                assert member.isfile() or member.isdir()
                target = root / path
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(archive.extractfile(member).read())
        initialized = command(["git", "init", "--quiet"], root)
        assert initialized["exit"] == 0, initialized
        original = (root / "duration.py").read_bytes()
        result = {}
        if patch is not None:
            result["git_check"] = command(["git", "apply", "--check", "-"], root, patch)
            assert (root / "duration.py").read_bytes() == original
            if result["git_check"]["exit"] != 0:
                return result
            result["git_apply"] = command(["git", "apply", "-"], root, patch)
            assert result["git_apply"]["exit"] == 0, result
        result["tests"] = command([sys.executable, "-B", "-m", "unittest", "discover", "-s", "tests", "-p", "test_duration.py"], root)
        content = (root / "duration.py").read_text()
        result["output_sha256"] = hashlib.sha256(content.encode()).hexdigest()
        result["dead_return_retained"] = "return value * 86400\n        return value * 3600" in content
        return result


probe = json.loads(PROBE.read_text())
results = {"baseline": replay(None)}
for name, case in probe["patches"].items():
    results[name] = {"lint": case["lint"], **replay(case["patch"])}
assert results["baseline"]["tests"]["exit"] == 1
for name in ("raw", "newline_only", "counts_only"):
    assert results[name]["git_check"]["exit"] != 0
for name in ("counts_and_newline", "native_same_intent", "native_clean_edit"):
    assert results[name]["git_check"]["exit"] == 0
    assert results[name]["tests"]["exit"] == 0
assert results["counts_and_newline"]["dead_return_retained"]
assert results["native_same_intent"]["output_sha256"] == results["counts_and_newline"]["output_sha256"]
assert not results["native_clean_edit"]["dead_return_retained"]
if "live_structured" in results:
    assert results["live_raw"]["git_check"]["exit"] != 0
    assert results["live_raw_format_repaired"]["git_check"]["exit"] != 0
    assert results["live_structured"]["git_check"]["exit"] == 0
    assert results["live_structured"]["tests"]["exit"] == 0
    assert not results["live_structured"]["dead_return_retained"]
print(json.dumps({"kind": "offline_replay_not_end_to_end", "results": results}, indent=2))

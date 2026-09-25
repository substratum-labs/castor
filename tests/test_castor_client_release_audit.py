"""Release artifacts must remain the new client, without legacy authority code."""

from __future__ import annotations

import shutil
import tarfile
import tomllib
import zipfile
from io import BytesIO
from pathlib import Path

import pytest
from scripts.check_client_release import check_release

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "packages/castor-client"


@pytest.fixture
def candidate_dist(tmp_path: Path) -> tuple[Path, str]:
    with (PACKAGE / "pyproject.toml").open("rb") as source:
        version = tomllib.load(source)["project"]["version"]
    for name in (
        f"castor_client-{version}-py3-none-any.whl",
        f"castor_client-{version}.tar.gz",
    ):
        shutil.copy2(PACKAGE / "dist" / name, tmp_path / name)
    return tmp_path, version


def test_candidate_client_release_artifacts_are_accepted(
    candidate_dist: tuple[Path, str],
) -> None:
    dist, version = candidate_dist
    check_release(f"client-v{version}", dist)


def test_release_audit_rejects_old_tag_or_extra_distribution(
    candidate_dist: tuple[Path, str],
) -> None:
    dist, version = candidate_dist
    with pytest.raises(ValueError, match="tag must match"):
        check_release(f"v{version}", dist)
    (dist / "castor_kernel-0.6.0a1-py3-none-any.whl").write_bytes(b"old")
    with pytest.raises(ValueError, match="unexpected release artifacts"):
        check_release(f"client-v{version}", dist)


def test_release_audit_rejects_legacy_module_in_client_wheel(
    candidate_dist: tuple[Path, str],
) -> None:
    dist, version = candidate_dist
    wheel = dist / f"castor_client-{version}-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "a") as archive:
        archive.writestr("castor/core.py", "# forbidden historical authority module\n")
    with pytest.raises(ValueError, match="legacy authority module"):
        check_release(f"client-v{version}", dist)


def test_release_audit_rejects_sdist_with_wrong_package_metadata(
    candidate_dist: tuple[Path, str],
) -> None:
    dist, version = candidate_dist
    sdist = dist / f"castor_client-{version}.tar.gz"
    altered = dist / "altered.tar.gz"
    with tarfile.open(sdist, "r:gz") as source, tarfile.open(altered, "w:gz") as output:
        for member in source:
            data = source.extractfile(member).read() if member.isfile() else None
            if member.name.endswith("/PKG-INFO"):
                assert data is not None
                data = data.replace(b"Name: castor-client\n", b"Name: castor-kernel\n")
            if data is not None:
                member.size = len(data)
                output.addfile(member, BytesIO(data))
            else:
                output.addfile(member)
    altered.replace(sdist)
    with pytest.raises(ValueError, match="sdist metadata"):
        check_release(f"client-v{version}", dist)


def test_release_audit_rejects_legacy_module_anywhere_in_sdist(
    candidate_dist: tuple[Path, str],
) -> None:
    dist, version = candidate_dist
    sdist = dist / f"castor_client-{version}.tar.gz"
    altered = dist / "altered.tar.gz"
    with tarfile.open(sdist, "r:gz") as source, tarfile.open(altered, "w:gz") as output:
        for member in source:
            data = source.extractfile(member).read() if member.isfile() else None
            output.addfile(member, BytesIO(data) if data is not None else None)
        code = b"# forbidden historical authority module\n"
        legacy = tarfile.TarInfo(f"castor_client-{version}/tests/castor/core.py")
        legacy.size = len(code)
        output.addfile(legacy, BytesIO(code))
    altered.replace(sdist)
    with pytest.raises(ValueError, match="legacy authority module"):
        check_release(f"client-v{version}", dist)

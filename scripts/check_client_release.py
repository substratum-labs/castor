"""Fail closed if a Castor A release tag would upload the wrong distribution."""

from __future__ import annotations

import argparse
import os
import tarfile
import tomllib
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "packages/castor-client"


def check_release(tag: str, dist: Path) -> None:
    with (PACKAGE / "pyproject.toml").open("rb") as source:
        project = tomllib.load(source)["project"]
    version = project["version"]
    if project["name"] != "castor-client" or tag != f"client-v{version}":
        raise ValueError("tag must match the castor-client package name and version")

    wheel_name = f"castor_client-{version}-py3-none-any.whl"
    sdist_name = f"castor_client-{version}.tar.gz"
    artifacts = {path.name for path in dist.iterdir() if path.is_file()}
    if artifacts - {wheel_name, sdist_name, ".gitignore"} or not {
        wheel_name,
        sdist_name,
    }.issubset(artifacts):
        raise ValueError(f"unexpected release artifacts: {sorted(artifacts)}")

    with zipfile.ZipFile(dist / wheel_name) as wheel:
        wheel_entries = set(wheel.namelist())
        if "castor_client/session.py" not in wheel_entries:
            raise ValueError("client session module is absent from wheel")
        if any(
            name.startswith(("castor/", "castor_kernel/")) for name in wheel_entries
        ):
            raise ValueError("legacy authority module is present in client wheel")
        if any(name.endswith("entry_points.txt") for name in wheel_entries):
            raise ValueError("client wheel unexpectedly installs command entrypoints")
        metadata_name = f"castor_client-{version}.dist-info/METADATA"
        metadata = wheel.read(metadata_name).decode("utf-8")
        if (
            "Name: castor-client\n" not in metadata
            or f"Version: {version}\n" not in metadata
        ):
            raise ValueError("client wheel metadata does not match the tag")

    with tarfile.open(dist / sdist_name, "r:gz") as sdist:
        names = sdist.getnames()
        prefix = f"castor_client-{version}/"
        if f"{prefix}src/castor_client/session.py" not in names:
            raise ValueError("client session module is absent from sdist")
        if any(not name.startswith(prefix) for name in names):
            raise ValueError("sdist contains a file outside the client package root")
        if any(
            {"castor", "castor_kernel"}.intersection(Path(name).parts) for name in names
        ):
            raise ValueError("legacy authority module is present in client sdist")
        info_file = sdist.extractfile(f"{prefix}PKG-INFO")
        project_file = sdist.extractfile(f"{prefix}pyproject.toml")
        if info_file is None or project_file is None:
            raise ValueError("sdist metadata is missing")
        package_info = info_file.read().decode("utf-8")
        embedded_project = tomllib.loads(project_file.read().decode("utf-8"))["project"]
        if (
            "Name: castor-client\n" not in package_info
            or f"Version: {version}\n" not in package_info
            or embedded_project.get("name") != "castor-client"
            or embedded_project.get("version") != version
        ):
            raise ValueError("sdist metadata does not match the tag")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dist", type=Path, default=PACKAGE / "dist")
    args = parser.parse_args()
    check_release(os.environ.get("GITHUB_REF_NAME", ""), args.dist)


if __name__ == "__main__":
    main()

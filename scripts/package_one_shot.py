#!/usr/bin/env python3
"""Build-only, deterministic Castor one-shot host archive assembler."""
from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import pathlib
import re
import struct
import tarfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
SHA256 = re.compile(r"sha256:[0-9a-f]{64}\Z")
HEX40 = re.compile(r"[0-9a-f]{40}\Z")
VERSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9.-]*\Z")


def no_duplicate_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def binary_platform(binary: bytes) -> tuple[str, str]:
    if len(binary) >= 20 and binary[:4] == b"\x7fELF" and binary[4:6] == b"\x02\x01":
        machine = struct.unpack_from("<H", binary, 18)[0]
        if machine in (62, 183):
            return "linux", "amd64" if machine == 62 else "arm64"
    if len(binary) >= 8 and binary[:4] == b"\xcf\xfa\xed\xfe":
        cpu = struct.unpack_from("<I", binary, 4)[0]
        if cpu in (0x01000007, 0x0100000C):
            return "darwin", "amd64" if cpu == 0x01000007 else "arm64"
    raise ValueError("host binary is not a supported 64-bit Linux ELF or macOS Mach-O")


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def checked_pins(path: pathlib.Path, version: str) -> dict:
    images = json.loads(path.read_text(), object_pairs_hook=no_duplicate_pairs)
    if not isinstance(images, dict) or set(images) != {"linux/amd64", "linux/arm64"}:
        raise ValueError("engine pin file must contain both Linux architectures")
    for platform, values in images.items():
        if not isinstance(values, dict) or set(values) != {"controller", "carrier", "verifier", "carrier_tag"}:
            raise ValueError(f"invalid image pin fields for {platform}")
        if values["carrier_tag"] != f"substratum/castor-pi-carrier:one-shot-{version}":
            raise ValueError("carrier tag/version mismatch")
        for role in ("controller", "carrier", "verifier"):
            pin = values[role]
            if not isinstance(pin, dict) or set(pin) != {"reference", "image_id"}:
                raise ValueError(f"invalid {role} pin")
            reference, image_id = pin["reference"], pin["image_id"]
            if not isinstance(reference, str) or not isinstance(image_id, str):
                raise ValueError(f"invalid {role} pin types")
            if not SHA256.fullmatch(image_id) or "@" not in reference or not SHA256.fullmatch(reference.rsplit("@", 1)[1]):
                raise ValueError(f"invalid {role} pin digest")
            repo = reference.rsplit("@", 1)[0]
            if not re.fullmatch(r"[a-z0-9][a-z0-9./:_-]*", repo) or ".." in repo:
                raise ValueError(f"invalid {role} pin repository")
    return images


def add_member(archive: tarfile.TarFile, name: str, data: bytes, mode: int):
    member = tarfile.TarInfo(name)
    member.size = len(data)
    member.mode = mode
    member.mtime = 0
    member.uid = member.gid = 0
    member.uname = member.gname = ""
    archive.addfile(member, io.BytesIO(data))


def assemble(version: str, source_revision: str, host_bin: pathlib.Path,
             engine_pins: pathlib.Path, output: pathlib.Path) -> dict:
    if not VERSION.fullmatch(version) or ".." in version or not HEX40.fullmatch(source_revision):
        raise ValueError("invalid release version or source revision")
    binary = host_bin.read_bytes()
    if len(binary) > 128 * 1024 * 1024:
        raise ValueError("host binary exceeds release limit")
    os_name, arch = binary_platform(binary)
    expected_name = f"castor-one-shot-{version}-{os_name}-{arch}.tar.gz"
    if output.name != expected_name:
        raise ValueError(f"output filename must be {expected_name}")
    images = checked_pins(engine_pins, version)
    model_pin = (ROOT / "kernel/carrier/pi/host/model_pin.mjs").read_bytes()
    adapter = (ROOT / "kernel/carrier/pi/host/ollama_model_adapter.mjs").read_bytes()
    if len(model_pin) > 4 * 1024 * 1024 or len(adapter) > 4 * 1024 * 1024:
        raise ValueError("host script exceeds release limit")
    manifest = {
        "schema_version": 1,
        "release_version": version,
        "source_revision": source_revision,
        "host": {"os": os_name, "arch": arch,
                 "castor_sha256": sha(binary), "model_pin_sha256": sha(model_pin),
                 "adapter_sha256": sha(adapter)},
        "images": images,
    }
    manifest_bytes = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
    members = {
        "bin/castor": (binary, 0o755),
        "libexec/castor/model_pin.mjs": (model_pin, 0o644),
        "libexec/castor/ollama_model_adapter.mjs": (adapter, 0o644),
        "share/castor/release.json": (manifest_bytes, 0o644),
        "README.md": ((ROOT / "docs/one-shot-install.md").read_bytes(), 0o644),
        "LICENSE": ((ROOT / "LICENSE").read_bytes(), 0o644),
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("wb") as raw:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as compressed:
            with tarfile.open(fileobj=compressed, mode="w", format=tarfile.USTAR_FORMAT) as archive:
                for name, (data, mode) in sorted(members.items()):
                    add_member(archive, name, data, mode)
    return {"archive": str(output), "archive_sha256": sha(output.read_bytes()),
            "host_platform": f"{os_name}-{arch}", "members": sorted(members),
            "release_version": version, "source_revision": source_revision}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--version", required=True)
    parser.add_argument("--source-revision", required=True)
    parser.add_argument("--host-bin", required=True, type=pathlib.Path)
    parser.add_argument("--engine-pins", required=True, type=pathlib.Path)
    parser.add_argument("--output", required=True, type=pathlib.Path)
    args = parser.parse_args()
    result = assemble(args.version, args.source_revision, args.host_bin, args.engine_pins, args.output)
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()

"""Packaging configuration regressions."""

from __future__ import annotations

import tomllib
from pathlib import Path


def test_mnemos_source_is_portable_for_ci() -> None:
    """Optional Mnemos resolution must not require a sibling checkout."""
    pyproject = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
    source = pyproject["tool"]["uv"]["sources"]["mnemos-engine"]

    assert "git" in source
    assert "path" not in source
    assert source["rev"]

# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Karl Toussaint (kt2saint)
"""Package-resource ownership contracts for shipped assets and skills."""

from __future__ import annotations

import importlib.resources
import tomllib
from pathlib import Path


def test_resource_namespaces_are_declared_to_setuptools():
    """Wheel resources must be formal packages, not incidental directories."""
    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    config = tomllib.loads(pyproject.read_text(encoding="utf-8"))

    assert config["tool"]["setuptools"]["packages"] == [
        "braincell",
        "braincell.assets",
        "braincell.skills",
    ]


def test_packaged_assets_and_skills_are_readable_through_resource_namespaces():
    """The resource API used after installation can see both shipped families."""
    assert importlib.resources.files("braincell.assets").joinpath("braincell.svg").is_file()
    assert (
        importlib.resources.files("braincell.skills")
        .joinpath("braincell-init", "SKILL.md")
        .is_file()
    )

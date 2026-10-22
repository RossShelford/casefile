"""The version is written once, in casefile/__init__.py; the desktop app must ship the same one."""

import json
import re
from pathlib import Path

import pytest

import casefile

REPO = Path(__file__).resolve().parents[1]


def test_version_is_a_plain_release_number():
    assert re.fullmatch(r"\d+\.\d+\.\d+", casefile.__version__)


def test_desktop_app_version_matches_the_engine():
    package = json.loads((REPO / "desktop" / "package.json").read_text(encoding="utf-8"))
    assert package["version"] == casefile.__version__


def test_pyproject_takes_the_version_from_the_package():
    tomllib = pytest.importorskip("tomllib")  # standard library from Python 3.11
    project = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
    assert "version" not in project["project"]
    assert "version" in project["project"]["dynamic"]
    assert project["tool"]["setuptools"]["dynamic"]["version"] == {"attr": "casefile.__version__"}

"""The daytona extras keep the Daytona SDK on the 0.x line the backend is written for.

Daytona SDK 2.x is a breaking major: `DaytonaConfig` refuses `api_url`, the package root no
longer exports `Image`, and `daytona.common` is gone. The Daytona backend imports 0.x paths,
so an install that resolves 2.x fails on its first sandbox.
"""
import tomllib
from pathlib import Path

from packaging.requirements import Requirement
from packaging.version import Version

ROOT = Path(__file__).resolve().parents[1]


def _daytona_pins(extra: str) -> list[Requirement]:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    pins = [Requirement(line) for line in project["optional-dependencies"][extra]]
    return [pin for pin in pins if pin.name == "daytona"]


def test_daytona_extras_exclude_sdk_1_and_later():
    for extra in ("daytona", "all"):
        pins = _daytona_pins(extra)
        assert pins, extra
        # An upper bound `<X` with X <= 1: no 1.x, 2.x or later release can pass.
        assert [s for pin in pins for s in pin.specifier
                if s.operator == "<" and Version(s.version) <= Version("1")], (extra, pins)
        assert all(pin.specifier.contains("0.233.0") for pin in pins), extra

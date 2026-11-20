"""The daytona extras keep the Daytona SDK on the 0.x line the backend is written for.

Daytona SDK 2.x is a breaking major: `DaytonaConfig` refuses `api_url`, the package root no
longer exports `Image`, and `daytona.common` is gone. The Daytona backend imports 0.x paths,
so an install that resolves 2.x fails on its first sandbox.
"""
import tomllib
from pathlib import Path

from packaging.requirements import Requirement

ROOT = Path(__file__).resolve().parents[1]


def _daytona_pins(extra: str) -> list[Requirement]:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    pins = [Requirement(line) for line in project["optional-dependencies"][extra]]
    return [pin for pin in pins if pin.name == "daytona"]


def test_daytona_extras_exclude_sdk_2():
    for extra in ("daytona", "all"):
        pins = _daytona_pins(extra)
        assert pins, extra
        for version in ("1.0.0", "2.0.0", "2.1.0"):
            assert not all(pin.specifier.contains(version) for pin in pins), (extra, version)
        assert all(pin.specifier.contains("0.233.0") for pin in pins), extra

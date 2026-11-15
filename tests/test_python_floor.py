"""The Python floor is stated in four places that nothing else connects.

The manifest refuses an install below it, the README tells a plugin user which
`python3` the hooks need, CI's matrix decides which interpreter the suite is
seen to pass on, and ruff's target decides which syntax lint accepts. Raising
one alone leaves a promise nothing tests, or a test of nothing promised.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _minor(version: str) -> tuple[int, int]:
    major, minor = version.split(".")[:2]
    return int(major), int(minor)


def test_the_readme_ci_and_ruff_state_the_manifests_python_floor() -> None:
    manifest = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    floors = re.findall(r'^requires-python = ">=(\d+\.\d+(?:\.\d+)?)"$', manifest, re.M)
    assert len(floors) == 1, floors
    floor = floors[0]

    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert re.findall(r"The hooks need `python3` ([\d.]+)\+", readme) == [floor]

    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    matrices = re.findall(r"^\s+python: \[([^\]]+)\]$", ci, re.M)
    assert len(matrices) == 1, matrices
    tested = [v.strip().strip('"') for v in matrices[0].split(",")]
    # The lowest interpreter CI runs is the floor's minor: setup-python resolves
    # "3.11" to its newest patch, which is at or above any patch-level floor.
    assert min(map(_minor, tested)) == _minor(floor), tested
    # Positive control on the parse: the matrix also tests something newer.
    assert max(map(_minor, tested)) > _minor(floor), tested

    major, minor = _minor(floor)
    assert re.findall(r'^target-version = "(py\d+)"$', manifest, re.M) == [f"py{major}{minor}"]

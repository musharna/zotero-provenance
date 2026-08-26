"""Shared fixtures."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

PLUGIN_ROOT = Path(__file__).resolve().parents[1]


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "live: needs live Zotero API access (deselect with -m 'not live')",
    )


@pytest.fixture(autouse=True)
def _installed_version_matches(monkeypatch):
    """Decouple the suite from whatever plugin version this machine installed.

    capture_message asks the staleness guard, first thing, whether the running
    version matches the INSTALLED one — and `installed_version()` reads a real
    manifest under ~/.claude. So the suite quietly depended on the developer's
    plugin install: it passed only while the repo and the installed clone
    happened to hold the same number, and bumping the version to 0.12.0 turned
    32 unrelated capture tests red at once.

    That coupling is worth naming rather than patching per-test. A unit test
    must not care what is deployed on the machine running it; the guard's own
    behaviour is exercised deliberately in test_stale_guard.py and
    test_audit_eight_staleness.py, which patch this on purpose.

    Both modules that ask the guard are covered. cli.py started asking it too
    when triage got a staleness check, and patching only capture.py meant the
    same machine-dependence came straight back through the second import --
    three triage tests turned red on a developer whose installed version simply
    differed from the checkout.
    """
    import zotero_capture.capture as cap
    import zotero_capture.cli as cli

    monkeypatch.setattr(cap, "installed_version", lambda: cap.__version__)
    monkeypatch.setattr(cli, "installed_version", lambda: cli.__version__)


@pytest.fixture
def tmp_db(tmp_path: Path) -> Path:
    return tmp_path / "test_index.db"


@pytest.fixture
def live_zotero_creds() -> dict[str, str]:
    """Skip live tests unless RUN_LIVE_ZOTERO=1 and full credentials are present."""
    if os.environ.get("RUN_LIVE_ZOTERO") != "1":
        pytest.skip("RUN_LIVE_ZOTERO not set")
    missing = [
        v
        for v in ("ZOTERO_API_KEY", "ZOTERO_LIBRARY_ID", "ZOTERO_WEBSOURCES_COLLECTION_KEY")
        if not os.environ.get(v)
    ]
    if missing:
        pytest.skip(f"missing env: {', '.join(missing)}")
    return {
        "api_key": os.environ["ZOTERO_API_KEY"],
        "library_id": os.environ["ZOTERO_LIBRARY_ID"],
        "library_type": os.environ.get("ZOTERO_LIBRARY_TYPE", "user"),
        "collection_key": os.environ["ZOTERO_WEBSOURCES_COLLECTION_KEY"],
    }

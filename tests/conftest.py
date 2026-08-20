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

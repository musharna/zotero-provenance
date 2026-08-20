"""Config loading: identity must never fall back to a default."""

from __future__ import annotations

from pathlib import Path

import pytest

from zotero_capture.config import REQUIRED_VARS, ConfigError, load_config

FULL_ENV = {
    "ZOTERO_API_KEY": "k",
    "ZOTERO_LIBRARY_ID": "12345",
    "ZOTERO_WEBSOURCES_COLLECTION_KEY": "COLLKEY",
    "HOME": "/home/someone",
}


def test_loads_when_all_required_present():
    cfg = load_config(FULL_ENV)
    assert cfg.library_id == "12345"
    assert cfg.collection_key == "COLLKEY"
    assert cfg.library_type == "user"


@pytest.mark.parametrize("missing", REQUIRED_VARS)
def test_missing_required_var_raises_and_names_it(missing):
    env = {k: v for k, v in FULL_ENV.items() if k != missing}
    with pytest.raises(ConfigError) as exc:
        load_config(env)
    assert missing in str(exc.value)


def test_library_id_has_no_default():
    """Regression guard: a default library id would write into someone else's library."""
    env = {k: v for k, v in FULL_ENV.items() if k != "ZOTERO_LIBRARY_ID"}
    with pytest.raises(ConfigError):
        load_config(env)


def test_rejects_bogus_library_type():
    with pytest.raises(ConfigError):
        load_config({**FULL_ENV, "ZOTERO_LIBRARY_TYPE": "instiution"})


def test_state_dir_follows_xdg_then_home():
    cfg = load_config({**FULL_ENV, "XDG_STATE_HOME": "/xdg/state"})
    assert cfg.db_path == Path("/xdg/state/zotero-provenance/url_index.db")
    cfg2 = load_config(FULL_ENV)
    assert cfg2.log_path == Path("/home/someone/.local/state/zotero-provenance/capture.log")


def test_explicit_state_dir_wins():
    cfg = load_config({**FULL_ENV, "ZOTERO_CAPTURE_STATE_DIR": "/tmp/zp"})
    assert cfg.queue_path == Path("/tmp/zp/retry_queue.jsonl")

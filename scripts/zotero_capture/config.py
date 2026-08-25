"""Environment-driven configuration.

Fail loud: library identity has NO default. A wrong-but-plausible default would
silently write another user's citations into whatever library happened to be
configured, which is exactly the failure this plugin exists to prevent.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

REQUIRED_VARS = (
    "ZOTERO_API_KEY",
    "ZOTERO_LIBRARY_ID",
    "ZOTERO_WEBSOURCES_COLLECTION_KEY",
)

DEFAULT_SECRETS_FILE = "~/.config/zotero-provenance/secrets.env"


class ConfigError(RuntimeError):
    """Raised when required configuration is absent or malformed."""


@dataclass(frozen=True)
class Config:
    api_key: str
    library_id: str
    library_type: str
    collection_key: str
    state_dir: Path

    @property
    def db_path(self) -> Path:
        return self.state_dir / "url_index.db"

    @property
    def log_path(self) -> Path:
        return self.state_dir / "capture.log"


def _state_dir(env: Mapping[str, str]) -> Path:
    explicit = env.get("ZOTERO_CAPTURE_STATE_DIR")
    if explicit:
        return Path(explicit).expanduser()
    xdg = env.get("XDG_STATE_HOME")
    base = Path(xdg).expanduser() if xdg else Path(env.get("HOME", "~")).expanduser() / ".local" / "state"
    return base / "zotero-provenance"


def load_config(env: Mapping[str, str] | None = None) -> Config:
    """Build a Config from the environment, or raise ConfigError naming what is missing."""
    env = os.environ if env is None else env
    missing = [name for name in REQUIRED_VARS if not env.get(name)]
    if missing:
        raise ConfigError(
            "missing required environment variable(s): "
            + ", ".join(missing)
            + f"\nSet them in your shell or in {DEFAULT_SECRETS_FILE}"
            + " (run `/zotero-setup` to create it)."
        )
    library_type = env.get("ZOTERO_LIBRARY_TYPE", "user")
    if library_type not in ("user", "group"):
        raise ConfigError(
            f"ZOTERO_LIBRARY_TYPE must be 'user' or 'group', got {library_type!r}"
        )
    return Config(
        api_key=env["ZOTERO_API_KEY"],
        library_id=env["ZOTERO_LIBRARY_ID"],
        library_type=library_type,
        collection_key=env["ZOTERO_WEBSOURCES_COLLECTION_KEY"],
        state_dir=_state_dir(env),
    )

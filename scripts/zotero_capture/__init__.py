"""zotero-provenance: record the sources your agent actually cited into Zotero."""

# Single source of truth for the version. plugin.json is what the installer keys
# on, so these two must agree — tests/test_version.py fails the build if they
# drift. Bump both together when releasing.
__version__ = "0.62.2"

# One User-Agent for every outbound request. Wikimedia's policy rejects a UA that
# carries no way to reach the operator, so the project URL is the contact point —
# deliberately not a personal email.
USER_AGENT = (
    f"zotero-provenance/{__version__} (+https://github.com/musharna/zotero-provenance)"
)

# A library must not print unless its application asked it to. Without this,
# WARNING and ERROR fall through to `logging.lastResort`, which is how the hook
# log got its diagnostic trail BY ACCIDENT -- see logging_setup. Safe to add only
# now that both applications configure explicitly: the CLIs call
# configure_cli_logging and the hook calls configure_hook_logging.
import logging as _logging  # noqa: E402

_logging.getLogger(__name__).addHandler(_logging.NullHandler())

"""zotero-provenance: record the sources your agent actually cited into Zotero."""

# Single source of truth for the version. plugin.json is what the installer keys
# on, so these two must agree — tests/test_version.py fails the build if they
# drift. Bump both together when releasing.
__version__ = "0.19.0"

# One User-Agent for every outbound request. Wikimedia's policy rejects a UA that
# carries no way to reach the operator, so the project URL is the contact point —
# deliberately not a personal email.
USER_AGENT = (
    f"zotero-provenance/{__version__} (+https://github.com/musharna/zotero-provenance)"
)

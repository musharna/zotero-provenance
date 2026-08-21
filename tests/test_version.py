"""The plugin version must have exactly one source of truth.

The install cache is keyed by the version in plugin.json, so that file is the
authority. Everything that reports a version — the package, and every outbound
User-Agent — has to derive from it rather than carry its own copy, because a
copy is a thing that silently drifts.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx

from zotero_capture import USER_AGENT, __version__
from zotero_capture.zotero_client import ZoteroClient

PLUGIN_JSON = Path(__file__).resolve().parents[1] / ".claude-plugin" / "plugin.json"


def test_package_version_matches_the_plugin_manifest():
    """Drift guard. plugin.json is what the installer keys on, so it wins."""
    manifest = json.loads(PLUGIN_JSON.read_text())
    assert __version__ == manifest["version"]


def test_user_agent_carries_the_current_version_and_a_contact_url():
    """Wikimedia's policy needs the contact URL; operators need the real version."""
    assert __version__ in USER_AGENT
    assert USER_AGENT.startswith("zotero-provenance/")
    assert "https://github.com/musharna/zotero-provenance" in USER_AGENT


def test_zotero_client_sends_the_shared_user_agent():
    """Regression: this client shipped a hardcoded 0.1 through three releases."""
    seen: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["ua"] = req.headers.get("User-Agent")
        return httpx.Response(200, json={"successful": {}, "failed": {}, "success": {}})

    with ZoteroClient(
        api_key="fake",
        library_id="0000",
        library_type="user",
        web_sources_collection_key="COLL1",
        transport=httpx.MockTransport(handler),
    ) as client:
        try:
            client.post_webpage_item(
                url_canonical="https://fixturehost.org/a",
                title="T",
                access_date="2026-08-21",
                tags=[],
            )
        except Exception:
            pass  # only the header matters here

    assert seen["ua"] == USER_AGENT


def test_title_fetcher_sends_the_shared_user_agent():
    from zotero_capture import title_fetcher

    assert title_fetcher._USER_AGENT == USER_AGENT


def test_setup_script_sends_the_shared_user_agent():
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    import zotero_setup

    assert zotero_setup._headers("k")["User-Agent"] == USER_AGENT

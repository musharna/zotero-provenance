"""End-to-end: the real Stop hook, the real CLI, against a real HTTP server.

Every unit test above stubs something. This one stubs only the remote service:
the shell script, the argument threading, the config loading, the SQLite dedup,
and the HTTP layer are all the shipping code. It is the only test that would
catch a break in how those pieces are wired to each other.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

import pytest

from conftest import PLUGIN_ROOT

STOP_HOOK = PLUGIN_ROOT / "hooks" / "capture-stop.sh"

# .invalid never resolves, so the title fetch fails instantly instead of
# reaching the network and spending its one-second budget.
CAPTURED_URL = "https://zp-e2e.invalid/paper?utm_source=news#intro"
CANONICAL_URL = "https://zp-e2e.invalid/paper"

requires_jq = pytest.mark.skipif(shutil.which("jq") is None, reason="jq not installed")


class FakeZotero(BaseHTTPRequestHandler):
    """Minimal stand-in for the parts of the Zotero API the plugin touches."""

    items: dict[str, dict] = {}
    requests: list[tuple[str, str, Any]] = []

    def log_message(self, format, *args):  # silence stderr noise
        pass

    def _body(self) -> Any:
        length = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(length)) if length else None

    def _respond(self, code: int, payload=None, headers: dict | None = None):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if payload is not None:
            self.wfile.write(json.dumps(payload).encode())

    def do_POST(self):
        body = self._body()
        type(self).requests.append(("POST", self.path, body))
        entry = body[0]
        key = f"KEY{len(type(self).items) + 1}"
        type(self).items[key] = {
            "key": key,
            "version": 1,
            "data": {**entry, "key": key},
        }
        self._respond(200, {"successful": {"0": {"key": key}}, "failed": {}})

    def do_GET(self):
        type(self).requests.append(("GET", self.path, None))
        key = self.path.rsplit("/", 1)[-1].split("?")[0]
        if key in type(self).items:
            self._respond(200, type(self).items[key], {"Last-Modified-Version": "1"})
        else:
            self._respond(200, [])

    def do_PATCH(self):
        body = self._body()
        type(self).requests.append(("PATCH", self.path, body))
        key = self.path.rsplit("/", 1)[-1]
        type(self).items[key]["data"]["tags"] = body["tags"]
        self._respond(204)


@pytest.fixture
def fake_zotero():
    FakeZotero.items = {}
    FakeZotero.requests = []
    server = HTTPServer(("127.0.0.1", 0), FakeZotero)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server, FakeZotero
    server.shutdown()
    server.server_close()


def _run_hook(
    tmp_path: Path, port: int, text: str, cwd: str
) -> subprocess.CompletedProcess:
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text(
        json.dumps(
            {
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": text}]},
            }
        )
        + "\n"
    )
    secrets = tmp_path / "secrets.env"
    secrets.write_text(
        "ZOTERO_API_KEY=testkey\n"
        "ZOTERO_LIBRARY_TYPE=user\n"
        "ZOTERO_LIBRARY_ID=testlib\n"
        "ZOTERO_WEBSOURCES_COLLECTION_KEY=COLL1\n"
        f"ZOTERO_API_BASE=http://127.0.0.1:{port}\n"
    )
    env = os.environ.copy()
    env.pop("ZOTERO_CAPTURE_DISABLE", None)
    env["ZOTERO_SECRETS_FILE"] = str(secrets)
    env["ZOTERO_CAPTURE_STATE_DIR"] = str(tmp_path / "state")
    return subprocess.run(
        ["bash", str(STOP_HOOK)],
        input=json.dumps(
            {"transcript_path": str(transcript), "session_id": "s1", "cwd": cwd}
        ),
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


@requires_jq
def test_cited_url_becomes_a_tagged_zotero_item(tmp_path: Path, fake_zotero):
    server, handler = fake_zotero
    port = server.server_address[1]

    proc = _run_hook(
        tmp_path, port, f"I found {CAPTURED_URL} useful", "/home/someone/my-thesis"
    )
    assert proc.returncode == 0, proc.stderr

    posts = [r for r in handler.requests if r[0] == "POST"]
    assert len(posts) == 1, f"expected one item POST, got {handler.requests}"
    item = posts[0][2][0]

    assert item["itemType"] == "webpage"
    assert item["url"] == CANONICAL_URL, "tracking params and fragment must be stripped"
    assert item["collections"] == ["COLL1"]

    tags = {t["tag"] for t in item["tags"]}
    assert "project:my-thesis" in tags, f"project tag missing from {tags}"
    assert "domain:zp-e2e.invalid" in tags
    assert any(t.startswith("seen:") for t in tags)
    assert any(t.startswith("context:") for t in tags)


@requires_jq
def test_same_url_cited_twice_is_retagged_not_duplicated(tmp_path: Path, fake_zotero):
    server, handler = fake_zotero
    port = server.server_address[1]

    first = _run_hook(tmp_path, port, f"see {CAPTURED_URL}", "/home/someone/my-thesis")
    assert first.returncode == 0, first.stderr
    # A different spelling of the same URL, in a later session.
    second = _run_hook(
        tmp_path, port, f"see {CANONICAL_URL}/", "/home/someone/my-thesis"
    )
    assert second.returncode == 0, second.stderr

    posts = [r for r in handler.requests if r[0] == "POST"]
    assert len(posts) == 1, (
        f"the second citation created a duplicate item; POSTs: {len(posts)}"
    )
    # The second run reached Zotero and read the item back...
    assert any(r[0] == "GET" for r in handler.requests), (
        "the second citation never reached the API at all"
    )
    # ...but had no new tag to add, so it must not spend a write.
    assert not [r for r in handler.requests if r[0] == "PATCH"], (
        "re-citing the same URL on the same day should not issue a pointless PATCH"
    )


@requires_jq
def test_recite_under_a_new_context_adds_that_tag(tmp_path: Path, fake_zotero):
    """The re-tag path: a genuinely new tag must reach the existing item."""
    server, handler = fake_zotero
    port = server.server_address[1]

    _run_hook(tmp_path, port, f"see {CAPTURED_URL}", "/home/someone/my-thesis")
    second = _run_hook(
        tmp_path,
        port,
        f"[SOURCE-CONTEXT: lit-review] see {CAPTURED_URL}",
        "/home/someone/my-thesis",
    )
    assert second.returncode == 0, second.stderr

    assert len([r for r in handler.requests if r[0] == "POST"]) == 1, (
        "no duplicate item"
    )
    patches = [r for r in handler.requests if r[0] == "PATCH"]
    assert patches, "a new context tag should have been PATCHed onto the existing item"
    patched_tags = {t["tag"] for t in patches[-1][2]["tags"]}
    assert "context:lit-review" in patched_tags
    assert "context:general" in patched_tags, "existing tags must be preserved"


@requires_jq
def test_private_urls_never_reach_the_api(tmp_path: Path, fake_zotero):
    """Positive control lives in the test above: a public URL DOES produce a POST,
    so an empty request list here means exclusion worked, not that the rig broke."""
    server, handler = fake_zotero
    port = server.server_address[1]

    proc = _run_hook(
        tmp_path,
        port,
        "internal only: http://localhost:8080/admin and http://192.168.1.5/panel "
        "and https://box.tail1234.ts.net/dash",
        "/home/someone/my-thesis",
    )
    assert proc.returncode == 0, proc.stderr
    assert not [r for r in handler.requests if r[0] == "POST"], (
        f"a private URL was sent to the API: {handler.requests}"
    )

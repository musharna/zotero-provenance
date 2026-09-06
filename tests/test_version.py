"""The plugin version must have exactly one source of truth.

The install cache is keyed by the version in plugin.json, so that file is the
authority. Everything that reports a version — the package, and every outbound
User-Agent — has to derive from it rather than carry its own copy, because a
copy is a thing that silently drifts.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import httpx

from zotero_capture import USER_AGENT, __version__
from zotero_capture.zotero_client import ZoteroClient

PLUGIN_JSON = Path(__file__).resolve().parents[1] / ".claude-plugin" / "plugin.json"
PYPROJECT = Path(__file__).resolve().parents[1] / "pyproject.toml"


def test_package_version_matches_the_plugin_manifest():
    """Drift guard. plugin.json is what the installer keys on, so it wins."""
    manifest = json.loads(PLUGIN_JSON.read_text())
    assert __version__ == manifest["version"]


def test_pyproject_version_matches_the_plugin_manifest():
    """The third copy — the one this guard did not cover, and which had drifted.

    An external audit found pyproject still advertising 0.1.0 against a 0.9.0
    plugin: eight releases stale. "Exactly one source of truth" was asserted in
    this module's own docstring while a file nobody checked kept its own copy.
    """
    manifest = json.loads(PLUGIN_JSON.read_text())
    declared = re.search(r'^version = "([^"]+)"', PYPROJECT.read_text(), re.MULTILINE)
    assert declared, "pyproject.toml has no [project] version"
    assert declared.group(1) == manifest["version"]


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


# --- outbound identity ------------------------------------------------------
#
# The three tests below replace a single line that read
#
#     assert title_fetcher._USER_AGENT == USER_AGENT
#
# and which passed, correctly, for ten days while `snapshot.py` sent a bare
# "zotero-provenance" to every page it hashed. The guard could not have caught
# it: it NAMED its subjects, one test per module, so the set it checked was the
# set of callers that existed on the day it was written. snapshot.py was written
# six days later. A hand-maintained list of call sites cannot fail on a call
# site that is not on it -- which is the same defect, in a test, that the test
# exists to prevent in the code.
#
# So these derive the set from the source instead of from memory.


def _package_sources() -> list[Path]:
    root = Path(__file__).resolve().parents[1] / "scripts"
    return sorted(root.rglob("*.py"))


def test_no_module_carries_its_own_user_agent_string():
    """Every User-Agent value must be a REFERENCE, never a literal.

    A literal is how the drift happened: two constants, both spelled
    `_USER_AGENT`, one derived from the package and one invented. This walks
    every dict in every module and fails on any "User-Agent" mapped to a string,
    so the next module to invent one fails the build the day it is written
    rather than ten days later in the live index.
    """
    import ast

    checked = 0
    offenders = []
    for path in _package_sources():
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            # A literal written straight into the header dict.
            if isinstance(node, ast.Dict):
                for key, value in zip(node.keys, node.values):
                    if not (
                        isinstance(key, ast.Constant) and key.value == "User-Agent"
                    ):
                        continue
                    checked += 1
                    if isinstance(value, ast.Constant):
                        offenders.append(
                            f"{path.name}:{value.lineno} header = {value.value!r}"
                        )
                continue
            # A literal hidden one hop away, behind a module constant. This is
            # the form the real defect took -- `_USER_AGENT = "zotero-provenance"`
            # in snapshot.py, referenced by NAME at both call sites, so a check
            # that only inspected the header dict saw a reference and passed.
            # It passed on the broken code when first written, which is why the
            # rule is stated over the ASSIGNMENT and not only over the use.
            if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant):
                for target in node.targets:
                    name = getattr(target, "id", "")
                    if "USER_AGENT" not in name.upper():
                        continue
                    checked += 1
                    # __init__.py is the source of truth and is allowed to be a
                    # literal; it is the only file that may say what we are called.
                    if path.name != "__init__.py":
                        offenders.append(
                            f"{path.name}:{node.lineno} {name} = {node.value.value!r}"
                        )

    # Positive control: a sweep that found nothing would report a clean pass
    # while checking nothing at all, which is how a vacuous check reads as
    # evidence. Assert the discovery worked before trusting what it did not find.
    assert checked >= 2, f"discovery found no User-Agent headers at all ({checked})"
    assert not offenders, "User-Agent set from a literal instead of the shared constant: " + "; ".join(offenders)


def test_every_outbound_client_is_identified_at_construction():
    """A UA passed per-request is a rule N callers must remember; a client
    default is a rule kept once. Assert every client in the package sets one."""
    import ast

    clients = 0
    unidentified = []
    for path in _package_sources():
        for node in ast.walk(ast.parse(path.read_text(), filename=str(path))):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "Client"
            ):
                continue
            clients += 1
            headers = next((kw for kw in node.keywords if kw.arg == "headers"), None)
            keys = []
            if headers is not None and isinstance(headers.value, ast.Dict):
                keys = [
                    k.value for k in headers.value.keys if isinstance(k, ast.Constant)
                ]
            if "User-Agent" not in keys:
                unidentified.append(f"{path.name}:{node.lineno}")

    assert clients >= 2, f"discovery found no httpx clients at all ({clients})"
    assert not unidentified, "httpx client built without a User-Agent default: " + "; ".join(unidentified)


def test_the_outbound_client_actually_carries_the_shared_user_agent():
    """The AST guard proves a header is written; this proves it is the right one."""
    from zotero_capture.title_fetcher import build_fetch_client

    with build_fetch_client() as client:
        assert client.headers["User-Agent"] == USER_AGENT


def test_no_fetch_path_overrides_the_clients_identity():
    """The one that fails on the old code.

    Hands every fetch path a client whose identity is a sentinel, and asserts
    the sentinel is what reaches the wire. A call site that passes its own
    User-Agent header overrides the client default, so this goes red for exactly
    the mistake that put 53 rows in the index as `blocked` -- and stays green
    only while no call site writes the header itself.
    """
    from zotero_capture.snapshot import hash_page, page_is_visible
    from zotero_capture.title_fetcher import fetch_title

    SENTINEL = "sentinel-agent/1.0 (+https://example.invalid/contact)"
    seen: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req.headers.get("User-Agent", ""))
        return httpx.Response(200, html="<title>T</title>")

    def client() -> httpx.Client:
        return httpx.Client(
            headers={"User-Agent": SENTINEL},
            transport=httpx.MockTransport(handler),
            follow_redirects=True,
        )

    with client() as c:
        hash_page("https://fixturehost.invalid/a", client=c)
    with client() as c:
        page_is_visible("https://fixturehost.invalid/b", client=c)
    with client() as c:
        fetch_title("https://fixturehost.invalid/c", client=c)

    assert seen, "positive control: no request was made, so nothing was checked"
    assert set(seen) == {SENTINEL}, f"a call site overrode the client identity: {sorted(set(seen))}"


def test_setup_script_sends_the_shared_user_agent():
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    import zotero_setup

    assert zotero_setup._headers("k")["User-Agent"] == USER_AGENT


def test_the_readme_test_count_is_within_ten_percent_of_reality() -> None:
    """README said 780 while the suite ran 1,169. Nothing tied the prose to the
    code. Ten percent, not exact: a count that must be edited on every commit
    gets edited by hand into something else."""
    import subprocess
    import sys

    root = Path(__file__).resolve().parents[1]
    text = (root / "README.md").read_text()
    m = re.search(r"(\d[\d,]*) tests run by default", text)
    assert m, "README no longer states a test count"
    stated = int(m.group(1).replace(",", ""))
    out = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider"],
        capture_output=True, text=True, cwd=root, timeout=180,
    ).stdout
    m2 = re.search(r"(\d+) tests? collected", out) or re.search(r"(\d+)/(\d+) tests collected", out)
    assert m2, out[-400:]
    collected = int(m2.group(1))
    assert abs(stated - collected) <= collected * 0.1, f"README says {stated}, collected {collected}"

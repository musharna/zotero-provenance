"""A superseded cache root must delegate to the installed one, not refuse.

A session keeps whichever plugin root it resolved at its own start and cannot be
made to re-resolve without restarting. The hook SCRIPT, though, is re-read from
disk on every fire — so the entry point is the one place a running session's
behaviour can still be corrected, and the correction is to hand the work to the
root the plugin manager currently pins.

The alternative the plugin shipped before this was to refuse, which is safe but
takes capture down for every live session until it restarts: on 2026-08-25 that
was 18 sessions and 29 hours. `staleness.py` said such roots "have to be removed
from disk instead, because there is no way to reason with them" — but you do not
have to reason with old CODE to replace the ENTRYPOINT that reaches it.

The authority is the pinned `installPath`, not a version comparison: it is what
the manager actually resolves, it needs no parsing, and it follows a rollback in
the right direction. The guard in staleness.py stays as defence in depth for the
case where forwarding cannot resolve a target at all.

These are real-execution tests. They run the shell entry points as subprocesses,
because the trampoline lives in shell, above the Python it protects.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from conftest import PLUGIN_ROOT

requires_jq = pytest.mark.skipif(shutil.which("jq") is None, reason="jq not installed")

CACHE_REL = Path(".claude/plugins/cache/zotero-provenance/zotero-provenance")
HOOKS = ("capture-stop.sh", "capture-prompt.sh")


def _fake_python(tmp_path: Path) -> Path:
    """A stub `python3` recording argv, so "ran locally" is observable."""
    argv_out = tmp_path / "argv.txt"
    stub = tmp_path / "bin" / "python3"
    stub.parent.mkdir(parents=True, exist_ok=True)
    stub.write_text(f'#!/bin/sh\necho "$@" > {argv_out}\ncat > /dev/null\n')
    stub.chmod(0o755)
    return argv_out


def _marker_hook(path: Path, marker: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f'#!/usr/bin/env bash\ncat >/dev/null\necho forwarded > "{marker}"\nexit 0\n'
    )
    path.chmod(0o755)


def _install(tmp_path: Path, *, mine: str, pinned: str, hook: str) -> dict:
    """Build a fake plugin cache: `mine` holds the real hook, `pinned` a marker."""
    home = tmp_path / "home"
    cache = home / CACHE_REL
    mine_root, pinned_root = cache / mine, cache / pinned
    (mine_root / "hooks").mkdir(parents=True, exist_ok=True)

    for name in (hook, "lib.sh", "run-python.sh"):
        src = PLUGIN_ROOT / "hooks" / name
        if src.exists():
            shutil.copy(src, mine_root / "hooks" / name)
    shutil.copytree(PLUGIN_ROOT / "scripts", mine_root / "scripts", dirs_exist_ok=True)

    marker = tmp_path / "marker.txt"
    if pinned_root != mine_root:
        _marker_hook(pinned_root / "hooks" / hook, marker)
    else:
        pinned_root.mkdir(parents=True, exist_ok=True)

    reg = home / ".claude" / "plugins" / "installed_plugins.json"
    reg.parent.mkdir(parents=True, exist_ok=True)
    reg.write_text(
        json.dumps(
            {
                "version": 2,
                "plugins": {
                    "zotero-provenance@zotero-provenance": [
                        {
                            "scope": "user",
                            "installPath": str(pinned_root),
                            "version": pinned,
                        }
                    ]
                },
            }
        )
    )
    return {"home": home, "hook": mine_root / "hooks" / hook, "marker": marker}


def _env(tmp_path: Path, home: Path) -> dict[str, str]:
    env = os.environ.copy()
    for var in (
        "ZOTERO_CAPTURE_DISABLE",
        "ZOTERO_API_KEY",
        "ZOTERO_LIBRARY_ID",
        "ZOTERO_LIBRARY_TYPE",
        "ZOTERO_WEBSOURCES_COLLECTION_KEY",
        "ZP_FORWARDED_FROM",
    ):
        env.pop(var, None)
    env["HOME"] = str(home)
    env["ZOTERO_CAPTURE_STATE_DIR"] = str(tmp_path / "state")
    env["ZOTERO_SECRETS_FILE"] = str(tmp_path / "no-such-secrets.env")
    env["PATH"] = f"{tmp_path / 'bin'}:{env['PATH']}"
    return env


def _run(hook: Path, env: dict[str, str], payload: dict) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(hook)],
        input=json.dumps(payload),
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )


def _appears(path: Path, timeout: float = 10.0) -> bool:
    """Whether `path` shows up within `timeout`.

    capture-prompt.sh runs its capture detached so prompt submission is never
    delayed, so the file it produces arrives after the hook has already exited.
    Asserting immediately raced that — and, worse, made the NEGATIVE assertions
    pass for free: "the stale root did not capture" is indistinguishable from
    "the background job had not started yet". Both directions wait.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return True
        time.sleep(0.05)
    return False


def _stays_absent(path: Path, grace: float = 3.0) -> bool:
    """Whether `path` is still absent after a detached job would have written it."""
    return not _appears(path, timeout=grace)


def _payload() -> dict:
    return {
        "last_assistant_message": "see https://fixturehost.org/foo",
        "prompt": "see https://fixturehost.org/foo",
        "session_id": "s1",
        "cwd": "/home/someone/agrigen",
        "transcript_path": "",
    }


@requires_jq
@pytest.mark.parametrize("hook", HOOKS)
def test_superseded_root_forwards_to_the_pinned_one(tmp_path: Path, hook: str) -> None:
    """The whole point: an old root runs the CURRENT code instead of its own."""
    setup = _install(tmp_path, mine="0.9.0", pinned="1.0.0", hook=hook)
    argv_out = _fake_python(tmp_path)
    proc = _run(setup["hook"], _env(tmp_path, setup["home"]), _payload())

    assert proc.returncode == 0, proc.stderr
    assert _appears(setup["marker"]), f"never forwarded; stderr: {proc.stderr!r}"
    # Positive control: forwarding means the STALE root's own Python never ran.
    assert _stays_absent(argv_out), "stale root executed its own capture as well"


@requires_jq
@pytest.mark.parametrize("hook", HOOKS)
def test_pinned_root_does_not_forward(tmp_path: Path, hook: str) -> None:
    """The current root must run locally — or forwarding would be an infinite pass."""
    setup = _install(tmp_path, mine="1.0.0", pinned="1.0.0", hook=hook)
    argv_out = _fake_python(tmp_path)
    proc = _run(setup["hook"], _env(tmp_path, setup["home"]), _payload())

    assert proc.returncode == 0, proc.stderr
    assert _appears(argv_out), f"current root did not capture; stderr: {proc.stderr!r}"
    assert _stays_absent(setup["marker"])


@requires_jq
@pytest.mark.parametrize("hook", HOOKS)
def test_a_development_checkout_never_forwards(tmp_path: Path, hook: str) -> None:
    """Running the repo's own hooks must not silently execute the installed copy.

    Otherwise the test suite, and anyone debugging from a checkout, would be
    exercising whatever is deployed on the machine rather than the code in front
    of them — the exact confusion this plugin has already been bitten by twice.
    """
    setup = _install(tmp_path, mine="0.9.0", pinned="1.0.0", hook=hook)
    argv_out = _fake_python(tmp_path)
    env = _env(tmp_path, setup["home"])

    proc = _run(PLUGIN_ROOT / "hooks" / hook, env, _payload())

    assert proc.returncode == 0, proc.stderr
    assert _stays_absent(setup["marker"]), "a checkout forwarded to the installed root"
    assert _appears(argv_out), f"checkout did not run locally; stderr: {proc.stderr!r}"


@requires_jq
@pytest.mark.parametrize("hook", HOOKS)
def test_forwarding_does_not_recurse(tmp_path: Path, hook: str) -> None:
    """A forward that lands back on a stale root must stop, not loop."""
    setup = _install(tmp_path, mine="0.9.0", pinned="1.0.0", hook=hook)
    argv_out = _fake_python(tmp_path)
    env = _env(tmp_path, setup["home"])
    env["ZP_FORWARDED_FROM"] = "/somewhere/else"

    proc = _run(setup["hook"], env, _payload())

    assert proc.returncode == 0, proc.stderr
    assert _stays_absent(setup["marker"]), "forwarded despite the recursion guard"
    assert _stays_absent(argv_out), "stale root captured despite being forwarded-to"


@requires_jq
@pytest.mark.parametrize("hook", HOOKS)
def test_unresolvable_target_exits_zero_without_capturing(
    tmp_path: Path, hook: str
) -> None:
    """No registry to read: refuse quietly rather than write from a stale root."""
    setup = _install(tmp_path, mine="0.9.0", pinned="1.0.0", hook=hook)
    (setup["home"] / ".claude" / "plugins" / "installed_plugins.json").unlink()
    argv_out = _fake_python(tmp_path)

    proc = _run(setup["hook"], _env(tmp_path, setup["home"]), _payload())

    assert proc.returncode == 0, proc.stderr
    assert _stays_absent(setup["marker"])
    assert _stays_absent(argv_out), "stale root captured with no way to check itself"


# --- registry resolution, per the 2026-08-25 audit ----------------------------


def _write_registry(home: Path, entries: dict) -> None:
    reg = home / ".claude" / "plugins" / "installed_plugins.json"
    reg.parent.mkdir(parents=True, exist_ok=True)
    reg.write_text(json.dumps({"version": 2, "plugins": entries}))


@requires_jq
@pytest.mark.parametrize("hook", HOOKS)
def test_another_marketplace_listed_first_is_not_executed(tmp_path: Path, hook: str) -> None:
    """The audit's reproduction: the prefix match exec'd the wrong plugin."""
    setup = _install(tmp_path, mine="0.9.0", pinned="1.0.0", hook=hook)
    home = setup["home"]
    wrong = home / CACHE_REL.parent.parent / "other-marketplace" / "zotero-provenance" / "9.9.9"
    wrong_marker = tmp_path / "wrong.txt"
    _marker_hook(wrong / "hooks" / hook, wrong_marker)
    _write_registry(
        home,
        {
            "zotero-provenance@other-marketplace": [
                {"scope": "project", "installPath": str(wrong)}
            ],
            "zotero-provenance@zotero-provenance": [
                {"scope": "user", "installPath": str(home / CACHE_REL / "1.0.0")}
            ],
        },
    )

    proc = _run(setup["hook"], _env(tmp_path, home), _payload())

    assert proc.returncode == 0, proc.stderr
    assert _stays_absent(wrong_marker), "executed a plugin from another marketplace"
    assert _appears(setup["marker"]), "did not reach the correct root"


@requires_jq
@pytest.mark.parametrize("hook", HOOKS)
def test_a_trailing_slash_does_not_make_a_root_forward_to_itself(
    tmp_path: Path, hook: str
) -> None:
    """Otherwise every fire self-forwards, hits the recursion guard, and is lost.

    Not one capture — all of them, silently, for the life of the session. That is
    the 29-hour outage again, reached by a spelling difference.
    """
    setup = _install(tmp_path, mine="1.0.0", pinned="1.0.0", hook=hook)
    home = setup["home"]
    argv_out = _fake_python(tmp_path)
    _write_registry(
        home,
        {
            "zotero-provenance@zotero-provenance": [
                {"scope": "user", "installPath": str(home / CACHE_REL / "1.0.0") + "/"}
            ]
        },
    )

    proc = _run(setup["hook"], _env(tmp_path, home), _payload())

    assert proc.returncode == 0, proc.stderr
    assert _appears(argv_out), f"the pinned root refused itself; stderr: {proc.stderr!r}"


@requires_jq
@pytest.mark.parametrize("hook", HOOKS)
def test_two_scopes_disagreeing_refuses_instead_of_guessing(
    tmp_path: Path, hook: str
) -> None:
    setup = _install(tmp_path, mine="0.9.0", pinned="1.0.0", hook=hook)
    home = setup["home"]
    argv_out = _fake_python(tmp_path)
    other = home / CACHE_REL / "2.0.0"
    (other / "hooks").mkdir(parents=True, exist_ok=True)
    _write_registry(
        home,
        {
            "zotero-provenance@zotero-provenance": [
                {"scope": "user", "installPath": str(home / CACHE_REL / "1.0.0")},
                {"scope": "project", "installPath": str(other)},
            ]
        },
    )

    proc = _run(setup["hook"], _env(tmp_path, home), _payload())

    assert proc.returncode == 0, proc.stderr
    assert _stays_absent(setup["marker"]), "guessed between two candidates"
    assert _stays_absent(argv_out), "captured from a superseded root"


@requires_jq
@pytest.mark.parametrize("hook", HOOKS)
def test_a_symlinked_home_still_forwards(tmp_path: Path, hook: str) -> None:
    """A symlinked $HOME silently switched the whole trampoline off.

    ZP_MINE is canonicalised with `pwd -P`, but the cache prefix it was compared
    against came from a raw $HOME. With /home/alice -> /srv/users/alice the two
    never match, so a superseded root was classified as a development checkout
    and ran its own stale Python — the v0.3.0 failure, reintroduced by the fix
    that was supposed to prevent it.
    """
    physical = tmp_path / "physical"
    physical.mkdir()
    link = tmp_path / "link"
    link.symlink_to(physical)

    setup = _install(tmp_path, mine="0.9.0", pinned="1.0.0", hook=hook)
    # Rebuild the same layout under the physical dir, reached via the symlink.
    shutil.copytree(setup["home"], physical / "home", dirs_exist_ok=True)
    home_via_link = link / "home"

    argv_out = _fake_python(tmp_path)
    env = _env(tmp_path, home_via_link)
    marker = tmp_path / "marker.txt"
    target_hook = physical / "home" / CACHE_REL / "1.0.0" / "hooks" / hook
    _marker_hook(target_hook, marker)
    reg = physical / "home" / ".claude" / "plugins" / "installed_plugins.json"
    reg.write_text(
        json.dumps(
            {
                "version": 2,
                "plugins": {
                    "zotero-provenance@zotero-provenance": [
                        {
                            "scope": "user",
                            "installPath": str(physical / "home" / CACHE_REL / "1.0.0"),
                        }
                    ]
                },
            }
        )
    )

    proc = _run(home_via_link / CACHE_REL / "0.9.0" / "hooks" / hook, env, _payload())

    assert proc.returncode == 0, proc.stderr
    assert _appears(marker), "a symlinked HOME switched the trampoline off"
    assert _stays_absent(argv_out), "the stale root captured anyway"


@requires_jq
@pytest.mark.parametrize("hook", HOOKS)
def test_equivalent_spellings_are_one_candidate_not_two(tmp_path: Path, hook: str) -> None:
    """Python canonicalises before deduping; the shell did not, so two spellings
    of one root read as ambiguous and refused every capture."""
    setup = _install(tmp_path, mine="1.0.0", pinned="1.0.0", hook=hook)
    home = setup["home"]
    argv_out = _fake_python(tmp_path)
    root = home / CACHE_REL / "1.0.0"
    _write_registry(
        home,
        {
            "zotero-provenance@zotero-provenance": [
                {"scope": "user", "installPath": str(root)},
                {"scope": "project", "installPath": str(root / ".." / "1.0.0")},
            ]
        },
    )

    proc = _run(setup["hook"], _env(tmp_path, home), _payload())

    assert proc.returncode == 0, proc.stderr
    assert _appears(argv_out), f"two spellings of one root refused; {proc.stderr!r}"


@requires_jq
@pytest.mark.parametrize("order", ["valid-first", "valid-last"])
@pytest.mark.parametrize("hook", HOOKS)
def test_a_failing_jq_does_not_yield_a_usable_candidate(
    tmp_path: Path, hook: str, order: str
) -> None:
    """Process substitution hides the producer's exit status.

    With a valid entry followed by a malformed one, jq printed the good path and
    THEN exited 5; the loop saw exactly one candidate and accepted it. Reverse
    the entries and it refused. Registry resolution was serialisation-order
    dependent again — the same class as the first-prefix-match bug that started
    this whole sequence. The answer must not depend on entry order.
    """
    setup = _install(tmp_path, mine="0.9.0", pinned="1.0.0", hook=hook)
    home = setup["home"]
    argv_out = _fake_python(tmp_path)
    good = {"scope": "user", "installPath": str(home / CACHE_REL / "1.0.0")}
    entries = [good, "bad-entry"] if order == "valid-first" else ["bad-entry", good]
    _write_registry(home, {"zotero-provenance@zotero-provenance": entries})

    proc = _run(setup["hook"], _env(tmp_path, home), _payload())

    assert proc.returncode == 0, proc.stderr
    assert _stays_absent(setup["marker"]), f"accepted a candidate from a failed jq ({order})"
    assert _stays_absent(argv_out), "captured from a superseded root"


# --- 0.60.0: the health hook forwards too, and a refusal is reported ----------


@requires_jq
def test_health_hook_forwards_from_a_superseded_root(tmp_path: Path) -> None:
    """HOOKS above lists the two capture hooks; the health hook carries the
    same byte-identical block and nothing drove it. A monitor that ran its
    own superseded code would report on rules a later release corrected."""
    setup = _install(tmp_path, mine="0.9.0", pinned="1.0.0", hook="session-health.sh")
    proc = _run(setup["hook"], _env(tmp_path, setup["home"]), {"session_id": "s1"})

    assert proc.returncode == 0, proc.stderr
    assert _appears(setup["marker"]), f"health hook never forwarded; stderr: {proc.stderr!r}"


@pytest.mark.parametrize("hook", HOOKS)
def test_an_unresolvable_forward_is_reported_not_silent(tmp_path: Path, hook: str) -> None:
    """Deliberately NOT gated on jq. Without jq every superseded root resolves
    no target and takes this exact branch, so on a machine without jq the
    whole trampoline suite used to skip and the one behaviour that machine
    depends on went untested. Refusing is right; refusing SILENTLY is the
    failure class the health check exists for."""
    from zotero_capture.health import evaluate

    setup = _install(tmp_path, mine="0.9.0", pinned="1.0.0", hook=hook)
    (setup["home"] / ".claude" / "plugins" / "installed_plugins.json").unlink()
    argv_out = _fake_python(tmp_path)

    proc = _run(setup["hook"], _env(tmp_path, setup["home"]), _payload())

    assert proc.returncode == 0, proc.stderr
    assert _stays_absent(setup["marker"])
    assert _stays_absent(argv_out), "stale root captured with no way to check itself"
    log = tmp_path / "state" / "capture.log"
    assert log.exists(), "the refusal left no record"
    lines = log.read_text().splitlines()
    assert any('"forward-unresolved"' in line for line in lines), lines
    from datetime import datetime, timedelta

    warnings = evaluate(lines, pinned_root=None, now=datetime.now().astimezone(), window=timedelta(hours=24))
    assert any("forward-unresolved" in w for w in warnings), warnings

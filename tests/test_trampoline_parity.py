"""The three inline trampolines must stay byte-identical.

Inlining is right at runtime: a superseded root cannot rely on its own lib.sh,
so the delegation logic has to live in the file that gets replaced. But there
are three copies now — both capture hooks and the health hook — and three copies
of security-relevant path arithmetic drift silently.

They have drifted once already in spirit: session-health.sh had no trampoline at
all for two releases while its comment asserted it did not need one.
"""

from __future__ import annotations

from conftest import PLUGIN_ROOT

HOOKS_WITH_TRAMPOLINE = (
    "capture-stop.sh",
    "capture-prompt.sh",
    "session-health.sh",
)
START = "# --- trampoline"
END = "\t;;\nesac\n"


def _block(name: str) -> str:
    text = (PLUGIN_ROOT / "hooks" / name).read_text()
    start = text.index(START)
    end = text.index(END, start) + len(END)
    return text[start:end]


def test_every_hook_that_should_delegate_does() -> None:
    for name in HOOKS_WITH_TRAMPOLINE:
        text = (PLUGIN_ROOT / "hooks" / name).read_text()
        assert "ZP_FORWARDED_FROM" in text, f"{name} does not delegate"


def test_the_three_trampolines_are_byte_identical() -> None:
    blocks = {name: _block(name) for name in HOOKS_WITH_TRAMPOLINE}
    reference = blocks["capture-stop.sh"]
    for name, block in blocks.items():
        assert block == reference, f"{name}'s trampoline has drifted"


def test_the_trampoline_is_substantial_enough_to_be_the_real_thing() -> None:
    """Guards against the block markers matching an empty or stub region."""
    block = _block("capture-stop.sh")
    for needle in ("ZP_CACHE", "pwd -P", "installPath", "exec bash", "ZP_FORWARDED_FROM"):
        assert needle in block, f"trampoline block is missing {needle!r}"

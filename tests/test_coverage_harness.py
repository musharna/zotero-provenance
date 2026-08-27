"""The coverage harness's turn boundaries, which got it wrong the first time.

`dev/measure_coverage.py` asks what fraction of cited URLs reached the library.
That only means something if it knows which message the Stop hook was actually
offered: the hook is handed `last_assistant_message`, so the FINAL assistant
message of a turn is eligible and everything before it is structurally unseen.

Claude Code records tool results as `type: "user"` records. The first version
treated any user record as a turn boundary, so every assistant message ended its
own turn, every message looked eligible, and the structural gap was reported as
exactly zero across a corpus of agentic sessions. The number it produced was
plausible, which is what made it dangerous -- an implausible zero was the only
tell.

These tests pin the discriminator, because the failure was silent arithmetic
rather than an exception.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from conftest import PLUGIN_ROOT

sys.path.insert(0, str(PLUGIN_ROOT / "dev"))
sys.path.insert(0, str(PLUGIN_ROOT / "scripts"))

from measure_coverage import (  # noqa: E402
    _eligible_urls,
    _is_real_user_message,
    _turns,
)


def _assistant(text: str, ts: str = "2026-08-27T10:00:00Z") -> str:
    return json.dumps(
        {
            "type": "assistant",
            "timestamp": ts,
            "message": {"content": [{"type": "text", "text": text}]},
        }
    )


def _tool_result() -> str:
    return json.dumps(
        {
            "type": "user",
            "toolUseResult": {"stdout": "ok"},
            "message": {"content": [{"type": "tool_result", "content": "ok"}]},
        }
    )


def _user(text: str = "do the thing") -> str:
    return json.dumps(
        {"type": "user", "message": {"content": [{"type": "text", "text": text}]}}
    )


def test_a_tool_result_is_not_a_turn_boundary() -> None:
    """The bug. A tool result is the agent working, not a person speaking."""
    assert not _is_real_user_message(_tool_result())


def test_a_real_user_message_is_a_turn_boundary() -> None:
    """Positive control: if nothing counted as a boundary, the test above would
    pass against a function that always returns False."""
    assert _is_real_user_message(_user())


def test_a_string_content_user_message_is_a_boundary() -> None:
    line = json.dumps({"type": "user", "message": {"content": "just text"}})
    assert _is_real_user_message(line)


def test_an_empty_user_message_is_not_a_boundary() -> None:
    line = json.dumps({"type": "user", "message": {"content": "   "}})
    assert not _is_real_user_message(line)


def test_tool_calls_do_not_split_a_turn(tmp_path: Path) -> None:
    """One turn with two assistant messages, not two turns with one each.

    This is the whole point: only the SECOND message is eligible, and the first
    is the structural gap.
    """
    transcript = tmp_path / "t.jsonl"
    transcript.write_text(
        "\n".join(
            [
                _user(),
                _assistant("first, see https://example.org/a"),
                _tool_result(),
                _assistant("finally, see https://example.org/b"),
                _user("next thing"),
            ]
        )
        + "\n"
    )

    turns = list(_turns(transcript))

    assert len(turns) == 1, f"tool results split the turn: {len(turns)} turns"
    assert len(turns[0]) == 2
    assert turns[0][-1][0].startswith("finally")


def test_a_real_user_message_does_split_two_turns(tmp_path: Path) -> None:
    transcript = tmp_path / "t.jsonl"
    transcript.write_text(
        "\n".join([_user(), _assistant("one"), _user("again"), _assistant("two")])
        + "\n"
    )

    assert len(list(_turns(transcript))) == 2


def test_sidechain_records_are_ignored(tmp_path: Path) -> None:
    """Subagent traffic runs on a separate spine the Stop hook never sees, so
    counting it would manufacture a gap the design never had."""
    side = json.loads(_assistant("sub https://example.org/sub"))
    side["isSidechain"] = True
    transcript = tmp_path / "t.jsonl"
    transcript.write_text(
        "\n".join([_user(), json.dumps(side), _assistant("main")]) + "\n"
    )

    turns = list(_turns(transcript))

    assert [text for text, _ in turns[0]] == ["main"]


def test_excluded_urls_are_not_counted_as_misses() -> None:
    """Capture declines reserved names on purpose; a decline is not a failure."""
    assert _eligible_urls("see https://example.com/x") == []


def test_an_ordinary_url_is_eligible() -> None:
    """Positive control for the exclusion test above."""
    assert _eligible_urls("see https://arxiv.org/abs/2401.00001") == [
        "https://arxiv.org/abs/2401.00001"
    ]


def test_this_plugins_own_report_is_not_counted() -> None:
    """The self-capture guard. Its own output must not read as a citation.

    Driven through the real marker rather than a lookalike, because the point is
    that the harness honours the same guard capture does.
    """
    from zotero_capture.url_processing import NO_CAPTURE_MARKER

    body = "listed: https://arxiv.org/abs/2401.00002"

    assert _eligible_urls(f"{NO_CAPTURE_MARKER}\n{body}") == []
    # Positive control: the same body without the marker IS counted, so the
    # assertion above is the guard working rather than the URL being unusable.
    assert _eligible_urls(body) == ["https://arxiv.org/abs/2401.00002"]

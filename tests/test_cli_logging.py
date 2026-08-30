"""The maintenance CLIs must actually turn logging on -- and the hook must not.

`snapshot_pages.py` ran for over an hour against the live index and printed
nothing, because every `logger.info` in the package was below an unconfigured
root logger's WARNING threshold. Fixing the one script would have left the rule
"remember to configure logging" in nine places, which is how this codebase has
lost a rule before. So the rule is enforced here, by discovering the scripts
rather than listing them: a tenth CLI that forgets fails this file.
"""

from __future__ import annotations

import ast
import logging
from pathlib import Path

import pytest

from zotero_capture.logging_setup import PACKAGE_LOGGER, configure_cli_logging

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"

# Both of these deliberately keep the unconfigured behaviour; see logging_setup.
# They are named, not pattern-matched, so removing one is a visible edit.
EXEMPT = {
    # The hook appends stderr to capture.log; the bare lastResort lines are its
    # diagnostic trail.
    "zotero_capture_main.py",
    # SessionStart; its stderr is health-errors.log, whose emptiness is the signal.
    "zotero_capture_health.py",
}


def _defines_main(path: Path) -> bool:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return any(
        isinstance(node, ast.FunctionDef) and node.name == "main" for node in tree.body
    )


def _entry_points() -> list[Path]:
    return sorted(p for p in SCRIPTS.glob("*.py") if _defines_main(p))


def _maintenance_clis() -> list[Path]:
    return [p for p in _entry_points() if p.name not in EXEMPT]


def _configures_logging(path: Path) -> bool:
    return "configure_cli_logging(" in path.read_text(encoding="utf-8")


def test_the_discovery_actually_found_scripts() -> None:
    """A positive control: if the glob broke, every other test here would pass
    vacuously over an empty list and report the rule as upheld."""
    found = _maintenance_clis()
    assert len(found) >= 8, f"expected the maintenance CLIs, found {found}"


@pytest.mark.parametrize("script", _maintenance_clis(), ids=lambda p: p.name)
def test_every_maintenance_cli_configures_logging(script: Path) -> None:
    assert _configures_logging(script), (
        f"{script.name} defines main() but never calls configure_cli_logging(), "
        "so every logger.info it triggers is silently dropped"
    )


@pytest.mark.parametrize("name", sorted(EXEMPT))
def test_the_exempt_paths_still_exist(name: str) -> None:
    """A rename would drop the file out of EXEMPT's reach and the exemption
    would quietly become a claim about nothing."""
    assert (SCRIPTS / name).exists(), f"{name} is exempted but does not exist"


@pytest.mark.parametrize("name", sorted(EXEMPT))
def test_the_exempt_paths_do_not_configure_logging(name: str) -> None:
    """The other direction. Adding logging setup to the hook would redirect the
    lines capture.log depends on; this fails before that ships."""
    assert not _configures_logging(SCRIPTS / name), (
        f"{name} is exempt because another file depends on its unconfigured "
        "stderr; configuring logging here changes what that file receives"
    )


@pytest.fixture
def clean_logger():
    logger = logging.getLogger(PACKAGE_LOGGER)
    saved_handlers, saved_level = list(logger.handlers), logger.level
    logger.handlers.clear()
    yield logger
    logger.handlers[:] = saved_handlers
    logger.setLevel(saved_level)


def test_info_is_dropped_before_configuring(clean_logger) -> None:
    """The broken state, asserted directly -- this is what the run hit."""
    assert not logging.getLogger("zotero_capture.snapshot").isEnabledFor(logging.INFO)


def test_info_reaches_the_stream_after_configuring(clean_logger, capsys) -> None:
    import io

    stream = io.StringIO()
    configure_cli_logging(stream=stream)
    logging.getLogger("zotero_capture.snapshot").info("could not read %s", "u")
    assert "could not read u" in stream.getvalue()


def test_the_line_is_labelled_with_its_level(clean_logger) -> None:
    """lastResort emitted warnings bare, so a failure read like a status line."""
    import io

    stream = io.StringIO()
    configure_cli_logging(stream=stream)
    logging.getLogger("zotero_capture.prune").warning("prune failed for %s", "u")
    written = stream.getvalue()
    assert "WARNING" in written, f"level name missing from {written!r}"


def test_configuring_twice_does_not_double_the_output(clean_logger) -> None:
    import io

    stream = io.StringIO()
    configure_cli_logging(stream=stream)
    configure_cli_logging(stream=stream)
    logging.getLogger("zotero_capture.snapshot").info("once")
    assert stream.getvalue().count("once") == 1


def test_the_root_logger_is_left_alone(clean_logger) -> None:
    """Configuring root would change behaviour for the hook and for pytest."""
    before = list(logging.getLogger().handlers)
    configure_cli_logging()
    assert list(logging.getLogger().handlers) == before

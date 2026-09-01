"""The hook's diagnostic trail was a default, not a decision.

`0.37.0` found that no `basicConfig` existed anywhere: INFO was dropped and
WARNING fell through to `logging.lastResort`, which prints the bare message to
stderr. The capture hooks run the CLI with `2>>capture.log`, so those lastResort
lines ARE the hook's diagnostic trail -- and that is why the textbook library fix
(a NullHandler on the package) could not be applied: it would have silently
deleted them. 0.37.0 shipped a tripwire test and left the dependency in place.

This makes it explicit. The hook configures its own handler with the SAME bare
`%(message)s` shape lastResort was already producing, so capture.log lines do not
change, and the package can now carry a NullHandler like any other library.

Both directions are asserted, because either alone is satisfiable by a broken
change: a package that prints nothing passes "is silent when unconfigured" while
having destroyed the hook trail, and a package that prints everything passes "the
hook still warns" while making every importer noisy.
"""

from __future__ import annotations

import ast
import io
import logging
from pathlib import Path

import pytest

from zotero_capture import cli
from zotero_capture.logging_setup import (
    HOOK_HANDLER_NAME,
    PACKAGE_LOGGER,
    configure_cli_logging,
    configure_hook_logging,
)


@pytest.fixture(autouse=True)
def _restore_package_logger():
    """The package logger is process-global; leaking a handler would make a
    later test's silence assertion pass or fail for the wrong reason."""
    logger = logging.getLogger(PACKAGE_LOGGER)
    before, level = list(logger.handlers), logger.level
    yield
    logger.handlers[:] = before
    logger.setLevel(level)


def _package_logger_without_handlers() -> logging.Logger:
    logger = logging.getLogger(PACKAGE_LOGGER)
    logger.handlers[:] = [
        h for h in logger.handlers if isinstance(h, logging.NullHandler)
    ]
    logger.setLevel(logging.NOTSET)
    return logger


def test_the_hook_still_gets_its_warning_on_stderr() -> None:
    """The trail capture.log has always carried. Losing this is the failure the
    NullHandler would have caused, so it is asserted first."""
    _package_logger_without_handlers()
    buf = io.StringIO()
    configure_hook_logging(stream=buf)
    logging.getLogger("zotero_capture.capture").warning("item %s is gone", "K1")
    assert "item K1 is gone" in buf.getvalue()


def test_the_hook_line_keeps_the_bare_shape_lastresort_produced() -> None:
    """Not cosmetic. capture.log holds months of lines in this shape and the
    health tooling reads them; a formatter with a timestamp and level would
    change every future line while claiming to be a no-op refactor."""
    _package_logger_without_handlers()
    buf = io.StringIO()
    configure_hook_logging(stream=buf)
    logging.getLogger("zotero_capture.capture").warning("plain message")
    assert buf.getvalue() == "plain message\n"


def test_routine_info_stays_out_of_the_hook_log() -> None:
    """A per-turn hook log is not a place for routine chatter -- this project
    already spent four releases walking health-check noise back."""
    _package_logger_without_handlers()
    buf = io.StringIO()
    configure_hook_logging(stream=buf)
    logging.getLogger("zotero_capture.capture").info("captured 3 urls")
    assert buf.getvalue() == ""


def test_an_unconfigured_import_prints_nothing() -> None:
    """The other direction: importing the package must not make an application
    that never asked for logging start writing to its stderr.

    In a SUBPROCESS, and that is the whole point. The first version of this used
    `capsys` and PASSED WITH THE NULLHANDLER DELETED -- pytest's logging plugin
    installs a root handler, so `lastResort` never fires inside the suite and the
    assertion could not fail either way. A test that cannot fail is worse than no
    test, because it is counted as coverage. Only a fresh interpreter reproduces
    the condition the NullHandler exists to control.
    """
    import subprocess
    import sys as _sys

    src = (
        "import sys; sys.path.insert(0, 'scripts');"
        "import logging;"
        "import zotero_capture;"
        "logging.getLogger('zotero_capture.capture').warning('should not appear')"
    )
    proc = subprocess.run(
        [_sys.executable, "-c", src],
        cwd=str(Path(__file__).resolve().parents[1]),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stderr == "", (
        f"the package logged without being configured: {proc.stderr!r}"
    )


def test_the_subprocess_control_can_actually_see_a_message() -> None:
    """Positive control for the test above. If the subprocess harness were
    broken -- wrong cwd, failed import, stderr not captured -- an empty stderr
    would read as success. Prove the channel carries a message when one is sent.
    """
    import subprocess
    import sys as _sys

    src = (
        "import sys; sys.path.insert(0, 'scripts');"
        "import logging;"
        "from zotero_capture.logging_setup import configure_hook_logging;"
        "configure_hook_logging();"
        "logging.getLogger('zotero_capture.capture').warning('I am visible')"
    )
    proc = subprocess.run(
        [_sys.executable, "-c", src],
        cwd=str(Path(__file__).resolve().parents[1]),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    assert "I am visible" in proc.stderr


def test_the_package_carries_a_null_handler() -> None:
    """What makes the silence above a decision rather than an accident of which
    handlers a previous test happened to leave behind."""
    logger = logging.getLogger(PACKAGE_LOGGER)
    assert any(isinstance(h, logging.NullHandler) for h in logger.handlers)


def test_the_cli_entry_point_configures_before_it_can_log() -> None:
    """Derived from the source, not from a live run: an ordering bug here loses
    only the earliest lines, which is exactly the kind of thing a happy-path
    test never sees."""
    tree = ast.parse(Path(cli.__file__).read_text())
    fn = next(
        n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "main"
    )
    calls = [
        n.func.id
        for n in ast.walk(fn)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
    ]
    assert "configure_hook_logging" in calls, (
        "cli.main must configure the hook's logging; without it the package's "
        "NullHandler makes capture.log silent"
    )
    assert calls.index("configure_hook_logging") < calls.index(
        "install_termination_handler"
    ), "configuration must come before anything that can log"


def test_cli_logging_still_names_the_level() -> None:
    """Positive control on the OTHER configuration. The two applications want
    opposite things -- a maintenance CLI needs the level, because a warning that
    reads like a status line is how `prune failed for ...` got missed -- and a
    change that unified them would pass every test above."""
    _package_logger_without_handlers()
    buf = io.StringIO()
    configure_cli_logging(stream=buf)
    logging.getLogger("zotero_capture.snapshot").warning("something odd")
    out = buf.getvalue()
    assert "WARNING" in out and "something odd" in out


def test_the_two_configurations_do_not_fight() -> None:
    """A CLI that imports the hook path (or vice versa) must not double every
    line -- the failure `configure_cli_logging` was already made idempotent for,
    now that there are two of them."""
    _package_logger_without_handlers()
    buf = io.StringIO()
    configure_hook_logging(stream=buf)
    configure_hook_logging(stream=buf)
    logging.getLogger("zotero_capture.capture").warning("once")
    assert buf.getvalue() == "once\n"
    names = [
        getattr(h, "name", None) for h in logging.getLogger(PACKAGE_LOGGER).handlers
    ]
    assert names.count(HOOK_HANDLER_NAME) == 1

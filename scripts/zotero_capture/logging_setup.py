"""Turn on logging for the maintenance CLIs -- and only for them.

The library modules call `logger.info` and `logger.warning` throughout, but a
logger with no handler configured anywhere does two different unhelpful things:
INFO and DEBUG are dropped entirely, while WARNING and ERROR fall through to
`logging.lastResort`, which prints the bare message to stderr with no level, no
timestamp and no logger name. So `snapshot_pages.py` ran for over an hour
emitting nothing at all, and a `prune failed for ...` line was indistinguishable
from ordinary output.

This is deliberately NOT done in the package `__init__`, and NOT on the root
logger. Two paths depend on the current behaviour and must keep it:

  * the capture hook appends the child's stderr to capture.log, so those bare
    lastResort lines ARE the hook's human-readable diagnostic trail. Adding a
    NullHandler to the package -- the textbook library fix -- would silently
    delete them.
  * the SessionStart health check sends stderr to health-errors.log, a file
    whose being empty is the signal that nothing is wrong. Routine INFO would
    destroy that.

So configuration belongs at the application layer, and there are two different
applications here with opposite needs. A CLI opts in by calling this.
"""

from __future__ import annotations

import logging
import sys

# The package logger, not the root logger: configuring root would change
# behaviour for anything that imports us, including the hook and pytest.
PACKAGE_LOGGER = "zotero_capture"
_HANDLER_NAME = "zotero-provenance-cli"


def configure_cli_logging(level: int = logging.INFO, stream=None) -> logging.Logger:
    """Attach one formatted stderr handler to the package logger.

    Idempotent: calling it twice does not double every line, which matters
    because a CLI that imports another CLI would otherwise duplicate output.
    """
    logger = logging.getLogger(PACKAGE_LOGGER)
    for existing in logger.handlers:
        if getattr(existing, "name", None) == _HANDLER_NAME:
            logger.setLevel(level)
            existing.setLevel(level)
            return logger

    handler = logging.StreamHandler(stream if stream is not None else sys.stderr)
    handler.name = _HANDLER_NAME
    handler.setLevel(level)
    # The level name is the point. Without it a warning reads like a status line.
    handler.setFormatter(
        logging.Formatter(
            fmt="%(asctime)s %(levelname)-7s %(message)s",
            datefmt="%H:%M:%S",
        )
    )
    logger.addHandler(handler)
    logger.setLevel(level)

    # Progress goes to stderr while the report goes to stdout, and under the
    # usual `>log 2>&1` a block-buffered stdout would flush the whole report
    # after every progress line regardless of when it was printed. Line
    # buffering makes the interleaving reflect the order things happened.
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except (AttributeError, ValueError):  # not a real stream (pytest capture)
        pass

    return logger

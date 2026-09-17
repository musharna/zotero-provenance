#!/usr/bin/env python3
"""Executable entry point for the SessionStart health check.

A shim, like zotero_capture_main.py: the hook trampoline re-roots THIS PATH to
the pinned plugin root, so the file must stay here; the code lives in the
package (zotero_capture.health_cli), which is also the `zotero-capture-health`
console script of an installed wheel.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from zotero_capture.health_cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())

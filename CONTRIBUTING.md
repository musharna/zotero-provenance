# Contributing

## Setup

```
python -m pip install -e ".[dev]" ruff
python -m pytest -q          # network-free; ~3 minutes
ruff check scripts tests dev
```

The 10 live tests reach the real internet and are opted *into* with
`-m live`. Never point them at a production library: set
`ZOTERO_CAPTURE_STATE_DIR` to a scratch directory and check `env | grep ^ZOTERO_`
first. `dev/probe_root.sh` runs a deployed root with every side-effect channel
closed and proves it.

## How changes land here

- **Test first, and watch it fail.** A guard that has never been seen red is
  not evidence; several defects in this repository's history were hidden by
  tests that could not fail.
- **A negative assertion ships with a positive control** in the same test.
- **One rule, one place.** Six of the defects shipped here were a second copy
  of a rule that drifted. If a rule must be read in two places, add a parity
  test rather than a second copy.
- **Real execution at every boundary.** A fixture that makes a test runnable
  can also make a bug invisible (`init_db` in every fixture hid a missing
  migration from the whole suite). Run the real CLI against a scratch index
  once before calling a change done.
- **The CHANGELOG is written from measurements**, after the fact, and it says
  what was tried and refuted as well as what shipped.

## Layout

- `hooks/` — the Stop / UserPromptSubmit / SessionStart hooks. The trampoline
  block at the top of each is byte-identical on purpose and tested for parity.
- `scripts/zotero_capture/` — the package. `scripts/*.py` are maintenance CLIs.
- `commands/` — the slash commands.
- `dev/` — probes and backports used while developing. Not part of the plugin
  runtime, though the plugin manager copies the whole tree.
- `tests/` — network-free by default.

Releases bump the version in three places (`pyproject.toml`,
`.claude-plugin/plugin.json`, `scripts/zotero_capture/__init__.py`); a test
holds them together.

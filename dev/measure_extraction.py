#!/usr/bin/env python3
"""Measure a change to URL extraction against real assistant traffic.

Every claim in the changelog of the form "changes nothing on real traffic" was
produced here. Two rules make the number mean something:

  * a **control** that must fail. `--control` swaps in a tokenizer that admits a
    space, which mangles roughly one message in six. If the control reports no
    change, the harness is broken and the headline number is worthless.
  * a **seam check** that runs first. The control is only meaningful if the
    thing it replaces is the thing extraction actually calls. From 0.11.3 the
    boundary moved to linkify and `URL_RE` became vestigial, so the old harness
    went on swapping a regex nothing read: the control reported zero damage and
    every "changes nothing on real traffic" measured after that was vacuous.
    The canary caught it and nobody re-ran the control. Now the seam is proved
    on a fixture before the corpus is touched.
  * a **corpus of real messages**, read from Claude Code's own transcripts, not
    from fixtures written by the same person who wrote the regex.

    python3 dev/measure_extraction.py                 # shipped vs 0.11.0 blacklist
    python3 dev/measure_extraction.py --control       # prove the harness can fail

Reports messages changed, and for each changed URL whether the candidate
shortened it (a prefix survived), lost it, or invented a new one.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "scripts"))

from zotero_capture import url_processing as up  # noqa: E402

# The tokenizer 0.11.0 shipped: a character blacklist.
BLACKLIST = re.compile(
    r"https?://\[[0-9A-Fa-f:.]+\](?::\d+)?[^\s<>\"'`\]]*"
    r"|https?://[^\s<>\"'`\]]+",
    re.IGNORECASE,
)
# Admits a space, so a URL swallows the rest of its sentence. Must show damage.
BROKEN_CONTROL = re.compile(r"https?://[^\n]+", re.IGNORECASE)


def load_corpus(root: pathlib.Path) -> list[str]:
    """Every distinct assistant text block that mentions a URL."""
    msgs: list[str] = []
    seen: set[str] = set()
    for transcript in root.rglob("*.jsonl"):
        try:
            with transcript.open(encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    if "http" not in line:
                        continue
                    try:
                        record = json.loads(line)
                    except ValueError:
                        continue
                    if record.get("type") != "assistant":
                        continue
                    for block in (record.get("message") or {}).get("content") or []:
                        if not isinstance(block, dict) or block.get("type") != "text":
                            continue
                        text = block.get("text") or ""
                        if ("http://" in text or "https://" in text) and text not in seen:
                            seen.add(text)
                            msgs.append(text)
        except OSError as exc:  # a transcript being written, a permission gap
            print(f"skipped {transcript}: {exc}", file=sys.stderr)
    return msgs


def _bare_urls_from(pattern: re.Pattern[str]):
    """A `bare_urls` built from a plain tokenizer, for comparison arms.

    Patching `bare_urls` rather than a regex is what keeps this honest: it is
    the function `extract_urls` actually calls for prose, so a swap here cannot
    silently become a no-op the way swapping `URL_RE` did.
    """

    def _bare(text: str) -> list[str]:
        return [
            up._trim_prose_url(m.group(0)) for m in pattern.finditer(up.strip_ansi(text))
        ]

    return _bare


def extract_all(
    messages: list[str], tokenizer: re.Pattern[str] | None
) -> list[list[str]]:
    """Extract with `tokenizer` for prose, or with the shipped code if None."""
    if tokenizer is None:
        return [up.extract_urls(m) for m in messages]
    shipped = up.bare_urls
    try:
        up.bare_urls = _bare_urls_from(tokenizer)
        return [up.extract_urls(m) for m in messages]
    finally:
        up.bare_urls = shipped


# A sentence the shipped extractor and a space-admitting tokenizer MUST read
# differently. If they agree, the injection point is not connected to anything.
_SEAM_PROBE = "see https://example.org/a and then some more words here"


def seam_is_live() -> bool:
    """Whether replacing the tokenizer actually changes what extraction returns."""
    shipped = extract_all([_SEAM_PROBE], None)
    swapped = extract_all([_SEAM_PROBE], BROKEN_CONTROL)
    return shipped != swapped


def compare(baseline: list[list[str]], candidate: list[list[str]]) -> dict[str, int]:
    counts = {"messages_changed": 0, "shortened": 0, "lost": 0, "gained": 0}
    examples: list[str] = []
    for before, after in zip(baseline, candidate):
        old, new = set(before), set(after)
        if old == new:
            continue
        counts["messages_changed"] += 1
        for url in old - new:
            prefix = next((c for c in new - old if url.startswith(c) and c != url), None)
            if prefix is not None:
                counts["shortened"] += 1
                if len(examples) < 15:
                    examples.append(f"  shortened {url!r} -> {prefix!r}")
            else:
                counts["lost"] += 1
                if len(examples) < 15:
                    examples.append(f"  lost      {url!r}")
        for url in new - old:
            if not any(u.startswith(url) for u in old - new):
                counts["gained"] += 1
                if len(examples) < 15:
                    examples.append(f"  gained    {url!r}")
    for line in examples:
        print(line)
    return counts


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--transcripts",
        type=pathlib.Path,
        default=pathlib.Path.home() / ".claude" / "projects",
        help="directory of Claude Code transcripts to read the corpus from",
    )
    parser.add_argument(
        "--control",
        action="store_true",
        help="measure the deliberately broken tokenizer, which must show damage",
    )
    args = parser.parse_args()

    messages = load_corpus(args.transcripts)
    if not messages:
        print("no corpus: no assistant message mentioned a URL", file=sys.stderr)
        return 2

    if not seam_is_live():
        print(
            "the tokenizer swap changes nothing on a fixture designed to break: "
            "this harness is measuring code it does not control. Fix the seam "
            "before trusting any number below.",
            file=sys.stderr,
        )
        return 3

    candidate = BROKEN_CONTROL if args.control else None
    label = "BROKEN control (admits a space)" if args.control else "shipped extractor"

    baseline = extract_all(messages, BLACKLIST)
    result = compare(baseline, extract_all(messages, candidate))

    print(
        f"\ncorpus: {len(messages)} assistant messages, "
        f"{sum(len(u) for u in baseline)} URLs under the 0.11.0 blacklist"
    )
    print(f"{label}: {result['messages_changed']} messages differ "
          f"(shortened {result['shortened']}, lost {result['lost']}, gained {result['gained']})")

    if args.control and result["messages_changed"] == 0:
        print("\nthe control showed no damage: the harness cannot fail, so trust nothing it reports",
              file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

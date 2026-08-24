"""A host with no dot is never a public document.

19 rows in the live index had one, and not one was a source: intranet services
(``prometheus:9090``, ``homelab:3000``, Ollama on ``host:11434``), a machine
name, and this repo's own test fixtures (``https://h/R&D``, ``https://a``).

It belongs with the localhost, tailnet and reserved-name rules rather than being
a new kind of check — all four say the same thing, that the address cannot be a
source. A single-label name resolves only inside a network that already has it
in DNS or /etc/hosts, so it can never identify a document anyone else can read.

The trap is that an IP literal has no dot either. ``[2001:db8::1]`` is a host,
not a name, and it already has its own handling below; excluding it here would
resurrect the class of bug that once truncated every IPv6 URL.
"""

from __future__ import annotations

import pytest

from zotero_capture.url_processing import is_excluded


@pytest.mark.parametrize(
    "url",
    [
        "http://prometheus:9090",
        "http://homelab:3000",
        "http://host:11434/api/embeddings",
        "https://mjarnoldgt76",
        "https://public/api/fidelity.json",
        "https://h/R&D",
        "https://a",
        "http://fake",
    ],
)
def test_a_dotless_host_is_excluded(url):
    assert is_excluded(url) is True


@pytest.mark.parametrize(
    "url",
    [
        "https://doi.org/10.1016/j.cell.2013.12.027",
        "https://github.com/musharna/figcite",
        "https://de.wikipedia.org/wiki/München",
        "https://fixturehost.org/foo",
    ],
)
def test_an_ordinary_host_still_survives(url):
    """Positive control: without it the rule could exclude everything."""
    assert is_excluded(url) is False


def test_a_public_ipv6_literal_is_not_caught_by_the_dotless_rule():
    """The trap. "2001:db8::1" has no dot and is not a name.

    A naive rule excludes every IPv6 URL, which is the same damage as the "]"
    truncation that once stored them all as "https://[::1".
    """
    assert is_excluded("https://[2606:4700:4700::1111]/x") is False


def test_a_private_ipv6_literal_is_still_excluded_for_being_private():
    """And the reason has to stay the address rule, not the dot rule."""
    assert is_excluded("https://[::1]:8080/x") is True


def test_a_public_ipv4_literal_still_survives():
    assert is_excluded("https://93.184.216.34/page") is False

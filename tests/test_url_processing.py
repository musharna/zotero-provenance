"""URL processing tests — extraction, canonicalization, exclusion (spec D7 + Section 4)."""

from __future__ import annotations

import pytest

from zotero_capture.url_processing import canonicalize, extract_urls, is_excluded


# --- canonicalize ---


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("https://example.com/foo#bar", "https://example.com/foo"),
        ("https://example.com/foo/", "https://example.com/foo"),
        ("https://EXAMPLE.com/Foo", "https://example.com/Foo"),
        (
            "https://example.com/foo?utm_source=x&q=keep",
            "https://example.com/foo?q=keep",
        ),
        ("https://example.com/foo?fbclid=abc&gclid=def", "https://example.com/foo"),
        (
            "https://EXAMPLE.com/foo/?utm_campaign=x&id=42#frag",
            "https://example.com/foo?id=42",
        ),
        ("https://example.com/", "https://example.com"),
    ],
)
def test_canonicalize(raw: str, expected: str):
    assert canonicalize(raw) == expected


# --- extract_urls ---


def test_extract_basic_url():
    text = "See https://example.com/foo for details."
    assert extract_urls(text) == ["https://example.com/foo"]


def test_extract_strips_trailing_punctuation():
    text = "Check (https://example.com/foo), and [https://example.com/bar]."
    assert extract_urls(text) == ["https://example.com/foo", "https://example.com/bar"]


def test_extract_markdown_link():
    text = "See [the docs](https://example.com/docs)."
    assert extract_urls(text) == ["https://example.com/docs"]


def test_extract_source_line():
    text = "Source: https://example.com/article"
    assert extract_urls(text) == ["https://example.com/article"]


def test_extract_dedups_within_message():
    text = "https://example.com/x and again https://example.com/x"
    assert extract_urls(text) == ["https://example.com/x"]


def test_extract_no_urls():
    assert extract_urls("nothing here") == []


def test_extract_strips_backtick_fence():
    text = "See `https://example.com/foo` for details."
    assert extract_urls(text) == ["https://example.com/foo"]


# --- is_excluded ---


@pytest.mark.parametrize(
    "url, excluded",
    [
        ("https://example.com/foo", False),
        ("https://localhost:3000/x", True),
        ("http://127.0.0.1/x", True),
        ("http://0.0.0.0:8080/x", True),
        ("https://homelab.tail-abc12.ts.net/grafana", True),
        ("https://100.113.204.41:8765/jobs", True),
        ("http://10.0.0.110/", True),
        ("http://192.168.1.1/", True),
        ("http://172.16.5.5/", True),
        ("https://172.32.0.1/", False),
        ("http://[::1]/", True),
        ("http://[fe80::1]/", True),
        ("http://[fc00::1]/", True),
        ("https://[2606:4700:4700::1111]/", False),
    ],
)
def test_is_excluded(url: str, excluded: bool):
    assert is_excluded(url) is excluded

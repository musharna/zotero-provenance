"""Fetching must not be steerable at anything but a public address.

`is_excluded` inspects how a host is *spelled*, which is not a boundary: a name
can resolve wherever its owner likes, and a perfectly public URL can redirect to
the cloud metadata endpoint. The guard therefore sits in the transport, where
httpx re-enters once per hop, so the redirect that matters is checked too.
"""

from __future__ import annotations

import ipaddress

import httpx
import pytest

from zotero_capture.title_fetcher import GuardedTransport, UnsafeHostError


def _resolver(mapping: dict[str, str]):
    def resolve(host: str) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
        return [ipaddress.ip_address(mapping[host])]

    return resolve


def _ok(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, text="<title>fine</title>")


def test_a_private_literal_is_refused():
    transport = GuardedTransport(httpx.MockTransport(_ok))
    with httpx.Client(transport=transport) as client:
        with pytest.raises(UnsafeHostError):
            client.get("http://127.0.0.1/admin")


@pytest.mark.parametrize("host", ["2130706433", "0x7f000001", "017700000001", "127.1"])
def test_obfuscated_loopback_literals_are_refused(host: str):
    """The spellings that slipped past the textual check."""
    transport = GuardedTransport(httpx.MockTransport(_ok))
    with httpx.Client(transport=transport) as client:
        with pytest.raises(UnsafeHostError):
            client.get(f"http://{host}/admin")


def test_a_public_name_that_resolves_private_is_refused():
    """Spelling says nothing; only the resolved address does."""
    transport = GuardedTransport(
        httpx.MockTransport(_ok),
        resolve=_resolver({"nice.example-cdn.net": "10.0.0.5"}),
    )
    with httpx.Client(transport=transport) as client:
        with pytest.raises(UnsafeHostError):
            client.get("https://nice.example-cdn.net/x")


def test_link_local_metadata_is_refused():
    transport = GuardedTransport(
        httpx.MockTransport(_ok),
        resolve=_resolver({"meta.test-host": "169.254.169.254"}),
    )
    with httpx.Client(transport=transport) as client:
        with pytest.raises(UnsafeHostError):
            client.get("https://meta.test-host/latest/meta-data/")


def test_a_public_name_is_allowed():
    """Positive control: the guard must not simply refuse everything."""
    transport = GuardedTransport(
        httpx.MockTransport(_ok), resolve=_resolver({"real.test-host": "93.184.216.34"})
    )
    with httpx.Client(transport=transport) as client:
        assert client.get("https://real.test-host/x").status_code == 200


def test_a_redirect_into_a_private_address_is_refused():
    """The case a host filter structurally cannot catch.

    Asserts the first hop succeeded before the second was refused, so a guard
    that blocked everything could not masquerade as a pass.
    """
    hops: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        hops.append(request.url.host)
        if request.url.host == "public.test-host":
            return httpx.Response(302, headers={"location": "http://192.168.0.1/admin"})
        return _ok(request)

    transport = GuardedTransport(
        httpx.MockTransport(handler),
        resolve=_resolver({"public.test-host": "93.184.216.34"}),
    )
    with httpx.Client(transport=transport, follow_redirects=True) as client:
        with pytest.raises(UnsafeHostError):
            client.get("http://public.test-host/start")

    assert hops == ["public.test-host"], "the public first hop should have been served"


def test_a_host_that_does_not_resolve_is_refused():
    def resolve(host: str):
        raise OSError("no such host")

    transport = GuardedTransport(httpx.MockTransport(_ok), resolve=resolve)
    with httpx.Client(transport=transport) as client:
        with pytest.raises(UnsafeHostError):
            client.get("https://nowhere.test-host/x")

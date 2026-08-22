"""Checking an address is worthless if something else resolves the name again.

GuardedTransport resolved the hostname, refused it if any answer was private,
and then handed the ORIGINAL hostname to the inner transport — which resolved it
a second time when it connected. An attacker who controls the name's DNS can
answer the two queries differently: a public address for the check, a private
one for the connection. Alternating answers make that deterministic rather than
a race, and the redirect that reaches this code is already attacker-chosen.

The fix is to connect to the address that was actually validated. For the
duration of the call the URL's host is the validated literal, so httpcore's
connect_tcp uses it; the Host header keeps the original name so virtual hosts
still work; and the sni_hostname extension keeps it too, so TLS is negotiated
and the certificate verified against the name rather than the address (httpcore:
server_hostname = sni_hostname or origin.host). Afterwards the name is put back,
because httpx resolves a relative Location against this URL and carries the Host
header onward — so each hop gets its own lookup and its own check.

Because the pin is undone once the call returns, these tests snapshot what the
inner transport was actually handed rather than inspecting the request object
afterwards, which would only ever show the restored form.

The tests in test_ssrf.py cannot catch any of this: MockTransport never performs
the second resolution, so it cannot disagree with the first.

Reported by an external audit of v0.10.0 (2026-08-22).
"""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass

import httpx
import pytest

from zotero_capture.title_fetcher import GuardedTransport, UnsafeHostError

PUBLIC = "93.184.216.34"
PRIVATE = "10.0.0.5"
PUBLIC_V6 = "2606:2800:220:1:248:1893:25c8:1946"


@dataclass
class Hop:
    """What the inner transport saw, frozen at the moment it saw it."""

    url: httpx.URL
    host_header: str
    sni: str | None

    @property
    def connected_to(self) -> str:
        return self.url.host


def _recorder(hops: list[Hop], *, redirect_from: str | None = None):
    def handler(request: httpx.Request) -> httpx.Response:
        hops.append(
            Hop(
                url=request.url,
                host_header=request.headers["Host"],
                sni=request.extensions.get("sni_hostname"),
            )
        )
        if redirect_from is not None and request.url.path == redirect_from:
            return httpx.Response(302, headers={"location": "/next"})
        return httpx.Response(200, text="<title>ok</title>")

    return handler


def _fixed(mapping: dict[str, list[str]]):
    def resolve(host: str):
        return [ipaddress.ip_address(a) for a in mapping[host]]

    return resolve


def _fetch(url: str, resolve, *, redirect_from: str | None = None) -> list[Hop]:
    hops: list[Hop] = []
    transport = GuardedTransport(
        httpx.MockTransport(_recorder(hops, redirect_from=redirect_from)),
        resolve=resolve,
    )
    with httpx.Client(transport=transport, follow_redirects=True) as client:
        client.get(url)
    return hops


def test_the_connection_is_made_to_the_address_that_was_checked():
    hops = _fetch("https://cdn.test-host/page", _fixed({"cdn.test-host": [PUBLIC]}))
    assert hops[0].connected_to == PUBLIC, "the inner transport got a name to resolve"


def test_a_second_dns_answer_is_never_consulted():
    """The rebinding attack itself: answer public once, private next.

    A guard that passed the name onward would let the inner transport ask again
    and get PRIVATE. Pinning means there is no second question to answer.
    """
    answers = [PUBLIC, PRIVATE]
    calls = {"n": 0}

    def rebinding(_host: str):
        calls["n"] += 1
        return [ipaddress.ip_address(answers[min(calls["n"] - 1, len(answers) - 1)])]

    hops = _fetch("https://rebind.test-host/admin", rebinding)

    assert calls["n"] == 1, "the name was resolved more than once"
    assert hops[0].connected_to == PUBLIC
    assert hops[0].connected_to != PRIVATE


def test_the_host_header_still_names_the_site():
    """Pinning must not break virtual hosting."""
    hops = _fetch("https://cdn.test-host/page", _fixed({"cdn.test-host": [PUBLIC]}))
    assert hops[0].host_header == "cdn.test-host"


def test_tls_is_still_verified_against_the_name():
    """Without this the certificate would be checked against a bare IP."""
    hops = _fetch("https://cdn.test-host/page", _fixed({"cdn.test-host": [PUBLIC]}))
    assert hops[0].sni == "cdn.test-host"


def test_a_non_default_port_survives_pinning():
    hops = _fetch("https://cdn.test-host:8443/p", _fixed({"cdn.test-host": [PUBLIC]}))
    assert hops[0].connected_to == PUBLIC
    assert hops[0].url.port == 8443
    assert hops[0].host_header == "cdn.test-host:8443"


def test_an_ipv6_answer_is_pinned_as_a_bracketed_literal():
    hops = _fetch("https://v6.test-host/page", _fixed({"v6.test-host": [PUBLIC_V6]}))
    assert hops[0].connected_to == PUBLIC_V6
    assert f"[{PUBLIC_V6}]" in str(hops[0].url)
    assert hops[0].host_header == "v6.test-host"


def test_a_relative_redirect_is_followed_by_name_not_by_address():
    """Pinning must not leak into the next hop's addressing.

    httpx resolves a relative Location against the request URL and carries the
    Host header onward. Left pinned, the next hop would go to a bare IP with a
    bare-IP Host — missing a virtual-hosted site, and skipping its own check.
    """
    hops = _fetch(
        "https://vhost.test-host/start",
        _fixed({"vhost.test-host": [PUBLIC]}),
        redirect_from="/start",
    )
    assert [h.url.path for h in hops] == ["/start", "/next"]
    assert all(h.host_header == "vhost.test-host" for h in hops)
    assert all(h.connected_to == PUBLIC for h in hops), "each hop must still pin"


def test_a_private_answer_is_still_refused_before_any_pinning():
    """Negative control: pinning must not have replaced the check."""
    with pytest.raises(UnsafeHostError):
        _fetch("https://bad.test-host/x", _fixed({"bad.test-host": [PRIVATE]}))


def test_a_literal_address_is_passed_through_unchanged():
    """Nothing to pin: there was no name, so no second resolution can differ."""
    hops: list[Hop] = []
    transport = GuardedTransport(httpx.MockTransport(_recorder(hops)))
    with httpx.Client(transport=transport) as client:
        client.get(f"https://{PUBLIC}/page")

    assert hops[0].connected_to == PUBLIC
    assert hops[0].sni is None

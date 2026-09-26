"""SSRF blocklist completeness, NAT64/6to4 normalisation, fail-closed redirects (P0.6)."""

from __future__ import annotations

import ipaddress
import socket

import pytest

from nanobot.security import network
from nanobot.security.network import (
    configure_ssrf_whitelist,
    validate_resolved_url,
    validate_url_target,
)


@pytest.fixture(autouse=True)
def _reset_whitelist():
    saved = network._allowed_networks
    configure_ssrf_whitelist([])
    yield
    network._allowed_networks = saved


def _resolve_to(monkeypatch: pytest.MonkeyPatch, host: str, ip: str) -> None:
    def _resolver(hostname, port, family=0, type_=0, *args, **kwargs):
        if hostname == host:
            if ":" in ip:
                return [(socket.AF_INET6, socket.SOCK_STREAM, 0, "", (ip, 0, 0, 0))]
            return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", (ip, 0))]
        raise socket.gaierror(f"cannot resolve {hostname}")

    monkeypatch.setattr("nanobot.security.network.socket.getaddrinfo", _resolver)


def _no_resolution(monkeypatch: pytest.MonkeyPatch) -> None:
    def _resolver(*args, **kwargs):
        raise socket.gaierror("no resolution")

    monkeypatch.setattr("nanobot.security.network.socket.getaddrinfo", _resolver)


BLOCKED = [
    "192.0.0.8",
    "198.18.0.1",
    "198.19.255.254",
    "224.0.0.1",
    "239.255.255.250",
    "240.0.0.1",
    "255.255.255.255",
    "fec0::1",
    "ff02::1",
    # NAT64 well-known prefix embedding blocked IPv4
    "64:ff9b::7f00:1",  # 127.0.0.1
    "64:ff9b::a00:1",  # 10.0.0.1
    "64:ff9b::a9fe:a9fe",  # 169.254.169.254
    # 6to4 embedding blocked IPv4
    "2002:0a00:0001::",  # 10.0.0.1
    "2002:7f00:0001::",  # 127.0.0.1
]

ALLOWED = [
    "203.0.113.10",  # TEST-NET-3: scripts/test-docker.sh maps example.com here
    "8.8.8.8",
    "93.184.216.34",
    "64:ff9b::808:808",  # NAT64 of 8.8.8.8
    "2002:0808:0808::",  # 6to4 of 8.8.8.8
]


@pytest.mark.parametrize("ip", BLOCKED)
def test_blocked_addresses(ip: str, monkeypatch):
    assert network._is_private(ipaddress.ip_address(ip))
    _resolve_to(monkeypatch, "target.example", ip)
    ok, _ = validate_url_target("http://target.example/")
    assert not ok


@pytest.mark.parametrize("ip", ALLOWED)
def test_allowed_addresses(ip: str, monkeypatch):
    assert not network._is_private(ipaddress.ip_address(ip))
    _resolve_to(monkeypatch, "target.example", ip)
    ok, err = validate_url_target("http://target.example/")
    assert ok, err


def test_whitelist_still_overrides_and_resets(monkeypatch):
    addr = ipaddress.ip_address("100.64.0.1")
    assert network._is_private(addr)
    configure_ssrf_whitelist(["100.64.0.0/10"])
    assert not network._is_private(addr)
    configure_ssrf_whitelist([])
    assert network._is_private(addr)


def test_whitelist_applies_to_normalised_nat64():
    configure_ssrf_whitelist(["10.0.0.0/8"])
    assert not network._is_private(ipaddress.ip_address("64:ff9b::a00:1"))


# --- validate_resolved_url (redirect targets) ---


def test_redirect_to_unresolvable_host_is_blocked(monkeypatch):
    _no_resolution(monkeypatch)
    ok, msg = validate_resolved_url("http://nope.invalid/")
    assert not ok and msg


@pytest.mark.parametrize("url", ["http://[::1/", "http:///path", "not a url"])
def test_redirect_to_malformed_url_is_blocked(url: str, monkeypatch):
    _no_resolution(monkeypatch)
    ok, msg = validate_resolved_url(url)
    assert not ok and msg


def test_redirect_to_public_host_allowed(monkeypatch):
    _resolve_to(monkeypatch, "public.example", "8.8.8.8")
    assert validate_resolved_url("https://public.example/x") == (True, "")


def test_redirect_to_new_range_literal_blocked():
    ok, _ = validate_resolved_url("http://198.18.0.1/")
    assert not ok


def test_initial_unresolvable_host_still_blocked(monkeypatch):
    _no_resolution(monkeypatch)
    ok, _ = validate_url_target("http://nope.invalid/")
    assert not ok

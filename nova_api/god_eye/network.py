"""Bounded HTTPS client with DNS and redirect SSRF defenses."""
from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urljoin, urlsplit

import httpx

MAX_RESPONSE_BYTES = 2_000_000
MAX_REDIRECTS = 3
TIMEOUT_SECONDS = 8.0


class NetworkPolicyError(ValueError):
    pass


def validate_url(url: str, allowed_hosts: set[str]) -> str:
    parsed = urlsplit(url)
    host = (parsed.hostname or "").rstrip(".").casefold()
    if parsed.scheme != "https" or not host or parsed.username or parsed.password or parsed.port not in (None, 443):
        raise NetworkPolicyError("only allowlisted HTTPS URLs on port 443 are accepted")
    if host not in allowed_hosts and not any(host.endswith("." + allowed) for allowed in allowed_hosts):
        raise NetworkPolicyError("host is not allowlisted")
    try:
        addresses = {row[4][0] for row in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)}
    except socket.gaierror as error:
        raise NetworkPolicyError("DNS resolution failed") from error
    if not addresses or any(not ipaddress.ip_address(address).is_global for address in addresses):
        raise NetworkPolicyError("non-public address refused")
    return parsed.geturl()


class SafeHttpClient:
    def __init__(self, allowed_hosts: set[str], *, transport: httpx.BaseTransport | None = None) -> None:
        self.allowed_hosts = {host.casefold() for host in allowed_hosts}
        self.transport = transport

    def get_bytes(self, url: str, *, accept: str = "application/json",
                  headers: dict[str, str] | None = None) -> tuple[bytes, str, str]:
        current = url
        with httpx.Client(timeout=TIMEOUT_SECONDS, follow_redirects=False, transport=self.transport,
                          headers={"User-Agent": "Nova-God-Eyes/3", "Accept": accept, **(headers or {})}) as client:
            for count in range(MAX_REDIRECTS + 1):
                safe_url = validate_url(current, self.allowed_hosts)
                with client.stream("GET", safe_url) as response:
                    if response.status_code in {301, 302, 303, 307, 308}:
                        if count == MAX_REDIRECTS or not response.headers.get("location"):
                            raise NetworkPolicyError("invalid redirect chain")
                        current = urljoin(safe_url, response.headers["location"])
                        continue
                    response.raise_for_status()
                    body = bytearray()
                    for chunk in response.iter_bytes():
                        body.extend(chunk)
                        if len(body) > MAX_RESPONSE_BYTES:
                            raise NetworkPolicyError("response exceeds size limit")
                    return bytes(body), response.headers.get("content-type", ""), str(response.url)
        raise NetworkPolicyError("request failed")

    def get_json(self, url: str, headers: dict[str, str] | None = None) -> tuple[object, str]:
        import json
        body, content_type, final_url = self.get_bytes(url, headers=headers)
        if "json" not in content_type.lower():
            raise NetworkPolicyError("unexpected content type")
        return json.loads(body), final_url

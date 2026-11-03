"""Which URLs the platform may connect to on behalf of a workspace admin.

A workspace admin supplies server URLs, and they are not trusted with the internal network, so
this is SSRF protection. Checked at registration and again before each connection, because DNS
answers change.

Known limit: the check and the connection resolve the name separately, so an attacker controlling
DNS with a very short TTL can still rebind between them. Closing that needs a pinned-address
transport, or egress rules at the network layer. See docs/SECURITY_MODEL.md section 10.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from urllib.parse import urlsplit


class BlockedUrl(ValueError):
    """The URL is not allowed. The message is written for the admin who supplied it."""


def _is_public(address: str) -> bool:
    # Windows may append a scope to IPv6 results, for example fe80::1%12.
    ip = ipaddress.ip_address(address.split("%", 1)[0])
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip.is_global


async def check_server_url(url: str, *, allowed_private_hosts: frozenset[str]) -> None:
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"}:
        raise BlockedUrl("Server URL must use http or https")
    if not parts.hostname:
        raise BlockedUrl("Server URL has no host")
    # No credential store exists yet, and secrets in a URL end up in logs and API responses.
    if parts.username or parts.password or parts.query or parts.fragment:
        raise BlockedUrl("Server URL must not contain credentials, a query or a fragment")
    host = parts.hostname.lower()
    if host in allowed_private_hosts:
        return
    if parts.scheme != "https":
        raise BlockedUrl("Server URL must use https")
    try:
        answers = await asyncio.get_running_loop().getaddrinfo(
            host, parts.port or 443, type=socket.SOCK_STREAM
        )
    except OSError as exc:
        raise BlockedUrl("Server host could not be resolved") from exc
    # Every answer must be public: a client may connect to any one of them.
    if not answers or not all(_is_public(str(answer[4][0])) for answer in answers):
        raise BlockedUrl("Server host resolves to a non-public address")

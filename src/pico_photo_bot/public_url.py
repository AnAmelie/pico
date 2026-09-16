from __future__ import annotations

import asyncio
import ipaddress
import socket
from typing import Awaitable, Callable
from urllib.parse import urlsplit, urlunsplit

Resolver = Callable[[str, int], Awaitable[list[str]]]


class PublicUrlError(ValueError):
    """A URL is malformed, unresolvable, or targets a non-public network."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def normalize_public_http_url(value: str) -> str:
    clean = value.strip()
    try:
        parsed = urlsplit(clean)
        port = parsed.port
    except ValueError as exc:
        raise PublicUrlError("invalid_port", "URL contains an invalid port") from exc
    scheme = parsed.scheme.casefold()
    if scheme not in {"http", "https"}:
        raise PublicUrlError("scheme", "URL must use http or https")
    if parsed.username is not None or parsed.password is not None:
        raise PublicUrlError("credentials", "URL must not contain credentials")
    if not parsed.hostname:
        raise PublicUrlError("hostname", "URL must contain a hostname")
    host = parsed.hostname.casefold()
    try:
        ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        try:
            host = host.encode("idna").decode("ascii")
        except UnicodeError as exc:
            raise PublicUrlError("hostname", "URL contains an invalid hostname") from exc
    default_port = 443 if scheme == "https" else 80
    bracketed_host = f"[{host}]" if ":" in host and not host.startswith("[") else host
    netloc = bracketed_host if port in {None, default_port} else f"{bracketed_host}:{port}"
    return urlunsplit((scheme, netloc, parsed.path or "/", parsed.query, ""))


async def validate_public_http_url(url: str, resolver: Resolver) -> None:
    normalized = normalize_public_http_url(url)
    parsed = urlsplit(normalized)
    assert parsed.hostname is not None
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        addresses = await resolver(parsed.hostname, port)
    except OSError as exc:
        raise PublicUrlError("resolution_failed", "URL hostname could not be resolved") from exc
    if not addresses:
        raise PublicUrlError("no_addresses", "URL hostname did not resolve")
    for address in addresses:
        try:
            ip = ipaddress.ip_address(address)
        except ValueError as exc:
            raise PublicUrlError(
                "resolution_failed", "URL hostname returned an invalid address"
            ) from exc
        if (
            ip.is_loopback
            or ip.is_private
            or ip.is_link_local
            or ip.is_multicast
            or ip.is_unspecified
            or ip.is_reserved
        ):
            raise PublicUrlError(
                "non_public", "URL hostname resolves to a non-public address"
            )


async def resolve_host(hostname: str, port: int) -> list[str]:
    records = await asyncio.to_thread(
        socket.getaddrinfo, hostname, port, type=socket.SOCK_STREAM
    )
    return list(dict.fromkeys(record[4][0] for record in records))

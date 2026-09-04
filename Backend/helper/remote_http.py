"""Bounded, public-network-only HTTP access for administrator-added sources."""
from __future__ import annotations

import asyncio
import ipaddress
import socket
from urllib.parse import urljoin

import httpx


class UnsafeRemoteURL(ValueError):
    pass


def public_url(url: str) -> bool:
    try:
        parsed = httpx.URL(url)
        host = parsed.host.rstrip(".").lower()
        if parsed.scheme not in {"http", "https"} or not host or parsed.userinfo:
            return False
        if host == "localhost" or host.endswith(".localhost") or "%" in host:
            return False
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            return True  # Hostnames also require DNS validation before connecting.
        return address.is_global and not address.is_multicast
    except (ValueError, httpx.InvalidURL):
        return False


async def resolve_public_address(host: str, port: int) -> str:
    try:
        records = await asyncio.wait_for(
            asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM),
            timeout=10,
        )
    except (OSError, asyncio.TimeoutError) as exc:
        raise UnsafeRemoteURL("Source hostname could not be resolved") from exc
    addresses = [ipaddress.ip_address(record[4][0]) for record in records]
    if not addresses or any(not a.is_global or a.is_multicast for a in addresses):
        raise UnsafeRemoteURL("Source must resolve only to public IP addresses")
    # Prefer IPv4 on hosts without IPv6 egress. Pin the validated address to avoid
    # a second DNS lookup (and DNS rebinding) in the HTTP transport.
    addresses.sort(key=lambda address: address.version)
    return str(addresses[0])


async def open_remote(client: httpx.AsyncClient, method: str, url: str,
                      headers: dict | None = None) -> httpx.Response:
    """Return an unread response; caller owns aclose(). Check every redirect hop.

    Clients must use trust_env=False and must not carry credentials/cookies.
    Only Host, Range, If-Range, User-Agent, and Accept-Encoding are forwarded.
    """
    for hop in range(6):
        if not public_url(url):
            raise UnsafeRemoteURL("Invalid or non-public source URL")
        original = httpx.URL(url)
        address = await resolve_public_address(original.host, original.port or (443 if original.scheme == "https" else 80))
        allowed = {"range", "if-range", "user-agent", "accept-encoding"}
        outgoing = {k: v for k, v in (headers or {}).items() if k.lower() in allowed}
        outgoing["Host"] = original.netloc.decode("ascii")
        outgoing["Accept-Encoding"] = "identity"
        # Disable cross-host connection reuse for IP-pinned TLS connections.
        outgoing["Connection"] = "close"
        request = client.build_request(method, original.copy_with(host=address), headers=outgoing,
                                       extensions={"sni_hostname": original.host})
        request.headers.pop("cookie", None)
        response = await client.send(request, stream=True, follow_redirects=False)
        if response.status_code not in {301, 302, 303, 307, 308}:
            response.extensions["source_url"] = str(original)
            return response
        location = response.headers.get("location")
        await response.aclose()
        if not location or hop == 5:
            raise UnsafeRemoteURL("Invalid or excessive source redirects")
        next_url = urljoin(str(original), location)
        if not public_url(next_url):
            raise UnsafeRemoteURL("Invalid or non-public redirect URL")
        if original.scheme == "https" and httpx.URL(next_url).scheme != "https":
            raise UnsafeRemoteURL("Source redirected to an insecure URL")
        url = next_url
    raise UnsafeRemoteURL("Excessive source redirects")


async def read_text(client: httpx.AsyncClient, url: str, limit: int = 4 * 1024 * 1024) -> str:
    response = await open_remote(client, "GET", url)
    try:
        response.raise_for_status()
        if response.headers.get("content-encoding", "identity").lower() != "identity":
            raise ValueError("Compressed index responses are not supported")
        chunks = bytearray()
        async for chunk in response.aiter_raw(chunk_size=64 * 1024):
            if len(chunks) + len(chunk) > limit:
                raise ValueError("Index page exceeds the 4 MiB safety limit")
            chunks.extend(chunk)
        return chunks.decode(response.encoding or "utf-8", errors="replace")
    finally:
        await response.aclose()

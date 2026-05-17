"""Fetch a URL and return readable text — Phase 2 tool.

Pairs with web_search: the search returns links, fetch_url reads them.
Search snippets are too short and often stale for live data (currency
rates, weather, prices); a fetch + main-content extract closes that gap.

Design:
- primp.AsyncClient for the GET. Plain httpx works for many sites but
  Wikipedia / Cloudflare-fronted sites fingerprint TLS + HTTP/2 frame
  ordering and 403 anything that isn't a real browser. primp impersonates
  Chrome's fingerprint and is already a transitive dep via `ddgs`
  (cf. ADR 0005), so we reuse it instead of carrying a second client.
- trafilatura for boilerplate-stripped main content. Raw HTML floods
  the model context with nav/scripts/ads; trafilatura turns a typical
  article page into a few KB of paragraphs. For pages it can't parse
  (search engines, SPA shells) it returns empty — we surface that.
- http(s) only, public hosts only. We resolve the hostname and reject
  loopback / private / link-local / reserved ranges so the agent can
  not be talked into hitting localhost services or RFC1918 subnets.
- Approval-gated. Every fetch is one outbound request the user can see
  in the transcript before it goes out (ADR 0006).
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import socket
from urllib.parse import urlparse

import primp
import trafilatura

log = logging.getLogger("pa.tool.fetch_url")

MAX_DOWNLOAD_BYTES = 2_000_000     # hard cap on what we'll read off the wire
MAX_EXTRACTED_CHARS = 12_000       # hard cap on what we feed back to the model
TIMEOUT_S = 15.0
IMPERSONATE = "chrome"             # primp browser fingerprint; "chrome" is well-supported


NAME = "fetch_url"
DESCRIPTION = (
    "Fetch a web page over HTTP(S) and return its main text content "
    "with boilerplate (nav, ads, footer) stripped. Use this after "
    "web_search when a snippet is not enough — for example to read "
    "the actual currency rate, weather details, or article body "
    "behind a search result. Only http(s) URLs to public hosts."
)
PARAMETERS: dict = {
    "type": "object",
    "properties": {
        "url": {
            "type": "string",
            "description": "Absolute http(s) URL to fetch.",
        },
    },
    "required": ["url"],
}


def _check_public_host(host: str) -> None:
    """Resolve `host` and reject any address that's loopback / private /
    link-local / multicast / reserved. Raises ValueError on rejection.

    DNS resolution is sync; callers should run this via asyncio.to_thread.
    """
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as e:
        raise ValueError(f"could not resolve host {host!r}: {e}") from e

    for info in infos:
        addr = info[4][0]
        ip = ipaddress.ip_address(addr)
        if (
            ip.is_loopback
            or ip.is_private
            or ip.is_link_local
            or ip.is_multicast
            or ip.is_reserved
            or ip.is_unspecified
        ):
            raise ValueError(
                f"refusing to fetch non-public host {host!r} "
                f"(resolved to {addr})"
            )


def _validate(url: str) -> str:
    """Parse + scheme-check. Returns the hostname for the public-host check."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"unsupported scheme {parsed.scheme!r}; need http or https")
    if not parsed.hostname:
        raise ValueError(f"url has no host: {url!r}")
    return parsed.hostname


def _extract(html: str) -> str:
    text = trafilatura.extract(html, include_comments=False, include_tables=True)
    return text or ""


async def fetch_url(url: str) -> str:
    host = _validate(url)
    await asyncio.to_thread(_check_public_host, host)

    client = primp.AsyncClient(impersonate=IMPERSONATE, timeout=TIMEOUT_S)
    try:
        r = await client.get(url)
    except primp.PrimpError as e:
        raise ValueError(f"fetch failed: {type(e).__name__}: {e}") from e

    if r.status_code >= 400:
        raise ValueError(f"http {r.status_code} from {url}")

    ctype = (r.headers.get("content-type") or "").lower()
    if "html" not in ctype and "text/plain" not in ctype:
        raise ValueError(f"unsupported content-type {ctype!r} (only html/text)")

    body = r.content[:MAX_DOWNLOAD_BYTES]
    decoded = body.decode(r.encoding or "utf-8", errors="replace")

    if "text/plain" in ctype:
        text = decoded
    else:
        text = await asyncio.to_thread(_extract, decoded)

    text = text.strip()
    if not text:
        return f"Fetched {url} ({len(body)} bytes) but extracted no readable text."

    truncated = ""
    if len(text) > MAX_EXTRACTED_CHARS:
        truncated = (
            f"\n\n[... truncated; full extract was {len(text)} chars, "
            f"showing first {MAX_EXTRACTED_CHARS}]"
        )
        text = text[:MAX_EXTRACTED_CHARS]

    log.info("fetch_url url=%s status=%d chars=%d", url, r.status_code, len(text))
    return f"Fetched {url}\n\n{text}{truncated}"

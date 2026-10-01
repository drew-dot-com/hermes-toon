"""The ``toon`` web provider: extract only, paid per URL through a local sidecar.

Env: ``TOON_PAYER_URL`` (required), the anonfetch payer sidecar, e.g.
``http://127.0.0.1:3502``. The sidecar holds the TOON payment channel and the
spending rules; this provider only asks it for pages and reports what came back.

Each URL is one ``GET {TOON_PAYER_URL}/extract?url=...``. A 200 carries the page
as markdown (``content``) with ``content_hash`` (sha256 of the canonical markdown
under wuzzy/crawl v1) and ``raw_hash`` (sha256 of the bytes the origin served),
fetched by the node through an Anyone network exit. Anything else is returned as
that URL's ``error`` entry, never raised.

Hermes hands the model only ``url``, ``title``, ``content`` and ``error`` per page
(``tools/web_tools_truncate._trim_results``), so ``metadata`` never reaches the
agent. The receipt therefore also rides in ``content``, as a footer after the
page; ``raw_content`` stays the page alone, which is what ``content_hash`` covers.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from plugins.web._common import (
    BaseWebSearchProvider,
    document,
    page_error,
    provider_env,
    run_extract,
    setup_schema,
)

logger = logging.getLogger(__name__)

PAYER_ENV = "TOON_PAYER_URL"
# The node's fetch has its own 25 s timeout plus a channel round trip; a first
# call may also open a channel on chain.
REQUEST_TIMEOUT_S = 90.0
# Leads every error, so a model reading a failed paid fetch cannot mistake it for a page.
NO_PAGE = "No page content was retrieved."


def payer_url() -> str:
    return provider_env(PAYER_ENV).rstrip("/")


def _reason(payload: dict[str, Any], status: int) -> str:
    code = payload.get("code") or f"HTTP {status}"
    error = payload.get("error") or ""
    return f"{code}: {error}".strip(": ")


def _price_note(payload: dict[str, Any]) -> str:
    price = payload.get("price") or {}
    units = price.get("units")
    if units is None:
        return ""
    try:
        usd = int(units) / 10 ** int(price.get("decimals", 6))
    except (TypeError, ValueError):
        return f"{units} units"
    return f"${usd:.6f} {price.get('asset', '')}".strip()


def receipt(payload: dict[str, Any]) -> str:
    """The footer appended to ``content``: what was paid, how it was fetched, and the hash."""
    parts = [f"paid {_price_note(payload) or 'unknown'}"]
    node = payload.get("node") or {}
    if node.get("destination"):
        parts[0] += f" to {node['destination']}"
    if payload.get("exit"):
        parts.append(f"fetched through an {str(payload['exit']).capitalize()} network exit")
    if payload.get("content_hash"):
        scope = "covers the whole page; the text above is truncated" if payload.get("truncated") else "covers the page above this receipt"
        parts.append(f"content_hash sha256:{payload['content_hash']} ({scope}, wuzzy/crawl v1)")
    if payload.get("job_id"):
        parts.append(f"job {payload['job_id']}")
    if payload.get("fetched_at"):
        parts.append(f"at {payload['fetched_at']}")
    return "TOON receipt: " + "; ".join(parts) + "."


def extract_one(client: httpx.Client, base: str, url: str) -> dict[str, Any]:
    """One paid extract; returns a Hermes extract entry (document or page_error)."""
    try:
        response = client.get(f"{base}/extract", params={"url": url}, timeout=REQUEST_TIMEOUT_S)
    except httpx.RequestError as exc:
        return page_error(url, f"{NO_PAGE} TOON payer sidecar unreachable at {base}: {exc}")
    try:
        payload = response.json()
    except ValueError:
        return page_error(url, f"{NO_PAGE} TOON payer sidecar answered non-JSON (HTTP {response.status_code})")
    if response.status_code != 200 or not payload.get("ok"):
        return page_error(url, f"{NO_PAGE} TOON extract refused: {_reason(payload, response.status_code)}")
    status = int(payload.get("status") or 0)
    if status >= 400:
        return page_error(url, f"origin answered HTTP {status} (paid {_price_note(payload)})")
    if payload.get("thin"):
        return page_error(url, f"page too thin to extract (paid {_price_note(payload)})")

    page = payload.get("content") or ""
    entry = document(payload.get("final_url") or url, payload.get("title") or "", page, source_url=url)
    entry["content"] = f"{page.rstrip()}\n\n---\n{receipt(payload)}\n"
    entry["raw_content"] = page
    entry["metadata"].update(
        {
            "content_hash": payload.get("content_hash"),
            "raw_hash": payload.get("raw_hash"),
            "protocol": payload.get("protocol"),
            "protocol_version": payload.get("protocol_version"),
            "exit": payload.get("exit"),
            "fetched_at": payload.get("fetched_at"),
            "truncated": bool(payload.get("truncated")),
            "price": payload.get("price"),
            "node": payload.get("node"),
            "job_id": payload.get("job_id"),
        }
    )
    return entry


class ToonWebProvider(BaseWebSearchProvider):
    """Extract-only, paid, never keyless: ``is_available`` is exactly "a sidecar URL is set"."""

    NAME = "toon"
    DISPLAY_NAME = "TOON paid fetch"
    KEY_ENV = PAYER_ENV
    EXTRACT = True
    KEYLESS = False

    def supports_search(self) -> bool:
        return False

    def search(self, query: str, limit: int = 5) -> dict[str, Any]:
        return {"success": False, "error": "toon is an extract-only provider; set web.search_backend to another provider"}

    def extract(self, urls: list[str], **kwargs: Any) -> list[dict[str, Any]]:
        def _body() -> list[dict[str, Any]]:
            base = payer_url()
            if not base:
                raise ValueError(f"{PAYER_ENV} is not set: run the anonfetch payer sidecar and point {PAYER_ENV} at it")
            logger.info("TOON extract: %d URL(s) via %s", len(urls), base)
            with httpx.Client() as client:
                return [extract_one(client, base, u) for u in urls]

        return run_extract("TOON", logger, list(urls), _body)

    def get_setup_schema(self) -> dict[str, Any]:
        return setup_schema(
            "TOON paid fetch · Anyone exits",
            "paid",
            "Pages fetched through Anyone network exits by a TOON node, about $0.001 each from a local "
            "payment channel; markdown back with a reproducible content hash. Extract only.",
            PAYER_ENV,
            "anonfetch payer sidecar URL (e.g. http://127.0.0.1:3502)",
            "https://github.com/drew-dot-com/hermes-toon",
            web_tier="paid",
        )

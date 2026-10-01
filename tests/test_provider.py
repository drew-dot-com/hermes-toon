"""The provider against a fake sidecar: what it sends, and how each answer maps."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

import pytest

ANSWERS: dict[str, tuple[int, dict]] = {}
SEEN: list[str] = []

GOOD = {
    "ok": True, "url": "https://a.example/p", "final_url": "https://a.example/p", "status": 200,
    "title": "A page", "content": "## Heading\n\nBody text.\n", "content_hash": "ab" * 32, "raw_hash": "cd" * 32,
    "protocol": "wuzzy/crawl-experimental", "protocol_version": 1, "format": "html", "thin": False,
    "truncated": False, "exit": "anyone", "fetched_at": "2026-09-28T12:00:00.000Z", "job_id": "j1",
    "price": {"units": "1000", "asset": "USDC", "decimals": 6, "chain": "solana"},
    "node": {"edge": "https://edge", "destination": "g.drew.anon"},
}


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        u = urlparse(self.path)
        SEEN.append(self.path)
        target = parse_qs(u.query).get("url", [""])[0]
        status, body = ANSWERS.get(target, (404, {"ok": False, "code": "not_found", "error": "no answer scripted"}))
        raw = json.dumps(body).encode() if isinstance(body, dict) else body
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *_):
        pass


@pytest.fixture(scope="module")
def sidecar():
    srv = HTTPServer(("127.0.0.1", 0), Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()


@pytest.fixture
def provider(sidecar, monkeypatch):
    monkeypatch.setenv("TOON_PAYER_URL", sidecar + "/")
    from hermes_toon.provider import ToonWebProvider

    return ToonWebProvider()


def test_capabilities(provider, monkeypatch):
    assert provider.name == "toon"
    assert provider.supports_extract() and not provider.supports_search()
    assert provider.is_available()
    assert not provider.is_keyless_available()
    monkeypatch.delenv("TOON_PAYER_URL")
    assert not provider.is_available()


def test_extract_maps_a_paid_page(provider):
    ANSWERS["https://a.example/p"] = (200, GOOD)
    [entry] = provider.extract(["https://a.example/p"])
    assert "error" not in entry
    assert entry["url"] == "https://a.example/p"
    assert entry["title"] == "A page"
    assert entry["raw_content"] == entry["content"]  # Hermes rebuilds content from raw_content
    page, footer = entry["content"].split("\n\n---\n")
    assert page == GOOD["content"].rstrip()
    assert footer.startswith("TOON receipt: paid $0.001000 USDC to g.drew.anon;")
    assert "Anyone network exit" in footer and f"sha256:{'ab' * 32}" in footer and "job j1" in footer
    meta = entry["metadata"]
    assert meta["sourceURL"] == "https://a.example/p"
    assert meta["content_hash"] == "ab" * 32
    assert meta["exit"] == "anyone"
    assert meta["price"]["units"] == "1000"
    assert SEEN[-1].startswith("/extract?url=https%3A%2F%2Fa.example%2Fp")


def test_refusals_become_per_url_errors(provider):
    ANSWERS["https://a.example/cap"] = (402, {"ok": False, "code": "budget_exhausted", "error": "daily cap 100000 reached"})
    ANSWERS["https://a.example/404"] = (200, {**GOOD, "status": 404, "content": ""})
    ANSWERS["https://a.example/thin"] = (200, {**GOOD, "thin": True, "content": "", "content_hash": None})
    cap, nf, thin, good = provider.extract(["https://a.example/cap", "https://a.example/404", "https://a.example/thin", "https://a.example/p"])
    assert "budget_exhausted" in cap["error"] and "daily cap" in cap["error"]
    assert "HTTP 404" in nf["error"] and "$0.001000" in nf["error"]
    assert "thin" in thin["error"]
    assert "error" not in good
    for failed in (cap, nf, thin):
        assert not failed["content"]


def test_errors_say_no_page_was_retrieved(provider, monkeypatch):
    [cap] = provider.extract(["https://a.example/cap"])
    assert cap["error"].startswith("No page content was retrieved.")
    monkeypatch.setenv("TOON_PAYER_URL", "http://127.0.0.1:9")
    [down] = provider.extract(["https://a.example/p"])
    assert down["error"].startswith("No page content was retrieved.")


def test_receipt_flags_truncation():
    from hermes_toon.provider import receipt

    assert "the text above is truncated" in receipt({**GOOD, "truncated": True})
    assert "covers the page above this receipt" in receipt(GOOD)


def test_unreachable_sidecar_is_an_error_not_an_exception(monkeypatch):
    monkeypatch.setenv("TOON_PAYER_URL", "http://127.0.0.1:9")
    from hermes_toon.provider import ToonWebProvider

    [entry] = ToonWebProvider().extract(["https://a.example/p"])
    assert "unreachable" in entry["error"]


def test_missing_env_is_a_clear_error(monkeypatch):
    monkeypatch.delenv("TOON_PAYER_URL", raising=False)
    from hermes_toon.provider import ToonWebProvider

    [entry] = ToonWebProvider().extract(["https://a.example/p"])
    assert "TOON_PAYER_URL" in entry["error"]


def test_setup_schema_is_paid(provider):
    schema = provider.get_setup_schema()
    assert schema["badge"] == "paid"
    assert schema["env_vars"][0]["key"] == "TOON_PAYER_URL"


@pytest.mark.skipif(not __import__("os").environ.get("HERMES_SRC"), reason="needs a real Hermes checkout")
def test_receipt_survives_hermes_result_shaping(provider):
    """What the model sees: web_extract_tool runs _truncate_results then _trim_results."""
    from tools.web_tools_truncate import _effective_char_limit, _trim_results, _truncate_results

    ANSWERS["https://a.example/p"] = (200, GOOD)
    results = provider.extract(["https://a.example/p"])
    _truncate_results(results, _effective_char_limit(None), {"processing_applied": [], "pages": []})
    [seen] = _trim_results(results)
    assert "TOON receipt: paid $0.001000 USDC" in seen["content"]
    assert f"sha256:{'ab' * 32}" in seen["content"]

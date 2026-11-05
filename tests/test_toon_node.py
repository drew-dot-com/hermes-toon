"""toon-node: the manifest matches what register() does, the pure parts behave, and the three tools
talk to the payer service (faked with httpx.MockTransport) and never to anything else."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
import yaml

import toon_node
from toon_node import tools

ROOT = Path(__file__).resolve().parents[1]
PAYER = "http://127.0.0.1:3510"
PUBKEY = "14" * 32


class Ctx:
    def __init__(self):
        self.tools = {}

    def register_tool(self, name, toolset, schema, handler, **kw):
        self.tools[name] = (toolset, schema, kw)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch, tmp_path):
    for name in tools.SETTINGS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("TOON_STATE_DIR", str(tmp_path / "state"))
    tools._facts_cache.clear()
    return tmp_path


class FakeService:
    """Answers the payer service's endpoints and records every request it saw."""

    def __init__(self, monkeypatch, *, post=None, health=None, read=None):
        self.seen = []
        self.post = post or (200, {
            "ok": True, "event": {"id": "e" * 64, "pubkey": PUBKEY, "kind": 1, "content": "x"},
            "relay_status": 200, "relay_answer": {"eventId": "e" * 64}, "paid": "1000",
            "route": "g.drew.relay", "relay": "wss://relay.mainnet.toonprotocol.dev",
        })
        self.health = health or (200, {
            "ok": True, "service": "charter-payer", "version": "0.1.0", "uptime_s": 5,
            "connector": "https://connector.example/ilp", "nostr_pubkey": PUBKEY,
            "routes": {"extract": "g.drew.anon", "relay": "g.drew.relay"},
            "relay": "wss://relay.mainnet.toonprotocol.dev",
            "budget": {"day": "2026-11-05", "spent": "1200", "cap": "100000", "remaining": "98800"},
            "channel": {
                "chain": "evm", "payer": "0x" + "b8" * 20, "headroom": "1990000",
                "channels": [{"id": "0xch", "depositTotal": "2000000", "signed": "10000"}],
            },
        })
        self.read = read or (200, {
            "ok": True, "relay": "wss://relay.mainnet.toonprotocol.dev", "count": 2, "events": [
                {"id": "a" * 64, "pubkey": PUBKEY, "kind": 1, "created_at": 100, "tags": [["t", "charter"]],
                 "content": "mine"},
                {"id": "b" * 64, "pubkey": "c" * 64, "kind": 30617, "created_at": 200,
                 "tags": [["d", "repo"], ["p", "x"]], "content": ""},
            ],
        })

        def handle(request: httpx.Request) -> httpx.Response:
            self.seen.append(request)
            path = request.url.path
            if path == "/relay/post":
                status, body = self.post
            elif path == "/health":
                status, body = self.health
            elif path == "/relay/read":
                status, body = self.read
            else:
                status, body = 404, {"ok": False, "code": "not_found", "error": path}
            return httpx.Response(status, json=body)

        client = httpx.Client(transport=httpx.MockTransport(handle))

        def via(method):
            # The tools pass trust_env=False, a Client setting, not a request one.
            def call(url, **kw):
                return getattr(client, method)(url, **{k: v for k, v in kw.items() if k != "trust_env"})

            return call

        monkeypatch.setattr(tools.httpx, "get", via("get"))
        monkeypatch.setattr(tools.httpx, "post", via("post"))
        monkeypatch.setenv("TOON_PAYER_URL", PAYER + "/")


def test_manifest_lists_exactly_the_registered_tools_and_the_version():
    ctx = Ctx()
    toon_node.register(ctx)
    m = yaml.safe_load((ROOT / "src/toon_node/plugin.yaml").read_text())
    assert m["name"] == "toon-node"
    assert m["version"] == toon_node.__version__ == "0.2.0"
    assert sorted(m["provides_tools"]) == sorted(ctx.tools)
    assert {t[0] for t in ctx.tools.values()} == {"toon"}
    assert all(t[1]["name"] == n for n, t in ctx.tools.items())
    assert ctx.tools["toon_read"][2].get("is_async") is True
    assert all(t[2].get("check_fn") is tools.available for t in ctx.tools.values())
    assert [e["name"] for e in m["requires_env"]] == ["TOON_PAYER_URL"]
    assert set(m["optional_env"]) | {"TOON_PAYER_URL"} == set(tools.SETTINGS)
    assert "Disclosure" in m["description"]
    assert "websockets" not in json.dumps(m.get("python_dependencies"))


def test_available_is_exactly_a_payer_url(monkeypatch):
    assert tools.available() is False
    monkeypatch.setenv("TOON_PAYER_URL", "http://127.0.0.1:3510")
    assert tools.available() is True


def test_read_filter_is_bounded_and_drops_bad_ids():
    f = tools.build_filter({"limit": 500, "authors": ["zz", "a" * 64], "ids": ["b" * 63]}, None)
    assert f == {"limit": tools.MAX_READ, "authors": ["a" * 64]}


def test_read_defaults_to_every_kind():
    # A rig push lands as kind 30617/30618, not kind 1; a kind-1-only read missed it (10-20).
    assert "kinds" not in tools.build_filter({}, None)


def test_read_kinds_are_whole_numbers_only():
    f = tools.build_filter({"kinds": [1, "30617", "x", -2, 1.5, 70000]}, None)
    assert f["kinds"] == [1, 30617]


def test_event_summary_names_the_kind_and_keeps_useful_tags():
    e = {"id": "i", "pubkey": "p", "kind": 30618, "created_at": 0, "content": "",
         "tags": [["d", "toon_cli"], ["refs/heads/main", "abc"], ["p", "x" * 64]]}
    s = tools.summarize_event(e, None)
    assert s["kind"] == 30618
    assert "git" in s["kind_name"]
    assert s["tags"] == [["d", "toon_cli"], ["refs/heads/main", "abc"]]


def test_mine_uses_the_payer_pubkey():
    f = tools.build_filter({"mine": True, "authors": ["a" * 64]}, "c" * 64)
    assert f["authors"] == ["c" * 64]


# ---------- toon_post ----------


def test_post_refuses_empty_or_oversize_content_without_asking_the_service(monkeypatch, tmp_path):
    svc = FakeService(monkeypatch)
    assert "empty" in json.loads(tools.toon_post({"content": "  "}))["error"]
    assert "over 1000" in json.loads(tools.toon_post({"content": "x" * 1001}))["error"]
    assert svc.seen == []
    assert not (tmp_path / "state").exists()


def test_post_asks_the_service_to_sign_and_pay_and_reports_the_receipt(monkeypatch, tmp_path):
    svc = FakeService(monkeypatch)
    out = json.loads(tools.toon_post({"content": "hello from the box"}))
    assert out["outcome"] == "stored"
    assert out["event_id"] == "e" * 64 and out["author"] == PUBKEY
    assert out["paid_base_units"] == 1000 and out["paid_usdc"] == 0.001
    assert out["relay"] == "wss://relay.mainnet.toonprotocol.dev" and out["route"] == "g.drew.relay"
    assert out["posts_today"] == 1 and out["post_cap"] == 50
    (req,) = svc.seen
    assert req.method == "POST" and str(req.url) == PAYER + "/relay/post"
    assert json.loads(req.content) == {"content": "hello from the box", "tags": [["client", "toon-node"]]}
    today = datetime.now(UTC).strftime("%Y-%m-%d")
    assert (tmp_path / "state" / f"posts-{today}").read_text() == "1\n"


def test_post_counts_the_day_before_asking_and_stops_at_the_cap(monkeypatch, tmp_path):
    monkeypatch.setenv("TOON_POST_CAP", "2")
    svc = FakeService(monkeypatch)
    assert json.loads(tools.toon_post({"content": "one"}))["outcome"] == "stored"
    assert json.loads(tools.toon_post({"content": "two"}))["post_cap"] == 2
    err = json.loads(tools.toon_post({"content": "three"}))["error"]
    assert "cap of 2 posts" in err
    assert len(svc.seen) == 2


def test_post_reports_a_relay_rejection_as_paid_and_a_dead_service_plainly(monkeypatch):
    rejected = {"ok": False, "event": {}, "relay_status": 422, "paid": "1000",
                "relay_answer": {"error": "Invalid event signature"}}
    svc = FakeService(monkeypatch, post=(502, rejected))
    out = json.loads(tools.toon_post({"content": "x"}))
    assert out["error"].startswith("post refused: relay answered HTTP 422")
    assert out["paid_base_units"] == 1000
    capped = {"ok": False, "code": "budget_exhausted", "error": "daily cap 100000 reached"}
    svc2 = FakeService(monkeypatch, post=(402, capped))
    out = json.loads(tools.toon_post({"content": "x"}))
    assert out["error"] == "post refused: budget_exhausted: daily cap 100000 reached"
    assert "paid_base_units" not in out
    assert len(svc.seen) == 1 and len(svc2.seen) == 1


def test_post_without_a_payer_url_says_so(monkeypatch, tmp_path):
    out = json.loads(tools.toon_post({"content": "x"}))
    assert "TOON_PAYER_URL" in out["error"]
    assert not (tmp_path / "state").exists()


# ---------- toon_read ----------


def test_read_goes_through_the_service_with_the_filter_and_summarizes_newest_first(monkeypatch):
    svc = FakeService(monkeypatch)
    out = json.loads(asyncio.run(tools.toon_read({"kinds": [1, 30617], "limit": 5})))
    assert out["relay"] == "wss://relay.mainnet.toonprotocol.dev"
    assert out["filter"] == {"limit": 5, "kinds": [1, 30617]}
    assert [e["id"] for e in out["events"]] == ["b" * 64, "a" * 64]
    assert out["events"][0]["kind_name"] == "git repository announcement"
    assert out["events"][0]["tags"] == [["d", "repo"]]
    (req,) = svc.seen
    assert req.url.path == "/relay/read"
    assert json.loads(req.url.params["filter"]) == {"limit": 5, "kinds": [1, 30617]}
    assert req.url.params["relay"] == "wss://relay.mainnet.toonprotocol.dev"


def test_read_mine_learns_the_payer_pubkey_from_health_once(monkeypatch):
    svc = FakeService(monkeypatch)
    out = json.loads(asyncio.run(tools.toon_read({"mine": True})))
    assert out["filter"]["authors"] == [PUBKEY]
    assert [e["by_you"] for e in out["events"]] == [False, True]
    asyncio.run(tools.toon_read({"mine": True}))
    assert [r.url.path for r in svc.seen] == ["/health", "/relay/read", "/relay/read"]


def test_read_honours_a_relay_setting_and_reports_a_dead_relay(monkeypatch):
    monkeypatch.setenv("TOON_RELAY_URL", "wss://other.example")
    down = {"ok": False, "code": "relay_unreachable", "error": "connection failed"}
    svc = FakeService(monkeypatch, read=(502, down))
    out = json.loads(asyncio.run(tools.toon_read({})))
    assert out["error"] == "could not read wss://other.example: relay_unreachable: connection failed"
    assert svc.seen[0].url.params["relay"] == "wss://other.example"


# ---------- toon_status ----------


def test_status_reads_health_and_the_wallet_and_speaks_usdc(monkeypatch):
    svc = FakeService(monkeypatch)
    monkeypatch.setattr(tools, "balances", lambda evm: {"base": {"address": evm, "eth": 0.001, "usdc": 0.1}})
    out = json.loads(tools.toon_status({}))
    assert out["network"].startswith("mainnet")
    assert out["wallet"] == {"base": {"address": "0x" + "b8" * 20, "eth": 0.001, "usdc": 0.1}}
    assert out["channels"] == [
        {"id": "0xch", "chain": "evm", "deposit_usdc": 2.0, "signed_usdc": 0.01, "left_usdc": 1.99},
    ]
    assert out["channel_headroom_usdc"] == 1.99
    assert out["daily_cap_usdc"]["spent"] == 0.0012 and out["daily_cap_usdc"]["cap"] == 0.1
    assert out["nostr_pubkey"] == PUBKEY and out["posts_today"] == 0 and out["post_cap"] == 50
    assert [r.url.path for r in svc.seen] == ["/health"]
    json.dumps(out)


def test_status_reports_an_unhealthy_or_absent_service(monkeypatch):
    FakeService(monkeypatch, health=(500, {"ok": False, "code": "error", "error": "boom"}))
    assert json.loads(tools.toon_status({}))["error"] == "payer service unhealthy: error: boom"
    monkeypatch.delenv("TOON_PAYER_URL")
    assert "TOON_PAYER_URL" in json.loads(tools.toon_status({}))["error"]


def test_summary_tolerates_a_health_without_channel_facts():
    s = tools.summarize({"ok": True}, {}, 3, 50)
    assert s["channels"] == [] and s["channel_headroom_usdc"] is None and s["posts_today"] == 3
    json.dumps(s)

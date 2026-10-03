"""toon-node: the manifest matches what register() does, and the pure parts behave."""

from __future__ import annotations

import json
from pathlib import Path

import yaml

from toon_node import tools

ROOT = Path(__file__).resolve().parents[1]


class Ctx:
    def __init__(self):
        self.tools = {}

    def register_tool(self, name, toolset, schema, handler, **kw):
        self.tools[name] = (toolset, schema, kw)


def test_manifest_lists_exactly_the_registered_tools():
    import toon_node

    ctx = Ctx()
    toon_node.register(ctx)
    m = yaml.safe_load((ROOT / "src/toon_node/plugin.yaml").read_text())
    assert m["name"] == "toon-node"
    assert sorted(m["provides_tools"]) == sorted(ctx.tools)
    assert {t[0] for t in ctx.tools.values()} == {"toon"}
    assert all(t[1]["name"] == n for n, t in ctx.tools.items())
    assert ctx.tools["toon_read"][2].get("is_async") is True
    assert "Disclosure" in m["description"]


def test_read_filter_is_bounded_and_drops_bad_ids():
    f = tools.build_filter({"limit": 500, "authors": ["zz", "a" * 64], "ids": ["b" * 63]}, None)
    assert f == {"kinds": [1], "limit": 20, "authors": ["a" * 64]}


def test_mine_uses_the_node_pubkey():
    f = tools.build_filter({"mine": True, "authors": ["a" * 64]}, "c" * 64)
    assert f["authors"] == ["c" * 64]


def test_bridge_only_runs_its_two_commands():
    for bad in ("post; rm -rf /", "status && id", "limit"):
        try:
            tools.bridge(bad)
        except ValueError:
            continue
        raise AssertionError(bad)


def test_summary_turns_base_units_into_usdc():
    raw = {
        "facts": {"network": "mainnet", "agent_identity": "c" * 64},
        "node": {"toon_apps": [{"connector": {"ilp_address": "g.toon.x", "running": True}}]},
        "channels": [{"id": "ch", "chain": "solana", "direction": "outbound", "status": "open",
                      "collateral": 1000000, "watermark": 2000, "landed": 0}],
        "limits": {"per_day": "2000000", "remaining_today": "998000"},
        "posts_today": 1, "post_cap": 50, "relay": "wss://r",
    }
    s = tools.summarize(raw, {})
    assert s["channels"][0]["sent_usdc"] == 0.002
    assert s["spending_limit_usdc"] == {"per_day": 2.0, "remaining_today": 0.998}
    assert s["ilp_address"] == "g.toon.x"
    json.dumps(s)


def test_post_refuses_empty_content_without_calling_the_vm(monkeypatch):
    monkeypatch.setattr(tools, "bridge", lambda *a, **k: (_ for _ in ()).throw(AssertionError("called")))
    assert "empty" in json.loads(tools.toon_post({"content": "  "}))["error"]

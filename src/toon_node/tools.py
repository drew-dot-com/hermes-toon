"""Three tools over a toon agent node: post a note, read a relay, report the node.

Posting and the node's own state go through ``toon-bridge`` inside the VM that
holds the node (see ``vm/toon-bridge``): one fixed command line, with anything
a chat user wrote passed as JSON on stdin. Reading the relay and the chain is
public and free, so it happens here.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import time
from pathlib import Path

import httpx

RELAY_URL = os.environ.get("TOON_RELAY_URL", "wss://relay.167-233-221-236.sslip.io")
LIMACTL = os.environ.get("TOON_LIMACTL", str(Path.home() / ".local/lima/bin/limactl"))
VM = os.environ.get("TOON_VM", "toon")
BRIDGE = os.environ.get("TOON_BRIDGE", "~/bin/toon-bridge")
BASE_RPC = os.environ.get("TOON_BASE_RPC", "https://mainnet.base.org")
SOLANA_RPC = os.environ.get("TOON_SOLANA_RPC", "https://api.mainnet-beta.solana.com")
BASE_USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
SOLANA_USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"

MAX_READ = 20
MAX_CONTENT_SHOWN = 500


def available() -> bool:
    return Path(LIMACTL).is_file()


def bridge(command: str, stdin: dict | None = None, timeout: float = 120) -> dict:
    """Run ``toon-bridge <command>`` in the VM. ``command`` is one of two fixed words."""
    if command not in ("post", "status"):
        raise ValueError(command)
    done = subprocess.run(
        [LIMACTL, "shell", VM, "--", "bash", "-lc", f"{BRIDGE} {command}"],
        input=json.dumps(stdin or {}),
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    text = done.stdout.strip().splitlines()
    if not text:
        return {"error": (done.stderr.strip() or "toon-bridge printed nothing")[-500:]}
    try:
        return json.loads(text[-1])
    except json.JSONDecodeError:
        return {"error": text[-1][-500:]}


# ---------- toon_post ----------

POST_SCHEMA = {
    "name": "toon_post",
    "description": (
        "Publish a short public note (Nostr kind 1) to Drew Pierson's TOON relay from your own "
        "TOON agent node. Each post is a real payment: 1000 base units (0.001 USDC) on Solana "
        "mainnet, sent over your node's payment channel to Drew's node. Capped at 50 posts a day. "
        "Use it only when a user asks you to post something to TOON; never post private data."
    ),
    "parameters": {
        "type": "object",
        "properties": {"content": {"type": "string", "description": "The note, 1 to 1000 characters."}},
        "required": ["content"],
    },
}


def toon_post(args: dict, **_) -> str:
    content = str(args.get("content", "")).strip()
    if not content:
        return json.dumps({"error": "content is empty"})
    out = bridge("post", {"content": content})
    if "error" in out:
        return json.dumps(out)
    event = out.get("event", {})
    return json.dumps({
        "outcome": out.get("outcome"),
        "event_id": event.get("id"),
        "author": event.get("pubkey"),
        "paid_base_units": out.get("paid"),
        "paid_usdc": (out.get("paid") or 0) / 1e6,
        "relay": out.get("relay"),
        "posts_today": out.get("posts_today"),
        "post_cap": out.get("post_cap"),
    })


# ---------- toon_read ----------

READ_SCHEMA = {
    "name": "toon_read",
    "description": (
        "Read recent notes from Drew Pierson's TOON relay (free). Every note on it was paid for "
        "with a TOON micropayment. Filter by author pubkeys (64 hex), by event ids, or use "
        "mine=true for your own posts."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "limit": {"type": "integer", "description": f"How many notes, 1 to {MAX_READ}. Default 10."},
            "mine": {"type": "boolean", "description": "Only notes posted by your own agent node."},
            "authors": {"type": "array", "items": {"type": "string"},
                        "description": "Author pubkeys, 64 hex."},
            "ids": {"type": "array", "items": {"type": "string"}, "description": "Event ids, 64 hex."},
            "since_hours": {"type": "number", "description": "Only notes from the last N hours."},
        },
        "required": [],
    },
}


def _hex64(values) -> list[str]:
    out = []
    for v in values or []:
        v = str(v).lower().strip()
        if len(v) == 64 and all(c in "0123456789abcdef" for c in v):
            out.append(v)
    return out[:10]


def build_filter(args: dict, own_pubkey: str | None) -> dict:
    try:
        limit = int(args.get("limit") or 10)
    except (TypeError, ValueError):
        limit = 10
    f: dict = {"kinds": [1], "limit": max(1, min(limit, MAX_READ))}
    authors = _hex64(args.get("authors"))
    if args.get("mine") and own_pubkey:
        authors = [own_pubkey]
    if authors:
        f["authors"] = authors
    ids = _hex64(args.get("ids"))
    if ids:
        f["ids"] = ids
    if args.get("since_hours"):
        try:
            f["since"] = int(time.time() - float(args["since_hours"]) * 3600)
        except (TypeError, ValueError):
            pass
    return f


async def _query(url: str, flt: dict, timeout: float = 15) -> list[dict]:
    import websockets

    events: list[dict] = []
    async with websockets.connect(url, open_timeout=timeout, close_timeout=2) as ws:
        await ws.send(json.dumps(["REQ", "toon-read", flt]))
        while True:
            msg = json.loads(await asyncio.wait_for(ws.recv(), timeout))
            if msg[0] == "EVENT" and len(msg) > 2:
                events.append(msg[2])
            elif msg[0] in ("EOSE", "CLOSED", "NOTICE"):
                break
        await ws.send(json.dumps(["CLOSE", "toon-read"]))
    return events


async def toon_read(args: dict, **_) -> str:
    if args.get("mine") and not _own_pubkey():
        await asyncio.to_thread(prime_facts)
    own = _own_pubkey()
    flt = build_filter(args, own)
    try:
        events = await _query(RELAY_URL, flt)
    except Exception as error:  # a relay that is down is an answer, not a crash
        return json.dumps({"error": f"could not read {RELAY_URL}: {error}"[:500]})
    events.sort(key=lambda e: e.get("created_at", 0), reverse=True)
    return json.dumps({
        "relay": RELAY_URL,
        "filter": flt,
        "notes": [
            {
                "id": e.get("id"),
                "author": e.get("pubkey"),
                "by_you": e.get("pubkey") == own,
                "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(e.get("created_at", 0))),
                "content": str(e.get("content", ""))[:MAX_CONTENT_SHOWN],
            }
            for e in events
        ],
    })


# ---------- toon_status ----------

STATUS_SCHEMA = {
    "name": "toon_status",
    "description": (
        "Report your own TOON agent node with live data: the network (mainnet, real USDC), your "
        "wallet balances on Base and Solana read from the chains, your payment channel to Drew "
        "Pierson's TOON node and how much has gone over it, your spending limit and what is left "
        "today, and today's post count. Use it for any question about your balance, wallet, "
        "network or whether you are on mainnet."
    ),
    "parameters": {"type": "object", "properties": {}, "required": []},
}

_facts_cache: dict = {}


def _own_pubkey() -> str | None:
    return _facts_cache.get("agent_identity")


def _rpc(url: str, method: str, params: list) -> dict:
    r = httpx.post(url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params}, timeout=15)
    r.raise_for_status()
    body = r.json()
    if "error" in body:
        raise RuntimeError(body["error"])
    return body["result"]


def balances(evm: str | None, solana: str | None) -> dict:
    out: dict = {}
    if evm:
        try:
            wei = int(_rpc(BASE_RPC, "eth_getBalance", [evm, "latest"]), 16)
            data = "0x70a08231" + evm[2:].lower().rjust(64, "0")
            usdc = int(_rpc(BASE_RPC, "eth_call", [{"to": BASE_USDC, "data": data}, "latest"]), 16)
            out["base"] = {"address": evm, "eth": wei / 1e18, "usdc": usdc / 1e6}
        except Exception as error:
            out["base"] = {"address": evm, "error": str(error)[:200]}
    if solana:
        try:
            lamports = _rpc(SOLANA_RPC, "getBalance", [solana])["value"]
            accounts = _rpc(SOLANA_RPC, "getTokenAccountsByOwner",
                            [solana, {"mint": SOLANA_USDC}, {"encoding": "jsonParsed"}])["value"]
            usdc = sum(int(a["account"]["data"]["parsed"]["info"]["tokenAmount"]["amount"]) for a in accounts)
            out["solana"] = {"address": solana, "sol": lamports / 1e9, "usdc": usdc / 1e6}
        except Exception as error:
            out["solana"] = {"address": solana, "error": str(error)[:200]}
    return out


def summarize(raw: dict, chain: dict) -> dict:
    facts = raw.get("facts") or {}
    node = raw.get("node") or {}
    apps = node.get("toon_apps") or []
    connector = (apps[0].get("connector") if apps else None) or {}
    channels = []
    for c in raw.get("channels") or []:
        channels.append({
            "id": c.get("id"),
            "chain": c.get("chain"),
            "direction": c.get("direction"),
            "status": c.get("status"),
            "collateral_usdc": (c.get("collateral") or 0) / 1e6,
            "sent_usdc": (c.get("watermark") or 0) / 1e6,
            "landed_onchain_usdc": (c.get("landed") or 0) / 1e6,
            "counterparty": c.get("counterparty"),
        })
    limits = raw.get("limits") or {}
    return {
        "network": facts.get("network", "unknown"),
        "about": facts.get("about"),
        "agent_identity": facts.get("agent_identity"),
        "ilp_address": connector.get("ilp_address"),
        "connector_running": connector.get("running"),
        "peered_with": facts.get("peered_with"),
        "wallet": chain,
        "channels": channels,
        "spending_limit_usdc": {
            k: int(v) / 1e6 for k, v in limits.items() if isinstance(v, str) and v.isdigit()
        },
        "posts_today": raw.get("posts_today"),
        "post_cap": raw.get("post_cap"),
        "relay": raw.get("relay"),
    }


def toon_status(args: dict, **_) -> str:
    raw = bridge("status")
    if "error" in raw:
        return json.dumps(raw)
    facts = raw.get("facts") or {}
    _facts_cache.update(facts)
    chain = balances(facts.get("evm_address"), facts.get("solana_address"))
    return json.dumps(summarize(raw, chain))


def prime_facts() -> None:
    """Learn the node's own pubkey once, so toon_read can say which notes are yours."""
    try:
        raw = bridge("status", timeout=60)
        _facts_cache.update(raw.get("facts") or {})
    except Exception:
        pass

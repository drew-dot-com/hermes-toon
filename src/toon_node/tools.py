"""Three tools over the agent's TOON payer: post a note, read a relay, report the payer.

Everything paid goes through one local ``charter payer`` service (``TOON_PAYER_URL``, the same
variable the ``web-toon`` provider uses), the process that owns the machine's one payment channel
(github.com/drew-dot-com/charter, ``charter payer``). This plugin never holds a key: ``toon_post``
asks the service to sign and pay a note onto the relay, ``toon_read`` asks it for a free relay
read (the service's WebSocket follows the machine's proxy, which matters inside a Charter home),
and ``toon_status`` reports the service's channel plus the payer wallet's balances read from the
chain. Until 0.1.0 these tools bridged into a ``toon`` node in a Lima VM; that path is gone.
"""

from __future__ import annotations

import fcntl
import json
import os
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx

DEFAULT_RELAY = "wss://relay.mainnet.toonprotocol.dev"
BASE_RPC = os.environ.get("TOON_BASE_RPC", "https://mainnet.base.org")
BASE_USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"

MAX_READ = 50
MAX_CONTENT_SHOWN = 500
MAX_POST_CHARS = 1000
DEFAULT_POST_CAP = 50
# The service pays and waits for the relay's answer; a first call may also open a channel on chain.
POST_TIMEOUT_S = 90.0
READ_TIMEOUT_S = 30.0
HEALTH_TIMEOUT_S = 15.0

SETTINGS = ("TOON_PAYER_URL", "TOON_RELAY_URL", "TOON_POST_CAP", "TOON_STATE_DIR")


def _setting(name: str, default: str = "") -> str:
    """One operator setting, through Hermes's profile-scoped secret reader when it is importable."""
    try:
        from agent.secret_scope import get_secret_str
    except ImportError:
        return os.environ.get(name, default).strip()
    return (get_secret_str(name, default) or "").strip()


def payer_url() -> str:
    return _setting("TOON_PAYER_URL").rstrip("/")


def relay_url() -> str:
    return _setting("TOON_RELAY_URL") or DEFAULT_RELAY


def post_cap() -> int:
    raw = _setting("TOON_POST_CAP")
    return int(raw) if raw.isdigit() else DEFAULT_POST_CAP


def state_dir() -> Path:
    return Path(_setting("TOON_STATE_DIR") or "~/.toon-node").expanduser()


def available() -> bool:
    """The tools are offered exactly when a payer service URL is set."""
    return bool(payer_url())


def _error(message: str) -> str:
    return json.dumps({"error": message[:500]})


def _reason(payload: dict, status: int) -> str:
    code = payload.get("code") or f"HTTP {status}"
    error = payload.get("error") or payload.get("message") or ""
    return f"{code}: {error}".strip(": ")


def _get(path: str, params: dict | None = None, timeout: float = HEALTH_TIMEOUT_S) -> tuple[int, dict]:
    base = payer_url()
    try:
        r = httpx.get(f"{base}{path}", params=params, timeout=timeout, trust_env=False)
    except httpx.RequestError as exc:
        raise RuntimeError(f"TOON payer service unreachable at {base}: {type(exc).__name__}") from exc
    try:
        return r.status_code, r.json()
    except ValueError as exc:
        raise RuntimeError(f"TOON payer service answered non-JSON (HTTP {r.status_code})") from exc


def _post(path: str, body: dict, timeout: float) -> tuple[int, dict]:
    base = payer_url()
    try:
        r = httpx.post(f"{base}{path}", json=body, timeout=timeout, trust_env=False)
    except httpx.RequestError as exc:
        raise RuntimeError(f"TOON payer service unreachable at {base}: {type(exc).__name__}") from exc
    try:
        return r.status_code, r.json()
    except ValueError as exc:
        raise RuntimeError(f"TOON payer service answered non-JSON (HTTP {r.status_code})") from exc


# ---------- the day's post count, taken under a lock before the service is asked ----------


def count_post(directory: Path, cap: int) -> str | None:
    """Count one post against today's cap, or say why not.

    A counter that cannot be read counts as the cap reached.
    """
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    today = datetime.now(UTC).strftime("%Y-%m-%d")
    counter = directory / f"posts-{today}"
    with open(directory / "post.lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            n = int(counter.read_text() or "0") if counter.exists() else 0
        except (OSError, ValueError):
            return "today's post counter is unreadable; refusing to post"
        if n >= cap:
            return f"today's cap of {cap} posts is reached"
        counter.write_text(f"{n + 1}\n")
    return None


def posts_today(directory: Path) -> int:
    counter = directory / f"posts-{datetime.now(UTC).strftime('%Y-%m-%d')}"
    try:
        return int(counter.read_text() or "0") if counter.exists() else 0
    except (OSError, ValueError):
        return 0


# ---------- toon_post ----------

POST_SCHEMA = {
    "name": "toon_post",
    "description": (
        "Publish a short public note (Nostr kind 1) to Drew Pierson's TOON relay, paid from your own "
        "TOON payment channel through the local payer service. Each post is a real payment, the relay's "
        "price (0.001 USDC on the default relay), from the same channel that pays your rent and fetches. "
        "Capped at the operator's daily post cap (default 50). Use it only when a user asks you to post "
        "something to TOON; never post private data."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "content": {"type": "string", "description": f"The note, 1 to {MAX_POST_CHARS} characters."},
        },
        "required": ["content"],
    },
}


def toon_post(args: dict, **_) -> str:
    content = str(args.get("content", "")).strip()
    if not content:
        return _error("content is empty")
    if len(content) > MAX_POST_CHARS:
        return _error(f"content is over {MAX_POST_CHARS} characters")
    if not payer_url():
        return _error("TOON_PAYER_URL is not set: run a charter payer service and point it there")
    cap = post_cap()
    refused = count_post(state_dir(), cap)
    if refused:
        return _error(refused)
    try:
        body = {"content": content, "tags": [["client", "toon-node"]]}
        status, out = _post("/relay/post", body, POST_TIMEOUT_S)
    except RuntimeError as exc:
        return _error(str(exc))
    if status != 200 or not out.get("ok"):
        paid = out.get("paid")
        detail = _reason(out, status)
        if out.get("relay_status") is not None:
            detail = f"relay answered HTTP {out['relay_status']}: {json.dumps(out.get('relay_answer'))[:200]}"
        refused = {"error": f"post refused: {detail}"[:500]}
        if str(paid).isdigit():
            refused["paid_base_units"] = int(paid)
        return json.dumps(refused)
    event = out.get("event") or {}
    paid = int(out.get("paid") or 0)
    return json.dumps({
        "outcome": "stored",
        "event_id": event.get("id"),
        "author": event.get("pubkey"),
        "paid_base_units": paid,
        "paid_usdc": paid / 1e6,
        "relay": out.get("relay") or relay_url(),
        "route": out.get("route"),
        "relay_answer": out.get("relay_answer"),
        "posts_today": posts_today(state_dir()),
        "post_cap": cap,
    })


# ---------- toon_read ----------

READ_SCHEMA = {
    "name": "toon_read",
    "description": (
        "Read recent events from Drew Pierson's TOON relay (free). This is the relay at "
        "wss://relay.mainnet.toonprotocol.dev (same relay as wss://relay.167-233-221-236.sslip.io). "
        "Every event on it was paid for with a TOON micropayment. By default it returns EVERY kind: "
        "notes (kind 1), git repositories pushed with rig (kind 30617 announcement, 30618 refs/state, "
        "1617 patch, 1621 issue), Lading storage records (30320) and anything else. Pass kinds to "
        "narrow it, e.g. [1] for notes only. Also filter by author pubkeys (64 hex), event ids, "
        "since_hours, or mine=true for your own posts. When asked what landed on the relay, read "
        "all kinds and report each kind_name."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "limit": {"type": "integer", "description": f"How many events, 1 to {MAX_READ}. Default 10."},
            "kinds": {"type": "array", "items": {"type": "integer"},
                      "description": "Event kinds to include. Omit for every kind."},
            "mine": {"type": "boolean", "description": "Only events you posted yourself."},
            "authors": {"type": "array", "items": {"type": "string"},
                        "description": "Author pubkeys, 64 hex."},
            "ids": {"type": "array", "items": {"type": "string"}, "description": "Event ids, 64 hex."},
            "since_hours": {"type": "number", "description": "Only events from the last N hours."},
        },
        "required": [],
    },
}

KIND_NAMES = {
    0: "profile",
    1: "note",
    1617: "git patch",
    1621: "git issue",
    1630: "git status (open)",
    1631: "git status (applied/merged)",
    1632: "git status (closed)",
    30320: "Lading storage record",
    30617: "git repository announcement",
    30618: "git repository state (refs)",
}

# Tags worth showing the model; the rest (p, e, signatures of other events) is noise.
SHOWN_TAGS = {"d", "name", "description", "subject", "t", "r", "x", "size", "leg", "clone", "web", "HEAD"}
MAX_TAGS_SHOWN = 10
MAX_KINDS = 20


def _hex64(values) -> list[str]:
    out = []
    for v in values or []:
        v = str(v).lower().strip()
        if len(v) == 64 and all(c in "0123456789abcdef" for c in v):
            out.append(v)
    return out[:10]


def _kinds(values) -> list[int]:
    out = []
    for v in values or []:
        if isinstance(v, bool):
            continue
        if isinstance(v, float) and not v.is_integer():
            continue
        try:
            k = int(v)
        except (TypeError, ValueError):
            continue
        if 0 <= k <= 65535 and k not in out:
            out.append(k)
    return out[:MAX_KINDS]


def summarize_event(e: dict, own: str | None) -> dict:
    kind = e.get("kind")
    tags = [
        [str(x)[:120] for x in t[:3]]
        for t in e.get("tags") or []
        if t and (t[0] in SHOWN_TAGS or str(t[0]).startswith("refs/"))
    ][:MAX_TAGS_SHOWN]
    return {
        "id": e.get("id"),
        "kind": kind,
        "kind_name": KIND_NAMES.get(kind, f"kind {kind}"),
        "author": e.get("pubkey"),
        "by_you": e.get("pubkey") == own,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(e.get("created_at", 0))),
        "tags": tags,
        "content": str(e.get("content", ""))[:MAX_CONTENT_SHOWN],
    }


def build_filter(args: dict, own_pubkey: str | None) -> dict:
    try:
        limit = int(args.get("limit") or 10)
    except (TypeError, ValueError):
        limit = 10
    f: dict = {"limit": max(1, min(limit, MAX_READ))}
    kinds = _kinds(args.get("kinds"))
    if kinds:
        f["kinds"] = kinds
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


_facts_cache: dict = {}


def own_pubkey() -> str | None:
    """The payer service's Nostr pubkey, the author of every toon_post; learned once from /health."""
    if "nostr_pubkey" not in _facts_cache:
        try:
            status, h = _get("/health")
            if status == 200 and isinstance(h.get("nostr_pubkey"), str):
                _facts_cache["nostr_pubkey"] = h["nostr_pubkey"]
        except RuntimeError:
            return None
    return _facts_cache.get("nostr_pubkey")


def read_relay(flt: dict) -> list[dict]:
    """A free relay read through the payer service, whose WebSocket follows the machine's proxy."""
    params = {"filter": json.dumps(flt), "relay": relay_url()}
    status, out = _get("/relay/read", params, timeout=READ_TIMEOUT_S)
    if status != 200 or not out.get("ok"):
        raise RuntimeError(_reason(out, status))
    return [e for e in out.get("events") or [] if isinstance(e, dict)]


async def toon_read(args: dict, **_) -> str:
    import asyncio

    if not payer_url():
        return _error("TOON_PAYER_URL is not set: run a charter payer service and point it there")
    own = await asyncio.to_thread(own_pubkey) if args.get("mine") else _facts_cache.get("nostr_pubkey")
    flt = build_filter(args, own)
    relay = relay_url()
    try:
        events = await asyncio.to_thread(read_relay, flt)
    except RuntimeError as error:  # a relay that is down is an answer, not a crash
        return json.dumps({"error": f"could not read {relay}: {error}"[:500]})
    events.sort(key=lambda e: e.get("created_at", 0), reverse=True)
    return json.dumps({
        "relay": relay,
        "filter": flt,
        "events": [summarize_event(e, own) for e in events],
    })


# ---------- toon_status ----------

STATUS_SCHEMA = {
    "name": "toon_status",
    "description": (
        "Report your own TOON payer with live data: the network (mainnet, real USDC), the payer "
        "wallet's balances on Base read from the chain, your payment channel to Drew Pierson's edge "
        "connector (its deposit, what has been signed over it, what is left), today's spend against "
        "the daily cap for fetches and posts, and today's post count. Use it for any question about "
        "your balance, wallet, channel, network or whether you are on mainnet."
    ),
    "parameters": {"type": "object", "properties": {}, "required": []},
}


def _rpc(url: str, method: str, params: list) -> dict:
    r = httpx.post(url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params}, timeout=15)
    r.raise_for_status()
    body = r.json()
    if "error" in body:
        raise RuntimeError(body["error"])
    return body["result"]


def balances(evm: str | None) -> dict:
    """The payer wallet on Base: ETH and USDC, read from the chain through the machine's proxy."""
    out: dict = {}
    if evm:
        try:
            wei = int(_rpc(BASE_RPC, "eth_getBalance", [evm, "latest"]), 16)
            data = "0x70a08231" + evm[2:].lower().rjust(64, "0")
            usdc = int(_rpc(BASE_RPC, "eth_call", [{"to": BASE_USDC, "data": data}, "latest"]), 16)
            out["base"] = {"address": evm, "eth": wei / 1e18, "usdc": usdc / 1e6}
        except Exception as error:
            out["base"] = {"address": evm, "error": str(error)[:200]}
    return out


def _usdc(units) -> float | None:
    try:
        return int(units) / 1e6
    except (TypeError, ValueError):
        return None


def summarize(health: dict, chain: dict, posts: int, cap: int) -> dict:
    """The /health answer as the agent should read it: USDC amounts, one channel line, the cap."""
    channel = health.get("channel") or {}
    budget = health.get("budget") or {}
    channels = []
    for c in channel.get("channels") or []:
        deposit, signed = _usdc(c.get("depositTotal")), _usdc(c.get("signed"))
        channels.append({
            "id": c.get("id"),
            "chain": channel.get("chain"),
            "deposit_usdc": deposit,
            "signed_usdc": signed,
            "left_usdc": (deposit - signed) if deposit is not None and signed is not None else None,
        })
    return {
        "network": "mainnet (real USDC on Base)",
        "payer_service": {"version": health.get("version"), "uptime_s": health.get("uptime_s")},
        "connector": health.get("connector"),
        "routes": health.get("routes"),
        "relay": health.get("relay") or relay_url(),
        "nostr_pubkey": health.get("nostr_pubkey"),
        "wallet": chain,
        "channels": channels,
        "channel_headroom_usdc": _usdc(channel.get("headroom")),
        "daily_cap_usdc": {
            "day": budget.get("day"),
            "spent": _usdc(budget.get("spent")),
            "cap": _usdc(budget.get("cap")),
            "remaining": _usdc(budget.get("remaining")),
            "covers": "paid fetches and posts; the rent is paid apart from this cap",
        },
        "posts_today": posts,
        "post_cap": cap,
    }


def toon_status(args: dict, **_) -> str:
    if not payer_url():
        return _error("TOON_PAYER_URL is not set: run a charter payer service and point it there")
    try:
        status, health = _get("/health")
    except RuntimeError as exc:
        return _error(str(exc))
    if status != 200 or not health.get("ok"):
        return _error(f"payer service unhealthy: {_reason(health, status)}")
    if isinstance(health.get("nostr_pubkey"), str):
        _facts_cache["nostr_pubkey"] = health["nostr_pubkey"]
    payer = (health.get("channel") or {}).get("payer")
    chain = balances(payer if isinstance(payer, str) else None)
    return json.dumps(summarize(health, chain, posts_today(state_dir()), post_cap()))

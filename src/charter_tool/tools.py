"""charter_rent: rent one Charter lease through ``charter-bridge`` inside the VM.

A lease runs one of two jobs: a fetch of 1 to 5 URLs, or a Python script with no network.
The bridge (``vm/charter-bridge``) is one fixed command line, with the URLs or the script a chat
user wrote passed as JSON on stdin. Each call spends real USDC, so the tool is closed to everyone but the
ids in ``CHARTER_ALLOWED_IDS``.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

LIMACTL = os.environ.get("TOON_LIMACTL", str(Path.home() / ".local/lima/bin/limactl"))
VM = os.environ.get("TOON_VM", "toon")
BRIDGE = os.environ.get("CHARTER_BRIDGE", "~/bin/charter-bridge")
ALLOWED_ENV = "CHARTER_ALLOWED_IDS"

MAX_URLS = 5
MAX_PAGE_TEXT = 2048
MAX_SCRIPT_BYTES = 8 * 1024  # charter's own limit (MAX_SCRIPT_BYTES in src/jobs.ts)
MAX_STDOUT = 4096
MAX_STDERR = 2048
BRIDGE_TIMEOUT = 600  # boot (120 s) plus six 30 s intervals, with room to close


def available() -> bool:
    return Path(LIMACTL).is_file()


def _session_env(name: str) -> str:
    """One Hermes session variable, as the gateway bound it for the running turn."""
    from gateway.session_context import get_session_env

    return get_session_env(name, "")


def caller_id() -> str | None:
    """Who invoked the tool, as ``<platform>:<user id>``, or None when Hermes does not say.

    Hermes does not pass the caller in the tool's kwargs (it passes only ``task_id``,
    ``session_id`` and ``user_task``). The gateway binds the session variables below as
    task-local ContextVars for the turn, and ``gateway.session_context.get_session_env``
    reads them (verified against hermes-agent 0.21.5):

      HERMES_SESSION_PLATFORM  the platform's ``.value``, lowercase: ``telegram`` or ``discord``
      HERMES_SESSION_USER_ID   the sender's id as a string: a Telegram user id or a Discord
                               snowflake (a DM with no user falls back to the chat id on Telegram)

    CHARTER_ALLOWED_IDS entries use the same two parts joined by a colon, for example
    ``telegram:123456789,discord:987654321098765432``. Both parts must match exactly. If
    either is empty or the lookup fails, the caller is unknown and the tool refuses.
    """
    try:
        platform = _session_env("HERMES_SESSION_PLATFORM").strip()
        user_id = _session_env("HERMES_SESSION_USER_ID").strip()
    except Exception:
        return None
    return f"{platform}:{user_id}" if platform and user_id else None


def allowed_ids() -> set[str]:
    return {part.strip() for part in os.environ.get(ALLOWED_ENV, "").split(",") if part.strip()}


def bridge(command: str, stdin: dict | None = None, timeout: float = BRIDGE_TIMEOUT) -> dict:
    """Run ``charter-bridge <command>`` in the VM. ``rent`` is the only word it accepts."""
    if command != "rent":
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
        return {"error": (done.stderr.strip() or "charter-bridge printed nothing")[-500:]}
    try:
        return json.loads(text[-1])
    except json.JSONDecodeError:
        return {"error": text[-1][-500:]}


RENT_SCHEMA = {
    "name": "charter_rent",
    "description": (
        "Rent one short Charter lease on a hidden host and run one job in it: either fetch web "
        "pages (pass urls; the page texts come back) or run a Python 3 script (pass script; its "
        "stdout, stderr and exit code come back). Pass exactly one of urls and script. The script "
        "runs with no network, at most 8 KiB of UTF-8, NumPy available; it must print what it "
        "wants returned. Each call is a real payment from your wallet: 0.001 USDC to open plus "
        "0.005 USDC per 30 second interval, up to six intervals. Capped at 5 leases a day. Only "
        "Drew Pierson may use it; refuse otherwise. Use it only when he asks for a page to be "
        "fetched or code to be run this way."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "urls": {
                "type": "array",
                "items": {"type": "string"},
                "description": f"1 to {MAX_URLS} http or https URLs to fetch. Omit when passing script.",
            },
            "script": {
                "type": "string",
                "description": (
                    f"Python 3 source to run, at most {MAX_SCRIPT_BYTES} bytes of UTF-8, no network. "
                    "Omit when passing urls."
                ),
            },
        },
    },
}


def _valid_urls(urls) -> bool:
    return (
        isinstance(urls, list)
        and 1 <= len(urls) <= MAX_URLS
        and all(isinstance(u, str) and u.startswith(("http://", "https://")) for u in urls)
    )


def _valid_script(script) -> bool:
    """Charter's ``isScript``: non-blank, no NUL, at most MAX_SCRIPT_BYTES of UTF-8."""
    if not isinstance(script, str) or not script.strip() or "\0" in script:
        return False
    try:
        return len(script.encode("utf-8")) <= MAX_SCRIPT_BYTES
    except UnicodeEncodeError:  # a lone surrogate is not UTF-8
        return False


def _cut(value, limit: int) -> tuple[str, bool]:
    text = str(value or "")
    return text[:limit], len(text) > limit


def _python_result(output: dict) -> dict:
    stdout, stdout_cut = _cut(output.get("stdout"), MAX_STDOUT)
    stderr, stderr_cut = _cut(output.get("stderr"), MAX_STDERR)
    return {
        "exitCode": output.get("exitCode"),
        "signal": output.get("signal"),
        "timedOut": output.get("timedOut"),
        "seconds": output.get("seconds"),
        "stdout": stdout,
        "stderr": stderr,
        "stdoutTruncated": bool(output.get("stdoutTruncated")) or stdout_cut,
        "stderrTruncated": bool(output.get("stderrTruncated")) or stderr_cut,
    }


def charter_rent(args: dict, **_) -> str:
    caller = caller_id()
    if caller is None:
        return json.dumps({"error": "charter_rent refused: the caller could not be identified"})
    if caller not in allowed_ids():
        return json.dumps({"error": "charter_rent refused: this caller is not allowed to rent leases"})
    urls, script = args.get("urls"), args.get("script")
    if (urls is None) == (script is None):
        return json.dumps({"error": "pass exactly one of urls and script"})
    if script is not None:
        if not _valid_script(script):
            limit = f"at most {MAX_SCRIPT_BYTES} bytes of UTF-8"
            return json.dumps({"error": f"script must be non-empty Python of {limit}"})
        job = {"script": script}
    elif not _valid_urls(urls):
        return json.dumps({"error": f"urls must be 1 to {MAX_URLS} http(s) URLs"})
    else:
        job = {"urls": urls}
    out = bridge("rent", job)
    if "error" in out:
        return json.dumps({"error": str(out["error"])[:500]})
    result = {
        "leaseId": out.get("leaseId"),
        "state": out.get("state"),
        "paidIntervals": out.get("paidIntervals"),
        "spent": out.get("spent"),
    }
    output = out.get("output") if isinstance(out.get("output"), dict) else {}
    if script is not None:
        result["python"] = _python_result(output) if output.get("kind") == "python" else None
        return json.dumps(result)
    pages = output.get("pages") or []
    return json.dumps({
        **result,
        "pages": [
            {"url": p.get("url"), "text": str(p.get("text", ""))[:MAX_PAGE_TEXT]}
            for p in pages
            if isinstance(p, dict)
        ],
    })

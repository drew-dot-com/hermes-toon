"""charter_rent: rent one Charter lease and run one job in it.

A lease runs one of two jobs: a fetch of 1 to 5 URLs, or a Python script with no network.
By default the plugin runs the ``charter`` CLI on this machine (github.com/drew-dot-com/charter,
next to a ``toon`` node whose wallet pays). An operator whose node lives elsewhere, a Mac with
the node in a Lima VM for one, sets ``CHARTER_BRIDGE_CMD`` instead, and the job goes to that
command as JSON on stdin (see ``vm/charter-bridge``).

Every call spends real USDC from the operator's node wallet, so the plugin bounds a call
(``CHARTER_MAX_INTERVALS``), bounds a day (``CHARTER_DAILY_LEASES``, counted here before the
lease starts) and, when the operator asks, bounds who may call it (``CHARTER_ALLOWED_IDS``).
Settings are read when the tool runs, through Hermes's secret scope when it is importable.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import shlex
import shutil
import subprocess
import tempfile
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

# ---------- what one lease costs, as published for the Charter mainnet host (hourly since 2026-11-04) ----------

OPEN_UNITS = 1000  # 0.001 USDC to open a lease
TICK_UNITS = 7000  # 0.007 USDC per interval
INTERVAL_SECONDS = 3600
INTERVAL_TEXT = "one hour"  # how the interval reads in a price line
USDC_DECIMALS = 6

# ---------- limits ----------

MAX_URLS = 5
MAX_URL_CHARS = 2048
MAX_PAGE_TEXT = 2048
MAX_SCRIPT_BYTES = 8 * 1024  # charter's own limit (MAX_SCRIPT_BYTES in src/jobs.ts)
MAX_STDOUT = 4096
MAX_STDERR = 2048
DEFAULT_MAX_INTERVALS = 1  # one hour covers any fetch or short script
HARD_MAX_INTERVALS = 24  # a day, 0.169 USDC; no setting raises it
DEFAULT_DAILY_LEASES = 5
DEFAULT_RELAY = "wss://relay.mainnet.toonprotocol.dev"
BOOT_SECONDS = 120  # a lease boots, ticks, then closes; the timeout follows the interval cap

URL = re.compile(r"https?://[\x21-\x7e]+")  # printable ASCII, no spaces; commas are checked apart
COUNT = re.compile(r"\d{1,6}\n?")

SETTINGS = (
    "CHARTER_BRIDGE_CMD", "CHARTER_BIN", "TOON_PASSPHRASE_FILE", "CHARTER_OFFER_ID", "CHARTER_RELAY_URL",
    "CHARTER_MAX_INTERVALS", "CHARTER_DAILY_LEASES", "CHARTER_ALLOWED_IDS", "CHARTER_STATE_DIR",
)


# ---------- settings ----------


def _setting(name: str, default: str = "") -> str:
    """One operator setting, through Hermes's profile-scoped secret reader when it is importable."""
    try:
        from agent.secret_scope import get_secret_str
    except ImportError:
        return os.environ.get(name, default).strip()
    return (get_secret_str(name, default) or "").strip()


class SettingError(ValueError):
    """A setting is missing or malformed; the message names the variable, never a value."""


def _int_setting(name: str, default: int, low: int, high: int) -> int:
    raw = _setting(name)
    if not raw:
        return default
    if not raw.isdigit() or not low <= int(raw) <= high:
        raise SettingError(f"{name} must be a whole number from {low} to {high}")
    return int(raw)


def settings() -> dict:
    """Every setting, read now. Raises SettingError for a malformed one."""
    return {
        "bridge_cmd": _setting("CHARTER_BRIDGE_CMD"),
        "bin": _setting("CHARTER_BIN") or "charter",
        "passphrase_file": _setting("TOON_PASSPHRASE_FILE"),
        "offer": _setting("CHARTER_OFFER_ID"),
        "relay": _setting("CHARTER_RELAY_URL") or DEFAULT_RELAY,
        "max_intervals": _int_setting("CHARTER_MAX_INTERVALS", DEFAULT_MAX_INTERVALS, 1, HARD_MAX_INTERVALS),
        "daily_leases": _int_setting("CHARTER_DAILY_LEASES", DEFAULT_DAILY_LEASES, 0, 999_999),
        "allowed_ids": {p.strip() for p in _setting("CHARTER_ALLOWED_IDS").split(",") if p.strip()},
        "state_dir": Path(_setting("CHARTER_STATE_DIR") or "~/.charter-tool").expanduser(),
    }


def charter_path(binary: str) -> str | None:
    """The charter binary: a path that is a file, or a name found on PATH."""
    if Path(binary).expanduser().is_file():
        return str(Path(binary).expanduser())
    return shutil.which(binary)


def available() -> bool:
    """The tool is offered when a bridge command is set or a charter binary can be found."""
    try:
        s = settings()
    except SettingError:
        return False
    return bool(s["bridge_cmd"]) or charter_path(s["bin"]) is not None


# ---------- who is calling ----------


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
    either is empty or the lookup fails, the caller is unknown.
    """
    try:
        platform = _session_env("HERMES_SESSION_PLATFORM").strip()
        user_id = _session_env("HERMES_SESSION_USER_ID").strip()
    except Exception:
        return None
    return f"{platform}:{user_id}" if platform and user_id else None


def refusal(allowed_ids: set[str]) -> str | None:
    """Why this caller may not rent, or None.

    An empty allowlist means everyone the operator already lets talk to the agent: Hermes's own
    platform allowlists are the gate. A non-empty one admits exactly its entries, and refuses a
    caller it cannot identify.
    """
    if not allowed_ids:
        return None
    caller = caller_id()
    if caller is None:
        return "charter_rent refused: CHARTER_ALLOWED_IDS is set and the caller could not be identified"
    if caller not in allowed_ids:
        return "charter_rent refused: this caller is not in CHARTER_ALLOWED_IDS"
    return None


# ---------- the day counter ----------


def count_lease(state_dir: Path, cap: int) -> str | None:
    """Take one of today's leases, or say why not. Counted and flushed before charter starts.

    The count is read under a lock and replaced whole (temp file, fsync, os.replace), so a crash
    leaves the old count or the new one. A missing counter is 0; one that cannot be read or holds
    anything but a count counts as the cap reached, so a damaged file never resets the cap.
    """
    if cap <= 0:
        return "charter_rent refused: CHARTER_DAILY_LEASES is 0, so no lease may be rented"
    state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    today = datetime.now(UTC).strftime("%Y-%m-%d")
    path = state_dir / f"leases-{today}"
    with open(state_dir / "lease.lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            text = path.read_text()
            count = int(text) if COUNT.fullmatch(text) else None
        except FileNotFoundError:
            count = 0
        except (OSError, ValueError):
            count = None
        if count is None:
            return f"today's lease count is unreadable, so the cap of {cap} leases a day counts as reached"
        if count >= cap:
            return f"today's cap of {cap} leases is reached"
        tmp = state_dir / f".leases-{today}.tmp"
        with open(tmp, "w") as out:
            out.write(f"{count + 1}\n")
            out.flush()
            os.fsync(out.fileno())
        os.replace(tmp, path)
    return None


# ---------- running the job ----------


def run_seconds(max_intervals: int) -> int:
    return max(300, BOOT_SECONDS + INTERVAL_SECONDS * max_intervals + 60)


def _last_json_line(done: subprocess.CompletedProcess, who: str) -> dict:
    lines = done.stdout.strip().splitlines()
    try:
        result = json.loads(lines[-1]) if lines else None
    except ValueError:
        result = None
    if not isinstance(result, dict):
        tail = done.stderr.strip().splitlines()[-1:] or [f"{who} printed nothing"]
        return {"error": f"{who} failed: {tail[0][-300:]}"}
    return result


def run_bridge(s: dict, job: dict) -> dict:
    """Hand the job to ``CHARTER_BRIDGE_CMD`` as JSON on stdin; the command line is the operator's."""
    argv = shlex.split(s["bridge_cmd"])
    if not argv:
        return {"error": "CHARTER_BRIDGE_CMD is empty"}
    try:
        done = subprocess.run(argv, input=json.dumps(job), capture_output=True, text=True,
                              timeout=max(600, run_seconds(s["max_intervals"])))
    except (OSError, subprocess.TimeoutExpired) as error:
        return {"error": f"the bridge did not finish: {type(error).__name__}"}
    return _last_json_line(done, "the bridge")


def _write_script(state_dir: Path, script: str) -> str:
    """The script in a fresh 0600 file under the state dir, which only this user can read."""
    state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, path = tempfile.mkstemp(prefix="script-", suffix=".py", dir=state_dir)
    with os.fdopen(fd, "w", encoding="utf-8") as out:
        out.write(script)
    return path


def run_charter(s: dict, job: dict) -> dict:
    """Run ``charter rent`` here with a fixed command line and a minimal environment."""
    not_ready = _direct_ready(s)
    if not_ready:
        return {"error": not_ready}
    binary = charter_path(s["bin"])
    # Only what charter and the toon binary it runs need: nothing else in this process reaches them.
    env = {"PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"), "HOME": str(Path.home()),
           "TOON_PASSPHRASE_FILE": s["passphrase_file"]}
    script = _write_script(s["state_dir"], job["script"]) if "script" in job else None
    job_args = ["--python", script] if script else ["--urls", ",".join(job["urls"])]
    argv = [binary, "rent", "--offer", s["offer"], *job_args, "--max-intervals", str(s["max_intervals"]),
            "--relay", s["relay"], "--json"]
    try:
        done = subprocess.run(argv, env=env, capture_output=True, text=True,
                              timeout=run_seconds(s["max_intervals"]))
    except (OSError, subprocess.TimeoutExpired) as error:
        return {"error": f"charter did not finish: {type(error).__name__}"}
    finally:
        if script:
            os.unlink(script)
    return _last_json_line(done, "charter")


def run_job(s: dict, job: dict) -> dict:
    return run_bridge(s, job) if s["bridge_cmd"] else run_charter(s, job)


# ---------- the tool ----------


def usdc(units) -> str | None:
    """Base units as a USDC amount, e.g. ``"31000"`` -> ``"0.031"``; None when unreadable."""
    try:
        return f"{Decimal(int(units)) / (10 ** USDC_DECIMALS):f}"
    except (TypeError, ValueError, ArithmeticError):
        return None


def price_line(max_intervals: int, daily_leases: int) -> str:
    most = usdc(OPEN_UNITS + TICK_UNITS * max_intervals)
    plural = "" if max_intervals == 1 else "s"
    return (f"{usdc(OPEN_UNITS)} USDC to open plus {usdc(TICK_UNITS)} USDC per {INTERVAL_TEXT} "
            f"interval, at most {max_intervals} interval{plural} ({most} USDC) per call; "
            f"{daily_leases} leases a day")


RENT_SCHEMA = {
    "name": "charter_rent",
    "description": (
        "Rent one short lease on a Charter compute host and run one job in it: either fetch web pages "
        "(pass urls; the page texts come back) or run a Python 3 script (pass script; its stdout, "
        "stderr and exit code come back). Pass exactly one of urls and script. The script runs with "
        "no network, at most 8 KiB of UTF-8, NumPy available; it must print what it wants returned. "
        f"Each call is a real payment from this node's wallet: {usdc(OPEN_UNITS)} USDC to open plus "
        f"{usdc(TICK_UNITS)} USDC per {INTERVAL_TEXT} interval, up to the operator's interval "
        f"cap (default {DEFAULT_MAX_INTERVALS}, so {usdc(OPEN_UNITS + TICK_UNITS * DEFAULT_MAX_INTERVALS)} "
        f"USDC at most), and at most the operator's daily lease cap (default {DEFAULT_DAILY_LEASES}). "
        "Use it only when the user asks for a page to be fetched or code to be run on a rented machine, "
        "and tell the user what the call spent, from the spent and price fields of the result."
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
        and all(isinstance(u, str) and len(u) <= MAX_URL_CHARS and URL.fullmatch(u) and "," not in u
                for u in urls)
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


def _error(message: str) -> str:
    return json.dumps({"error": message[:500]})


def charter_rent(args: dict, **_) -> str:
    try:
        s = settings()
    except SettingError as error:
        return _error(str(error))
    refused = refusal(s["allowed_ids"])
    if refused:
        return _error(refused)
    urls, script = args.get("urls"), args.get("script")
    if (urls is None) == (script is None):
        return _error("pass exactly one of urls and script")
    if script is not None:
        if not _valid_script(script):
            return _error(f"script must be non-empty Python of at most {MAX_SCRIPT_BYTES} bytes of UTF-8")
        job = {"script": script}
    elif not _valid_urls(urls):
        return _error(f"urls must be 1 to {MAX_URLS} plain http(s) URLs without spaces or commas")
    else:
        job = {"urls": urls}
    if not s["bridge_cmd"]:  # a bridge keeps these settings itself; here they are checked before counting
        not_ready = _direct_ready(s)
        if not_ready:
            return _error(not_ready)
    counted = count_lease(s["state_dir"], s["daily_leases"])
    if counted:
        return _error(counted)
    out = run_job(s, job)
    if "error" in out:
        return _error(str(out["error"]))
    result = {
        "leaseId": out.get("leaseId"),
        "state": out.get("state"),
        "paidIntervals": out.get("paidIntervals"),
        "spent": out.get("spent"),
        "spentUsdc": usdc(out.get("spent")),
        "price": price_line(s["max_intervals"], s["daily_leases"]),
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


def _direct_ready(s: dict) -> str | None:
    """Why ``charter`` cannot run here yet, or None. Checked before a lease is counted."""
    if charter_path(s["bin"]) is None:
        return (f"the charter CLI was not found (CHARTER_BIN is {s['bin']!r}); install it from "
                "github.com/drew-dot-com/charter (npm ci, npm run build, npm install -g .) "
                "or set CHARTER_BRIDGE_CMD")
    needed = (("TOON_PASSPHRASE_FILE", "passphrase_file"), ("CHARTER_OFFER_ID", "offer"))
    missing = [name for name, key in needed if not s[key]]
    if missing:
        return f"{' and '.join(missing)} must be set to rent without a bridge"
    return None

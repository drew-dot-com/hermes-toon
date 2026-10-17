"""charter-tool: only Drew may rent, the tool answer is small, and the bridge caps leases a day."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml

from charter_tool import tools

ROOT = Path(__file__).resolve().parents[1]
BRIDGE = ROOT / "src/charter_tool/vm/charter-bridge"
URLS = ["https://example.com/a", "http://example.org/b"]


class Ctx:
    def __init__(self):
        self.tools = {}

    def register_tool(self, name, toolset, schema, handler, **kw):
        self.tools[name] = (toolset, schema, kw)


def as_session(monkeypatch, platform, user_id, allowed="telegram:111,discord:222"):
    env = {"HERMES_SESSION_PLATFORM": platform, "HERMES_SESSION_USER_ID": user_id}
    monkeypatch.setattr(tools, "_session_env", lambda name: env.get(name, ""))
    monkeypatch.setenv("CHARTER_ALLOWED_IDS", allowed)


def no_bridge(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("bridge was called")

    monkeypatch.setattr(tools, "bridge", boom)


# ---------- the tool ----------


def test_manifest_lists_exactly_the_registered_tools():
    import charter_tool

    ctx = Ctx()
    charter_tool.register(ctx)
    m = yaml.safe_load((ROOT / "src/charter_tool/plugin.yaml").read_text())
    assert m["name"] == "charter-tool"
    assert sorted(m["provides_tools"]) == sorted(ctx.tools) == ["charter_rent"]
    assert all(t[1]["name"] == n for n, t in ctx.tools.items())
    assert "Disclosure" in m["description"]
    assert "\u2014" not in (ROOT / "src/charter_tool/plugin.yaml").read_text()


def test_unknown_caller_is_refused_without_a_bridge_call(monkeypatch):
    as_session(monkeypatch, "telegram", "999")
    no_bridge(monkeypatch)
    out = json.loads(tools.charter_rent({"urls": URLS}))
    assert "not allowed" in out["error"]


def test_missing_caller_is_refused_without_a_bridge_call(monkeypatch):
    as_session(monkeypatch, "", "")
    no_bridge(monkeypatch)
    assert "error" in json.loads(tools.charter_rent({"urls": URLS}))
    as_session(monkeypatch, "telegram", "")
    assert "error" in json.loads(tools.charter_rent({"urls": URLS}))
    as_session(monkeypatch, "", "111")
    assert "error" in json.loads(tools.charter_rent({"urls": URLS}))


def test_empty_allow_list_refuses_everyone(monkeypatch):
    as_session(monkeypatch, "telegram", "111", allowed="")
    no_bridge(monkeypatch)
    assert "error" in json.loads(tools.charter_rent({"urls": URLS}))
    monkeypatch.delenv("CHARTER_ALLOWED_IDS")
    assert "error" in json.loads(tools.charter_rent({"urls": URLS}))


def test_platform_and_id_must_both_match_exactly(monkeypatch):
    no_bridge(monkeypatch)
    wrong = (("discord", "111"), ("telegram", "222"), ("Telegram", "111"), ("telegram", "1111"))
    for platform, user_id in wrong:
        as_session(monkeypatch, platform, user_id)
        assert "error" in json.loads(tools.charter_rent({"urls": URLS})), (platform, user_id)


def test_lookup_failure_inside_hermes_fails_closed(monkeypatch):
    def broken(name):
        raise RuntimeError("no gateway")

    monkeypatch.setattr(tools, "_session_env", broken)
    monkeypatch.setenv("CHARTER_ALLOWED_IDS", "telegram:111")
    no_bridge(monkeypatch)
    assert "error" in json.loads(tools.charter_rent({"urls": URLS}))


@pytest.mark.parametrize("platform,user_id", [("telegram", "111"), ("discord", "222")])
def test_allowed_caller_reaches_the_bridge_with_the_urls(monkeypatch, platform, user_id):
    as_session(monkeypatch, platform, user_id)
    seen = {}

    def fake(command, stdin=None, timeout=0):
        seen["command"] = command
        seen["stdin"] = stdin
        return {"leaseId": "L1", "state": "closed", "paidIntervals": 2, "spent": "11000",
                "output": {"pages": [{"url": URLS[0], "text": "hello", "jpegBase64": "AAAA"}]}}

    monkeypatch.setattr(tools, "bridge", fake)
    out = json.loads(tools.charter_rent({"urls": URLS}))
    assert (seen["command"], seen["stdin"]) == ("rent", {"urls": URLS})
    assert tools.BRIDGE_TIMEOUT >= 300  # a lease boots, ticks and closes
    assert out == {"leaseId": "L1", "state": "closed", "paidIntervals": 2, "spent": "11000",
                   "pages": [{"url": URLS[0], "text": "hello"}]}
    assert "jpegBase64" not in json.dumps(out)


def test_page_text_is_cut_to_2_kib(monkeypatch):
    as_session(monkeypatch, "telegram", "111")
    big = "x" * 5000
    monkeypatch.setattr(tools, "bridge", lambda *a, **k: {
        "leaseId": "L", "state": "closed", "paidIntervals": 1, "spent": "1",
        "output": {"pages": [{"url": "https://e.com", "text": big, "jpegBase64": "B" * 9000}]}})
    out = json.loads(tools.charter_rent({"urls": ["https://e.com"]}))
    assert len(out["pages"][0]["text"]) == 2048


def test_bridge_error_and_odd_output_are_passed_on_safely(monkeypatch):
    as_session(monkeypatch, "telegram", "111")
    monkeypatch.setattr(tools, "bridge", lambda *a, **k: {"error": "today's cap of 5 leases is reached"})
    assert "cap" in json.loads(tools.charter_rent({"urls": URLS}))["error"]
    monkeypatch.setattr(tools, "bridge", lambda *a, **k: {"leaseId": "L", "state": "open",
                                                           "paidIntervals": 0, "spent": "0"})
    assert json.loads(tools.charter_rent({"urls": URLS}))["pages"] == []


@pytest.mark.parametrize("urls", [[], "https://a.com", ["ftp://a.com"], ["https://a.com"] * 6, [1], None])
def test_bad_urls_are_rejected_before_the_bridge(monkeypatch, urls):
    as_session(monkeypatch, "telegram", "111")
    no_bridge(monkeypatch)
    assert "error" in json.loads(tools.charter_rent({"urls": urls}))


def test_bridge_wrapper_runs_only_rent():
    for bad in ("rent; id", "status", "offers"):
        with pytest.raises(ValueError):
            tools.bridge(bad)


# ---------- the bridge script ----------


@pytest.fixture
def vm(tmp_path):
    """A fake VM home: a bridge.env, a passphrase file, and a `charter` that records how it was run."""
    home = tmp_path / "home"
    (home / ".config/charter").mkdir(parents=True)
    (home / "secrets").mkdir()
    (home / "secrets/passphrase").write_text("do-not-print-me")
    (home / ".config/charter/bridge.env").write_text(
        f"TOON_PASSPHRASE_FILE={home}/secrets/passphrase\n"
        "CHARTER_OFFER_ID=" + "a" * 64 + ":gpu1\n"
        "CHARTER_RELAY_URL=wss://relay.example/\n"
    )
    fake = tmp_path / "charter"
    log = tmp_path / "charter.log"
    fake.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        f"open({str(log)!r}, 'a').write(json.dumps({{'argv': sys.argv[1:], "
        "'pass': os.environ.get('TOON_PASSPHRASE_FILE'), 'env': sorted(os.environ)}) + '\\n')\n"
        f"if os.path.exists({str(tmp_path / 'fail')!r}):\n"
        "    sys.stderr.write('boom\\n'); sys.exit(1)\n"
        "print(json.dumps({'leaseId': 'L1', 'state': 'closed', 'paidIntervals': 1, 'spent': '6000',\n"
        " 'output': {'pages': [{'url': 'https://example.com/a', 'text': 'hi', 'jpegBase64': 'AA'}]}}))\n"
    )
    fake.chmod(0o755)
    return {"home": home, "fake": fake, "log": log, "fail": tmp_path / "fail"}


def run_bridge(vm, *argv, stdin=None, extra=None):
    extra = dict(extra or {})
    # charter gets a minimal env, so the fake is told to fail by a file, not by a variable.
    if extra.pop("FAKE_FAIL", None):
        vm["fail"].touch()
    else:
        vm["fail"].unlink(missing_ok=True)
    env = {"PATH": os.environ["PATH"], "HOME": str(vm["home"]), "CHARTER_BIN": str(vm["fake"]), **extra}
    return subprocess.run([sys.executable, str(BRIDGE), *argv],
                          input=json.dumps(stdin if stdin is not None else {"urls": URLS}),
                          capture_output=True, text=True, env=env, timeout=30)


def calls(vm):
    return [json.loads(line) for line in vm["log"].read_text().splitlines()] if vm["log"].exists() else []


def test_bridge_runs_charter_with_the_fixed_command_line(vm):
    done = run_bridge(vm, "rent")
    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout)["leaseId"] == "L1"
    (call,) = calls(vm)
    assert call["argv"] == ["rent", "--offer", "a" * 64 + ":gpu1", "--urls", ",".join(URLS),
                            "--max-intervals", "6", "--relay", "wss://relay.example/", "--json"]
    assert call["pass"] == f"{vm['home']}/secrets/passphrase"


def test_bridge_refuses_the_sixth_lease_of_a_utc_day(vm):
    for _ in range(5):
        assert run_bridge(vm, "rent").returncode == 0
    sixth = run_bridge(vm, "rent")
    assert sixth.returncode != 0
    assert "cap of 5" in json.loads(sixth.stdout)["error"]
    assert len(calls(vm)) == 5
    today = datetime.now(UTC).strftime("%Y-%m-%d")
    assert (vm["home"] / f".charter-bridge/leases-{today}").read_text().strip() == "5"


def test_bridge_counts_the_lease_before_charter_runs(vm):
    for _ in range(5):
        run_bridge(vm, "rent", extra={"FAKE_FAIL": "1"})  # charter dies every time
    sixth = run_bridge(vm, "rent")
    assert "cap of 5" in json.loads(sixth.stdout)["error"]
    assert len(calls(vm)) == 5


def test_bridge_failure_is_one_json_error_line(vm):
    done = run_bridge(vm, "rent", extra={"FAKE_FAIL": "1"})
    assert done.returncode != 0
    assert "error" in json.loads(done.stdout.strip().splitlines()[-1])


@pytest.mark.parametrize("command", ["status", "offers", "rent; id", "", "--help"])
def test_bridge_has_only_the_rent_subcommand(vm, command):
    done = run_bridge(vm, *([command] if command else []))
    assert done.returncode != 0
    assert "usage" in json.loads(done.stdout)["error"]
    assert calls(vm) == []


@pytest.mark.parametrize("stdin", [
    {"urls": []}, {"urls": ["https://a.com"] * 6}, {"urls": ["file:///etc/passwd"]},
    {"urls": ["https://a.com/x,y"]}, {"urls": ["https://a.com/ x"]}, {"urls": ["--max-intervals"]},
    {"urls": "https://a.com"}, {"urls": [5]}, {}, [],
])
def test_bridge_rejects_bad_urls_without_spending_a_lease(vm, stdin):
    done = run_bridge(vm, "rent", stdin=stdin)
    assert done.returncode != 0
    assert "error" in json.loads(done.stdout)
    assert calls(vm) == []
    assert not list((vm["home"] / ".charter-bridge").glob("leases-*"))


def test_bridge_needs_its_env_file_and_never_prints_secrets(vm):
    done = run_bridge(vm, "rent")
    assert "do-not-print-me" not in done.stdout + done.stderr
    assert "secrets/passphrase" not in done.stdout
    (vm["home"] / ".config/charter/bridge.env").unlink()
    missing = run_bridge(vm, "rent")
    assert missing.returncode != 0
    assert "bridge.env" in json.loads(missing.stdout)["error"]
    assert "do-not-print-me" not in missing.stdout + missing.stderr


# ---------- final review F-I6: the day counter fails closed, the cap is fixed, charter gets a minimal env ----------


def counter(vm):
    return vm["home"] / f".charter-bridge/leases-{datetime.now(UTC).strftime('%Y-%m-%d')}"


@pytest.mark.parametrize("content", ["garbage\n", "", "-1\n", "3 4\n", "\x00\x01"])
def test_bridge_treats_a_corrupt_day_counter_as_the_cap_reached(vm, content):
    counter(vm).parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    counter(vm).write_text(content)
    done = run_bridge(vm, "rent")
    assert done.returncode == 3
    assert "cap" in json.loads(done.stdout)["error"]
    assert calls(vm) == []
    assert counter(vm).read_text() == content, "a counter it cannot read is left as it was"


def test_bridge_treats_an_unreadable_day_counter_as_the_cap_reached(vm):
    counter(vm).parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    counter(vm).write_text("1\n")
    counter(vm).chmod(0o000)
    try:
        done = run_bridge(vm, "rent")
    finally:
        counter(vm).chmod(0o600)
    assert done.returncode == 3
    assert "cap" in json.loads(done.stdout)["error"]
    assert calls(vm) == []


def test_bridge_writes_the_counter_whole_through_a_temp_file(vm):
    for n in (1, 2):
        assert run_bridge(vm, "rent").returncode == 0
        assert counter(vm).read_text() == f"{n}\n"
    left = sorted(p.name for p in counter(vm).parent.iterdir())
    assert left == ["lease.lock", counter(vm).name], left


def test_bridge_cap_is_a_constant_not_read_from_env(vm):
    for _ in range(5):
        assert run_bridge(vm, "rent", extra={"CHARTER_BRIDGE_MAX_LEASES": "100"}).returncode == 0
    sixth = run_bridge(vm, "rent", extra={"CHARTER_BRIDGE_MAX_LEASES": "100"})
    assert sixth.returncode == 3
    assert "cap of 5" in json.loads(sixth.stdout)["error"]


def test_bridge_hands_charter_a_minimal_env(vm):
    done = run_bridge(vm, "rent", extra={"SOME_SECRET_TOKEN": "x", "NODE_OPTIONS": "--require /tmp/evil.js"})
    assert done.returncode == 0, done.stderr
    (call,) = calls(vm)
    env = set(call["env"]) - {"__CF_USER_TEXT_ENCODING", "LC_CTYPE"}  # added by macOS / Python at exec
    assert env == {"PATH", "HOME", "TOON_PASSPHRASE_FILE"}, env
    assert call["pass"] == f"{vm['home']}/secrets/passphrase"

"""charter-tool: operator settings with safe defaults, the price in every answer, the bridge optional.

The tool half runs against a fake ``run_job`` or a fake ``charter`` binary; the bridge half runs
``vm/charter-bridge`` itself against a fake ``charter``.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import types
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml

from charter_tool import tools

ROOT = Path(__file__).resolve().parents[1]
BRIDGE = ROOT / "src/charter_tool/vm/charter-bridge"
URLS = ["https://example.com/a", "http://example.org/b"]
SCRIPT = "import numpy as np\nprint(np.pi)\n"
PY_OUT = {"kind": "python", "exitCode": 0, "signal": None, "timedOut": False, "seconds": 1.2,
          "stdout": "3.14159\n", "stderr": "", "stdoutTruncated": False, "stderrTruncated": False}
LEASE = {"leaseId": "L1", "state": "closed", "paidIntervals": 2, "spent": "11000",
         "output": {"pages": [{"url": URLS[0], "text": "hello", "jpegBase64": "AAAA"}]}}
PRICE_1_5 = ("0.001 USDC to open plus 0.007 USDC per one hour interval, at most 1 interval (0.008 USDC) "
             "per call; 5 leases a day")


class Ctx:
    def __init__(self):
        self.tools = {}

    def register_tool(self, name, toolset, schema, handler, **kw):
        self.tools[name] = (toolset, schema, kw)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch, tmp_path):
    """No charter setting leaks in from the developer's shell; the day counter lives in tmp_path."""
    for name in tools.SETTINGS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("CHARTER_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(tools, "_session_env", lambda name: "")
    return tmp_path


def as_session(monkeypatch, platform, user_id):
    env = {"HERMES_SESSION_PLATFORM": platform, "HERMES_SESSION_USER_ID": user_id}
    monkeypatch.setattr(tools, "_session_env", lambda name: env.get(name, ""))


def with_bridge(monkeypatch):
    """A bridge is configured, so direct-mode settings are not needed; the job itself is faked."""
    monkeypatch.setenv("CHARTER_BRIDGE_CMD", "fake-bridge rent")


def fake_job(monkeypatch, answer=None, seen=None):
    def run(s, job):
        if seen is not None:
            seen.append((s, job))
        return dict(answer if answer is not None else LEASE)

    monkeypatch.setattr(tools, "run_job", run)


def no_job(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("a lease was started")

    monkeypatch.setattr(tools, "run_job", boom)


def rent(args):
    return json.loads(tools.charter_rent(args))


def day_count(tmp_path):
    return tmp_path / "state" / f"leases-{datetime.now(UTC).strftime('%Y-%m-%d')}"


# ---------- the manifest and the schema ----------


def test_manifest_lists_exactly_the_registered_tools_and_the_version():
    import charter_tool

    ctx = Ctx()
    charter_tool.register(ctx)
    text = (ROOT / "src/charter_tool/plugin.yaml").read_text()
    m = yaml.safe_load(text)
    assert m["name"] == "charter-tool"
    assert m["version"] == charter_tool.__version__ == "0.3.0"
    assert sorted(m["provides_tools"]) == sorted(ctx.tools) == ["charter_rent"]
    assert all(t[1]["name"] == n for n, t in ctx.tools.items())
    assert ctx.tools["charter_rent"][2]["check_fn"] is tools.available
    assert "Disclosure" in m["description"]
    assert "\u2014" not in text
    assert sorted(m["optional_env"]) == sorted(tools.SETTINGS)
    assert m["requires_env"] == []


def test_schema_is_for_any_operator_and_states_the_prices():
    d = tools.RENT_SCHEMA["description"]
    assert "Drew" not in d and "refuse" not in d
    assert "0.001 USDC" in d and "0.007 USDC" in d and "one hour" in d
    assert "0.005" not in d and "30 second" not in d
    props = tools.RENT_SCHEMA["parameters"]["properties"]
    assert set(props) == {"urls", "script"}
    assert "required" not in tools.RENT_SCHEMA["parameters"]
    assert "\u2014" not in json.dumps(tools.RENT_SCHEMA, ensure_ascii=False)
    m = yaml.safe_load((ROOT / "src/charter_tool/plugin.yaml").read_text())
    assert "Drew" not in m["description"] and "0.001 USDC" in m["description"]
    assert "0.007 USDC" in m["description"] and "0.005" not in m["description"]


def test_the_schedule_is_the_live_hosts_hourly_one():
    # charter.mainnet.toonprotocol.dev sells one hour at 0.007 USDC since 2026-11-04; one hour
    # covers any fetch or short script, and no setting may buy more than a day in one call.
    assert tools.TICK_UNITS == 7000 and tools.INTERVAL_SECONDS == 3600
    assert tools.DEFAULT_MAX_INTERVALS == 1 and tools.HARD_MAX_INTERVALS == 24
    assert tools.price_line(1, 5) == PRICE_1_5
    assert "at most 2 intervals (0.015 USDC)" in tools.price_line(2, 5)


def test_the_subprocess_waits_out_the_paid_hours_and_no_longer():
    assert tools.run_seconds(1) == tools.BOOT_SECONDS + 3600 + 60
    assert tools.run_seconds(24) == tools.BOOT_SECONDS + 24 * 3600 + 60


# ---------- the allowlist: empty means everyone the operator allowed on the platform ----------


@pytest.mark.parametrize("platform,user_id", [("telegram", "999"), ("", ""), ("discord", "1")])
def test_without_an_allowlist_every_caller_may_rent(monkeypatch, platform, user_id):
    with_bridge(monkeypatch)
    as_session(monkeypatch, platform, user_id)
    seen = []
    fake_job(monkeypatch, seen=seen)
    out = rent({"urls": URLS})
    assert "error" not in out and out["leaseId"] == "L1"
    assert seen[0][1] == {"urls": URLS}


def test_with_an_allowlist_only_its_entries_may_rent(monkeypatch):
    with_bridge(monkeypatch)
    monkeypatch.setenv("CHARTER_ALLOWED_IDS", "telegram:111, discord:222")
    no_job(monkeypatch)
    wrong = (("telegram", "999"), ("discord", "111"), ("telegram", "222"), ("Telegram", "111"),
             ("telegram", "1111"))
    for platform, user_id in wrong:
        as_session(monkeypatch, platform, user_id)
        assert "CHARTER_ALLOWED_IDS" in rent({"urls": URLS})["error"], (platform, user_id)
    for platform, user_id in (("telegram", "111"), ("discord", "222")):
        as_session(monkeypatch, platform, user_id)
        fake_job(monkeypatch)
        assert rent({"urls": URLS})["leaseId"] == "L1"


@pytest.mark.parametrize("platform,user_id", [("", ""), ("telegram", ""), ("", "111")])
def test_with_an_allowlist_an_unidentified_caller_is_refused(monkeypatch, platform, user_id):
    with_bridge(monkeypatch)
    monkeypatch.setenv("CHARTER_ALLOWED_IDS", "telegram:111")
    as_session(monkeypatch, platform, user_id)
    no_job(monkeypatch)
    assert "could not be identified" in rent({"urls": URLS})["error"]


def test_with_an_allowlist_a_lookup_failure_inside_hermes_fails_closed(monkeypatch):
    with_bridge(monkeypatch)
    monkeypatch.setenv("CHARTER_ALLOWED_IDS", "telegram:111")

    def broken(name):
        raise RuntimeError("no gateway")

    monkeypatch.setattr(tools, "_session_env", broken)
    no_job(monkeypatch)
    assert "refused" in rent({"script": SCRIPT})["error"]


def test_without_an_allowlist_the_session_is_never_looked_up(monkeypatch):
    with_bridge(monkeypatch)

    def broken(name):
        raise AssertionError("looked up the caller")

    monkeypatch.setattr(tools, "_session_env", broken)
    fake_job(monkeypatch)
    assert rent({"urls": URLS})["leaseId"] == "L1"


# ---------- caps: one call, one day ----------


@pytest.mark.parametrize("value", ["0", "25", "x", "-1", "1.5", " 6 6"])
def test_a_malformed_interval_cap_refuses_by_name_before_anything_runs(monkeypatch, value):
    with_bridge(monkeypatch)
    monkeypatch.setenv("CHARTER_MAX_INTERVALS", value)
    no_job(monkeypatch)
    assert "CHARTER_MAX_INTERVALS" in rent({"urls": URLS})["error"]
    assert tools.available() is False


@pytest.mark.parametrize("value", ["x", "-2", "1.0"])
def test_a_malformed_daily_cap_refuses_by_name_before_anything_runs(monkeypatch, value):
    with_bridge(monkeypatch)
    monkeypatch.setenv("CHARTER_DAILY_LEASES", value)
    no_job(monkeypatch)
    assert "CHARTER_DAILY_LEASES" in rent({"urls": URLS})["error"]


def test_a_daily_cap_of_zero_refuses_every_call(monkeypatch, tmp_path):
    with_bridge(monkeypatch)
    monkeypatch.setenv("CHARTER_DAILY_LEASES", "0")
    no_job(monkeypatch)
    assert "CHARTER_DAILY_LEASES" in rent({"urls": URLS})["error"]
    assert not day_count(tmp_path).exists()


def test_the_default_caps_are_one_interval_and_five_leases(monkeypatch):
    with_bridge(monkeypatch)
    seen = []
    fake_job(monkeypatch, seen=seen)
    out = rent({"urls": URLS})
    assert seen[0][0]["max_intervals"] == 1 and seen[0][0]["daily_leases"] == 5
    assert out["price"] == PRICE_1_5


def test_the_day_counter_refuses_the_lease_past_the_cap(monkeypatch, tmp_path):
    with_bridge(monkeypatch)
    monkeypatch.setenv("CHARTER_DAILY_LEASES", "2")
    seen = []
    fake_job(monkeypatch, seen=seen)
    assert rent({"urls": URLS})["leaseId"] == "L1"
    assert rent({"script": SCRIPT})["leaseId"] == "L1"
    third = rent({"urls": URLS})
    assert "cap of 2" in third["error"]
    assert len(seen) == 2
    assert day_count(tmp_path).read_text() == "2\n"


def test_the_lease_is_counted_before_the_job_runs(monkeypatch, tmp_path):
    with_bridge(monkeypatch)
    monkeypatch.setenv("CHARTER_DAILY_LEASES", "1")

    def crash(s, job):
        raise OSError("the box went away")

    monkeypatch.setattr(tools, "run_job", crash)
    with pytest.raises(OSError):
        tools.charter_rent({"urls": URLS})
    assert day_count(tmp_path).read_text() == "1\n"
    no_job(monkeypatch)
    assert "cap of 1" in rent({"urls": URLS})["error"]


@pytest.mark.parametrize("content", ["garbage\n", "", "-1\n", "3 4\n"])
def test_a_corrupt_day_counter_counts_as_the_cap_reached(monkeypatch, tmp_path, content):
    with_bridge(monkeypatch)
    day_count(tmp_path).parent.mkdir(parents=True)
    day_count(tmp_path).write_text(content)
    no_job(monkeypatch)
    assert "cap" in rent({"urls": URLS})["error"]
    assert day_count(tmp_path).read_text() == content


def test_bad_arguments_are_refused_before_a_lease_is_counted(monkeypatch, tmp_path):
    with_bridge(monkeypatch)
    no_job(monkeypatch)
    bad = [{"urls": URLS, "script": SCRIPT}, {}, {"urls": None, "script": None},
           {"urls": []}, {"urls": "https://a.com"}, {"urls": ["ftp://a.com"]},
           {"urls": ["https://a.com"] * 6},
           {"urls": [1]}, {"urls": ["https://a.com/x,y"]}, {"urls": ["https://a.com/ x"]},
           {"urls": ["--max-intervals"]},
           {"script": ""}, {"script": "  \n\t"}, {"script": "print(1)\0"}, {"script": 42},
           {"script": ["print(1)"]},
           {"script": "x" * (8 * 1024 + 1)}, {"script": "é" * 4097}, {"script": "print('\ud800')"}]
    for args in bad:
        assert "error" in rent(args), args
    assert not day_count(tmp_path).exists()


def test_a_script_of_exactly_8_kib_is_taken(monkeypatch):
    with_bridge(monkeypatch)
    seen = []
    answer = {"leaseId": "L2", "state": "done", "paidIntervals": 1, "spent": "6000", "output": PY_OUT}
    fake_job(monkeypatch, answer, seen)
    script = "#" + "é" * 4095 + "\n"  # 1 + 8190 + 1 bytes
    assert len(script.encode()) == 8192
    assert "error" not in rent({"script": script})
    assert seen[0][1] == {"script": script}


# ---------- the answer: small, with the price and the spend in USDC ----------


def test_the_answer_carries_the_spend_and_the_price(monkeypatch):
    with_bridge(monkeypatch)
    monkeypatch.setenv("CHARTER_MAX_INTERVALS", "10")
    monkeypatch.setenv("CHARTER_DAILY_LEASES", "3")
    fake_job(monkeypatch)
    out = rent({"urls": URLS})
    assert out == {"leaseId": "L1", "state": "closed", "paidIntervals": 2, "spent": "11000",
                   "spentUsdc": "0.011",
                   "price": "0.001 USDC to open plus 0.007 USDC per one hour interval, at most 10 intervals "
                            "(0.071 USDC) per call; 3 leases a day",
                   "pages": [{"url": URLS[0], "text": "hello"}]}
    assert "jpegBase64" not in json.dumps(out)


def test_page_text_is_cut_to_2_kib(monkeypatch):
    with_bridge(monkeypatch)
    fake_job(monkeypatch, {"leaseId": "L", "state": "closed", "paidIntervals": 1, "spent": "6000",
                           "output": {"pages": [{"url": "https://e.com", "text": "x" * 5000,
                                                 "jpegBase64": "B" * 9000}]}})
    assert len(rent({"urls": ["https://e.com"]})["pages"][0]["text"]) == 2048


def test_a_python_lease_returns_the_cut_output(monkeypatch):
    with_bridge(monkeypatch)
    fake_job(monkeypatch, {"leaseId": "L2", "state": "done", "paidIntervals": 1, "spent": "6500",
                           "output": PY_OUT})
    out = rent({"script": SCRIPT})
    assert out["python"] == {k: v for k, v in PY_OUT.items() if k != "kind"}
    assert out["spentUsdc"] == "0.0065"
    big = {**PY_OUT, "exitCode": 1, "stdout": "o" * 9000, "stderr": "e" * 9000, "stderrTruncated": True}
    fake_job(monkeypatch, {"leaseId": "L", "state": "done", "output": big})
    py = rent({"script": SCRIPT})["python"]
    assert (len(py["stdout"]), len(py["stderr"])) == (4096, 2048)
    assert py["stdoutTruncated"] is True and py["stderrTruncated"] is True and py["exitCode"] == 1


def test_a_python_lease_without_python_output_says_so(monkeypatch):
    with_bridge(monkeypatch)
    for output in (None, {"pages": []}, "junk"):
        fake_job(monkeypatch, {"leaseId": "L", "state": "failed", "output": output})
        assert rent({"script": SCRIPT})["python"] is None
    fake_job(monkeypatch, {"leaseId": "L", "state": "open", "paidIntervals": 0, "spent": "0"})
    assert rent({"urls": URLS})["pages"] == []


def test_a_job_error_is_passed_on_cut(monkeypatch):
    with_bridge(monkeypatch)
    fake_job(monkeypatch, {"error": "today's cap of 5 leases is reached" + "!" * 900})
    err = rent({"urls": URLS})["error"]
    assert err.startswith("today's cap") and len(err) <= 500


@pytest.mark.parametrize("units,text", [("11000", "0.011"), (31000, "0.031"), ("0", "0"),
                                        ("6500", "0.0065"), (None, None), ("x", None), ("", None)])
def test_usdc_formats_base_units(units, text):
    assert tools.usdc(units) == text


# ---------- settings come through Hermes's secret scope when it is there ----------


def test_settings_prefer_hermes_secret_scope_over_the_environment(monkeypatch):
    scoped = {"CHARTER_MAX_INTERVALS": "3", "CHARTER_OFFER_ID": "scoped:offer"}
    module = types.ModuleType("agent.secret_scope")
    module.get_secret_str = lambda name, default="": scoped.get(name, os.environ.get(name, default))
    agent = types.ModuleType("agent")
    agent.secret_scope = module
    monkeypatch.setitem(sys.modules, "agent", agent)
    monkeypatch.setitem(sys.modules, "agent.secret_scope", module)
    monkeypatch.setenv("CHARTER_MAX_INTERVALS", "9")
    monkeypatch.setenv("CHARTER_RELAY_URL", "wss://env.example/")
    s = tools.settings()
    assert s["max_intervals"] == 3 and s["offer"] == "scoped:offer" and s["relay"] == "wss://env.example/"


def test_settings_have_safe_defaults(monkeypatch, tmp_path):
    monkeypatch.delenv("CHARTER_STATE_DIR")
    s = tools.settings()
    assert s["max_intervals"] == 1 and s["daily_leases"] == 5 and s["allowed_ids"] == set()
    assert s["relay"] == "wss://relay.mainnet.toonprotocol.dev" and s["bin"] == "charter"
    assert s["bridge_cmd"] == "" and s["passphrase_file"] == "" and s["offer"] == ""
    assert s["state_dir"] == Path.home() / ".charter-tool"


# ---------- direct mode: charter on this machine ----------


@pytest.fixture
def charter(tmp_path):
    """A fake `charter` that records how it was run, and a passphrase file it must never print."""
    (tmp_path / "secrets").mkdir()
    (tmp_path / "secrets/passphrase").write_text("do-not-print-me")
    fake = tmp_path / "bin" / "charter"
    fake.parent.mkdir()
    log = tmp_path / "charter.log"
    fake.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        "a = sys.argv[1:]\n"
        "py = a[a.index('--python') + 1] if '--python' in a else None\n"
        "script = open(py, encoding='utf-8').read() if py else None\n"
        "mode = oct(os.stat(py).st_mode & 0o777) if py else None\n"
        f"open({str(log)!r}, 'a').write(json.dumps({{'argv': a, 'script': script, 'mode': mode, "
        "'py': py, "
        "'pass': os.environ.get('TOON_PASSPHRASE_FILE'), 'env': sorted(os.environ)}) + '\\n')\n"
        f"if os.path.exists({str(tmp_path / 'fail')!r}):\n"
        "    sys.stderr.write('boom\\n'); sys.exit(1)\n"
        "sys.stderr.write('opened lease L1\\n')\n"
        "print(json.dumps({'leaseId': 'L1', 'state': 'closed', 'paidIntervals': 1, 'spent': '6000',\n"
        " 'output': {'pages': [{'url': 'https://example.com/a', 'text': 'hi', 'jpegBase64': 'AA'}]}}))\n"
    )
    fake.chmod(0o755)
    return {"bin": fake, "log": log, "fail": tmp_path / "fail", "pass": tmp_path / "secrets/passphrase"}


def direct(monkeypatch, charter, **env):
    monkeypatch.setenv("CHARTER_BIN", str(charter["bin"]))
    monkeypatch.setenv("TOON_PASSPHRASE_FILE", str(charter["pass"]))
    monkeypatch.setenv("CHARTER_OFFER_ID", "a" * 64 + ":gpu1")
    for k, v in env.items():
        monkeypatch.setenv(k, v)


def charter_calls(charter):
    if not charter["log"].exists():
        return []
    return [json.loads(line) for line in charter["log"].read_text().splitlines()]


def test_direct_mode_runs_charter_with_the_fixed_command_line(monkeypatch, charter, tmp_path):
    direct(monkeypatch, charter)
    out = rent({"urls": URLS})
    assert out["leaseId"] == "L1" and out["spentUsdc"] == "0.006" and out["price"] == PRICE_1_5
    assert out["pages"] == [{"url": URLS[0], "text": "hi"}]
    (call,) = charter_calls(charter)
    assert call["argv"] == ["rent", "--offer", "a" * 64 + ":gpu1", "--urls", ",".join(URLS),
                            "--max-intervals", "1", "--relay", "wss://relay.mainnet.toonprotocol.dev",
                            "--json"]
    assert call["pass"] == str(charter["pass"])
    assert day_count(tmp_path).read_text() == "1\n"


def test_direct_mode_honours_the_interval_cap_and_the_relay(monkeypatch, charter):
    direct(monkeypatch, charter, CHARTER_MAX_INTERVALS="12", CHARTER_RELAY_URL="wss://relay.example/")
    out = rent({"urls": URLS})
    assert "at most 12 intervals (0.085 USDC)" in out["price"]
    (call,) = charter_calls(charter)
    assert call["argv"][call["argv"].index("--max-intervals") + 1] == "12"
    assert call["argv"][call["argv"].index("--relay") + 1] == "wss://relay.example/"


def test_direct_mode_hands_charter_a_minimal_env_and_prints_no_secret(monkeypatch, charter):
    direct(monkeypatch, charter)
    monkeypatch.setenv("SOME_SECRET_TOKEN", "x")
    monkeypatch.setenv("NODE_OPTIONS", "--require /tmp/evil.js")
    text = tools.charter_rent({"urls": URLS})
    (call,) = charter_calls(charter)
    env = set(call["env"]) - {"__CF_USER_TEXT_ENCODING", "LC_CTYPE"}  # added by macOS / Python at exec
    assert env == {"PATH", "HOME", "TOON_PASSPHRASE_FILE"}, env
    assert "do-not-print-me" not in text


def test_direct_mode_runs_a_script_from_a_private_temp_file(monkeypatch, charter, tmp_path):
    direct(monkeypatch, charter)
    out = rent({"script": SCRIPT})
    assert out["leaseId"] == "L1" and out["python"] is None  # the fake answers with pages
    (call,) = charter_calls(charter)
    assert call["script"] == SCRIPT and call["mode"] == "0o600"
    assert Path(call["py"]).parent == tmp_path / "state"
    assert not Path(call["py"]).exists(), "the temp file is removed after charter runs"
    assert "--urls" not in call["argv"]


def test_direct_mode_removes_the_script_when_charter_fails(monkeypatch, charter, tmp_path):
    direct(monkeypatch, charter)
    charter["fail"].touch()
    out = rent({"script": SCRIPT})
    assert out["error"].startswith("charter failed: boom")
    assert not list((tmp_path / "state").glob("script-*"))
    assert day_count(tmp_path).read_text() == "1\n", "a failed lease still counts"


def test_direct_mode_needs_the_passphrase_file_and_the_offer_by_name(monkeypatch, charter, tmp_path):
    monkeypatch.setenv("CHARTER_BIN", str(charter["bin"]))
    err = rent({"urls": URLS})["error"]
    assert "TOON_PASSPHRASE_FILE" in err and "CHARTER_OFFER_ID" in err
    monkeypatch.setenv("TOON_PASSPHRASE_FILE", str(charter["pass"]))
    err = rent({"urls": URLS})["error"]
    assert "CHARTER_OFFER_ID" in err and "TOON_PASSPHRASE_FILE" not in err
    assert charter_calls(charter) == [] and not day_count(tmp_path).exists()
    assert str(charter["pass"]) not in err


def test_direct_mode_without_a_charter_binary_says_how_to_install(monkeypatch, tmp_path):
    monkeypatch.setenv("CHARTER_BIN", str(tmp_path / "nope"))
    monkeypatch.setenv("TOON_PASSPHRASE_FILE", "/x")
    monkeypatch.setenv("CHARTER_OFFER_ID", "a:b")
    err = rent({"urls": URLS})["error"]
    assert "not found" in err and "npm install -g" in err and "CHARTER_BRIDGE_CMD" in err
    assert not day_count(tmp_path).exists()


def test_available_follows_the_bridge_or_the_binary(monkeypatch, charter, tmp_path):
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    assert tools.available() is False
    monkeypatch.setenv("CHARTER_BIN", str(charter["bin"]))
    assert tools.available() is True
    monkeypatch.setenv("CHARTER_BIN", str(tmp_path / "nope"))
    assert tools.available() is False
    monkeypatch.setenv("CHARTER_BRIDGE_CMD", "limactl shell toon -- bash -lc '~/bin/charter-bridge rent'")
    assert tools.available() is True
    monkeypatch.delenv("CHARTER_BRIDGE_CMD")
    monkeypatch.delenv("CHARTER_BIN")
    monkeypatch.setenv("PATH", str(charter["bin"].parent))
    assert tools.available() is True, "a bare charter on PATH is enough"


# ---------- bridge mode: the job goes to the operator's command as JSON on stdin ----------


@pytest.fixture
def bridge_cmd(tmp_path):
    """A stand-in for `limactl shell ... charter-bridge rent`: echoes its stdin into a log, answers JSON."""
    script = tmp_path / "fake-bridge.py"
    log = tmp_path / "bridge.log"
    script.write_text(
        "import json, sys\n"
        f"open({str(log)!r}, 'a')"
        ".write(json.dumps({'argv': sys.argv[1:], 'stdin': sys.stdin.read()}) + '\\n')\n"
        "if 'fail' in sys.argv:\n"
        "    print(json.dumps({'error': \"today's cap of 5 leases is reached\"})); sys.exit(3)\n"
        "sys.stderr.write('noise\\n')\n"
        "print('progress line that is not json')\n"
        "print(json.dumps({'leaseId': 'B1', 'state': 'closed', 'paidIntervals': 3, 'spent': '16000', "
        "'output': "
        "{'pages': [{'url': 'https://example.com/a', 'text': 'via bridge'}]}}))\n"
    )
    return {"cmd": f"{sys.executable} {script} rent", "log": log}


def test_bridge_mode_sends_the_job_on_stdin_and_reads_the_last_json_line(monkeypatch, bridge_cmd,
                                                                          tmp_path):
    monkeypatch.setenv("CHARTER_BRIDGE_CMD", bridge_cmd["cmd"] + " 'quoted arg'")
    out = rent({"urls": URLS})
    assert out["leaseId"] == "B1" and out["spentUsdc"] == "0.016"
    assert out["pages"] == [{"url": URLS[0], "text": "via bridge"}]
    (call,) = [json.loads(line) for line in bridge_cmd["log"].read_text().splitlines()]
    assert call["argv"] == ["rent", "quoted arg"]
    assert json.loads(call["stdin"]) == {"urls": URLS}
    assert day_count(tmp_path).read_text() == "1\n", "the plugin counts the day even when a bridge counts too"


def test_bridge_mode_needs_no_passphrase_file_or_offer_here(monkeypatch, bridge_cmd):
    monkeypatch.setenv("CHARTER_BRIDGE_CMD", bridge_cmd["cmd"])
    monkeypatch.delenv("TOON_PASSPHRASE_FILE", raising=False)
    monkeypatch.delenv("CHARTER_OFFER_ID", raising=False)
    assert rent({"script": SCRIPT})["leaseId"] == "B1"


def test_bridge_mode_passes_the_bridge_error_on(monkeypatch, bridge_cmd):
    monkeypatch.setenv("CHARTER_BRIDGE_CMD", bridge_cmd["cmd"] + " fail")
    assert "cap of 5" in rent({"urls": URLS})["error"]


def test_bridge_mode_reports_a_command_that_cannot_run(monkeypatch, tmp_path):
    monkeypatch.setenv("CHARTER_BRIDGE_CMD", str(tmp_path / "missing-bridge") + " rent")
    assert "did not finish" in rent({"urls": URLS})["error"]


def test_the_shipped_bridge_script_works_as_the_bridge_command(monkeypatch, vm, tmp_path):
    """The Lima path, minus Lima: CHARTER_BRIDGE_CMD runs vm/charter-bridge straight."""
    monkeypatch.setenv("HOME", str(vm["home"]))
    monkeypatch.setenv("CHARTER_BIN", str(vm["fake"]))
    monkeypatch.setenv("CHARTER_BRIDGE_CMD", f"{sys.executable} {BRIDGE} rent")
    out = rent({"urls": URLS})
    assert out["leaseId"] == "L1" and out["spentUsdc"] == "0.006"
    (call,) = [json.loads(line) for line in vm["log"].read_text().splitlines()]
    assert call["argv"][:3] == ["rent", "--offer", "a" * 64 + ":gpu1"]
    assert stat.S_IMODE(os.stat(vm["home"] / ".charter-bridge").st_mode) == 0o700


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
        "a = sys.argv[1:]\n"
        "py = a[a.index('--python') + 1] if '--python' in a else None\n"
        "script = open(py, encoding='utf-8').read() if py else None\n"
        "mode = oct(os.stat(py).st_mode & 0o777) if py else None\n"
        f"open({str(log)!r}, 'a').write(json.dumps({{'argv': a, 'script': script, 'mode': mode, "
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
                            "--max-intervals", "1", "--relay", "wss://relay.example/", "--json"]
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


# ---------- F-I6: the day counter fails closed, the cap is fixed, charter gets a minimal env ----------


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


# ---------- python job: exactly one of urls and script, the script is checked, stdout comes back ----------



def test_bridge_runs_charter_with_the_script_in_a_private_temp_file(vm):
    done = run_bridge(vm, "rent", stdin={"script": SCRIPT})
    assert done.returncode == 0, done.stderr
    (call,) = calls(vm)
    argv = call["argv"]
    path = argv[argv.index("--python") + 1]
    assert argv == ["rent", "--offer", "a" * 64 + ":gpu1", "--python", path,
                    "--max-intervals", "1", "--relay", "wss://relay.example/", "--json"]
    assert "--urls" not in argv
    assert call["script"] == SCRIPT
    assert call["mode"] == "0o600"
    assert Path(path).parent == vm["home"] / ".charter-bridge"
    assert not Path(path).exists(), "the temp file is removed after charter runs"


def test_bridge_removes_the_script_when_charter_fails(vm):
    done = run_bridge(vm, "rent", stdin={"script": SCRIPT}, extra={"FAKE_FAIL": "1"})
    assert done.returncode != 0
    assert not list((vm["home"] / ".charter-bridge").glob("script-*"))


def test_bridge_script_lease_counts_against_the_same_day_cap(vm):
    for i in range(5):
        stdin = {"script": SCRIPT} if i % 2 else {"urls": URLS}
        assert run_bridge(vm, "rent", stdin=stdin).returncode == 0
    sixth = run_bridge(vm, "rent", stdin={"script": SCRIPT})
    assert sixth.returncode == 3
    assert "cap of 5" in json.loads(sixth.stdout)["error"]
    assert len(calls(vm)) == 5


def test_bridge_takes_a_worst_case_escaped_8_kib_script(vm):
    script = "#" + "\x01" * 8190 + "\n"  # each \x01 is six bytes of JSON
    done = run_bridge(vm, "rent", stdin={"script": script})
    assert done.returncode == 0, done.stdout
    assert calls(vm)[0]["script"] == script


@pytest.mark.parametrize("stdin", [
    {"urls": URLS, "script": SCRIPT}, {"script": ""}, {"script": " \n"}, {"script": "print(1)\0"},
    {"script": 7}, {"script": None}, {"script": "x" * 8193}, {"script": "é" * 4097},
    {"script": "print('\ud800')"}, {"urls": None},
])
def test_bridge_rejects_bad_scripts_without_spending_a_lease(vm, stdin):
    done = run_bridge(vm, "rent", stdin=stdin)
    assert done.returncode != 0
    assert "error" in json.loads(done.stdout)
    assert calls(vm) == []
    assert not list((vm["home"] / ".charter-bridge").glob("leases-*"))
    assert not list((vm["home"] / ".charter-bridge").glob("script-*"))


def test_bridge_rejects_oversized_stdin_without_spending_a_lease(vm):
    env = {"PATH": os.environ["PATH"], "HOME": str(vm["home"]), "CHARTER_BIN": str(vm["fake"])}
    done = subprocess.run([sys.executable, str(BRIDGE), "rent"], input='{"script": "' + "x" * 70000 + '"}',
                          capture_output=True, text=True, timeout=30, env=env)
    assert done.returncode != 0
    assert "error" in json.loads(done.stdout)
    assert calls(vm) == []

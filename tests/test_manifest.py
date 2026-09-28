"""The manifest and the package agree with the provider."""

from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def test_manifest_declares_what_the_code_does():
    m = yaml.safe_load((ROOT / "src/hermes_toon/plugin.yaml").read_text())
    assert m["name"] == "web-toon"
    assert m["kind"] == "backend"
    assert m["provides_web_providers"] == ["toon"]
    assert [e["name"] for e in m["requires_env"]] == ["TOON_PAYER_URL"]
    assert m["provides_tools"] == [] and m["provides_hooks"] == [] and m["provides_middleware"] == []
    assert "Disclosure" in m["description"]


def test_versions_agree():
    import hermes_toon

    m = yaml.safe_load((ROOT / "src/hermes_toon/plugin.yaml").read_text())
    py = (ROOT / "pyproject.toml").read_text()
    assert m["version"] == hermes_toon.__version__
    assert f'version = "{hermes_toon.__version__}"' in py

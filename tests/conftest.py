"""Run against a real Hermes checkout when HERMES_SRC points at one; otherwise stub the
four ``plugins.web._common`` names the provider uses, so the suite runs anywhere."""

from __future__ import annotations

import os
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

hermes_src = os.environ.get("HERMES_SRC")
if hermes_src:
    sys.path.insert(0, hermes_src)
else:
    common = types.ModuleType("plugins.web._common")

    class BaseWebSearchProvider:  # minimal twin of Hermes's helper base
        NAME = ""
        DISPLAY_NAME = ""
        KEY_ENV = ""
        EXTRACT = False
        KEYLESS = False
        name = property(lambda self: self.NAME)
        display_name = property(lambda self: self.DISPLAY_NAME)

        def is_available(self):
            return bool(provider_env(self.KEY_ENV))

        def is_keyless_available(self):
            return False

        def supports_search(self):
            return True

        def supports_extract(self):
            return self.EXTRACT

    def provider_env(name):
        return os.environ.get(name, "").strip()

    def document(url, title, content, *, source_url=None):
        return {"url": url, "title": title, "content": content, "raw_content": content,
                "metadata": {"sourceURL": url if source_url is None else source_url, "title": title}}

    def page_error(url, error):
        return {"url": url, "title": "", "content": "", "error": error}

    def run_extract(vendor, logger, urls, body, **_):
        try:
            return body()
        except Exception as exc:  # noqa: BLE001
            msg = str(exc) if isinstance(exc, ValueError) else f"{vendor} extract failed: {exc}"
            return [page_error(u, msg) for u in urls]

    def setup_schema(name, badge, tag, key_env="", prompt="", url="", **extra):
        env_vars = [{"key": key_env, "prompt": prompt, "url": url}] if key_env else []
        return {"name": name, "badge": badge, "tag": tag, "env_vars": env_vars, **extra}

    for k, v in dict(BaseWebSearchProvider=BaseWebSearchProvider, provider_env=provider_env, document=document,
                     page_error=page_error, run_extract=run_extract, setup_schema=setup_schema).items():
        setattr(common, k, v)
    plugins = types.ModuleType("plugins")
    web = types.ModuleType("plugins.web")
    plugins.web = web
    web._common = common
    sys.modules.setdefault("plugins", plugins)
    sys.modules.setdefault("plugins.web", web)
    sys.modules["plugins.web._common"] = common

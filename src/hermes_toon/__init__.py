"""Hermes Agent plugin: a paid web-extract provider over TOON.

Registers the ``toon`` web provider. Select it with ``web.extract_backend: toon``
in ``~/.hermes/config.yaml`` and point ``TOON_PAYER_URL`` at a running anonfetch
payer sidecar (github.com/drew-dot-com/anonfetch/payer).
"""

from __future__ import annotations

from typing import Any

__version__ = "0.1.1"


def register(ctx: Any) -> None:
    from .provider import ToonWebProvider

    ctx.register_web_search_provider(ToonWebProvider())

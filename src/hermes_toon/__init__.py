"""Hermes Agent plugin: a paid web-extract provider over TOON.

Registers the ``toon`` web provider. Select it with ``web.extract_backend: toon``
in ``~/.hermes/config.yaml`` and point ``TOON_PAYER_URL`` at a running payer:
the anonfetch payer sidecar (github.com/drew-dot-com/anonfetch/payer) or a
``charter payer`` (github.com/drew-dot-com/charter), which speak the same
``GET /extract?url=`` contract.
"""

from __future__ import annotations

from typing import Any

__version__ = "0.1.3"


def register(ctx: Any) -> None:
    from .provider import ToonWebProvider

    ctx.register_web_search_provider(ToonWebProvider())

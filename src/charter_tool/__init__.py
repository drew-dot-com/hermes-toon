"""charter-tool: let a Hermes agent rent one Charter compute lease and run one job in it.

Registers ``charter_rent`` in toolset ``charter``. The operator provides the ``charter`` CLI and
a ``toon`` node (or a bridge command to a machine that has them), the wallet passphrase file and
an offer id; the caps and the allowlist are settings with safe defaults. See ``tools.py``.
"""

from . import tools

__version__ = "0.3.0"


def register(ctx) -> None:
    ctx.register_tool(name="charter_rent", toolset="charter", schema=tools.RENT_SCHEMA,
                      handler=tools.charter_rent, check_fn=tools.available, emoji="🏷️")

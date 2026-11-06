"""charter-tool: let a Hermes agent rent one Charter compute lease and run one job in it.

Registers ``charter_rent`` in toolset ``charter``. The operator provides the ``charter`` CLI and
a ``toon`` node (or a bridge command to a machine that has them), the wallet passphrase file and
an offer id; the caps and the allowlist are settings with safe defaults. A ``pre_tool_call`` hook
sends every ``charter_rent`` call to Hermes's approval gate, so a human says yes before each
payment. See ``tools.py``.
"""

from . import tools

__version__ = "0.5.0"


def register(ctx) -> None:
    ctx.register_tool(name="charter_rent", toolset="charter", schema=tools.RENT_SCHEMA,
                      handler=tools.charter_rent, check_fn=tools.available, emoji="🏷️")
    ctx.register_hook("pre_tool_call", tools.pre_tool_call)

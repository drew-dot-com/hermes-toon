"""charter-tool: let Edison rent one Charter lease for an allowed caller."""

from . import tools


def register(ctx) -> None:
    ctx.register_tool(name="charter_rent", toolset="charter", schema=tools.RENT_SCHEMA,
                      handler=tools.charter_rent, check_fn=tools.available, emoji="🏷️")

"""toon-node: let a Hermes agent post to, read from and report on its TOON payer, through three tools."""

from . import tools

__version__ = "0.2.0"


def register(ctx) -> None:
    ctx.register_tool(name="toon_post", toolset="toon", schema=tools.POST_SCHEMA,
                      handler=tools.toon_post, check_fn=tools.available, emoji="📮")
    ctx.register_tool(name="toon_read", toolset="toon", schema=tools.READ_SCHEMA,
                      handler=tools.toon_read, is_async=True, check_fn=tools.available, emoji="📖")
    ctx.register_tool(name="toon_status", toolset="toon", schema=tools.STATUS_SCHEMA,
                      handler=tools.toon_status, check_fn=tools.available, emoji="🪙")

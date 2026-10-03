"""toon-node: let a Hermes agent use its own toon agent node through three narrow tools."""

from . import tools


def register(ctx) -> None:
    ctx.register_tool(name="toon_post", toolset="toon", schema=tools.POST_SCHEMA,
                      handler=tools.toon_post, check_fn=tools.available, emoji="📮")
    ctx.register_tool(name="toon_read", toolset="toon", schema=tools.READ_SCHEMA,
                      handler=tools.toon_read, is_async=True, emoji="📖")
    ctx.register_tool(name="toon_status", toolset="toon", schema=tools.STATUS_SCHEMA,
                      handler=tools.toon_status, check_fn=tools.available, emoji="🪙")

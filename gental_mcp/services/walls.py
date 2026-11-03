"""The connection wall: who may reach /mcp at all (spec §5.2, wall 2)."""
class McpForbidden(Exception):
    """The server is switched off or the user may not connect (HTTP 403)."""


def check_connect(env, user) -> None:
    """Raise ``McpForbidden`` unless the server is on and ``user`` is active and in a connect group.

    Checked on every request: removing a user from the groups (from either side of the relation)
    takes effect on the next call, whether or not their tokens were revoked.
    """
    config = env["gental.mcp.config"].get()
    if not config.enabled:
        raise McpForbidden("disabled")
    if not config.may_connect(user):
        raise McpForbidden("not_allowed")

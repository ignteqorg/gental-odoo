from odoo import models

from ..compat import all_groups_field


def _connected_users(env, users=None):
    """Active users (optionally within ``users``) in one of the groups allowed to connect, as sudo.

    Private to ``models/``: elsewhere, use ``gental.mcp.config.may_connect(user)``.
    """
    config = env.ref("gental_mcp.config_main", raise_if_not_found=False)
    if not config or not config.sudo().connect_group_ids:
        return env["res.users"].sudo().browse()
    domain = [(all_groups_field(env), "in", config.sudo().connect_group_ids.ids)]
    if users is not None:
        domain.append(("id", "in", users.ids))
    return env["res.users"].sudo().search(domain)


# Deprecated alias kept so in-flight branches (models only) still merge; use ``_connected_users``.
connected_users = _connected_users


def revoke_lost(env, before, users=None):
    """Revoke everything of the users in ``before`` who are no longer connected."""
    lost = before - _connected_users(env, users)
    if lost:
        env["gental.mcp.token"].revoke_for_user(lost)


class ResUsers(models.Model):
    _inherit = "res.users"

    def write(self, vals):
        # Group changes arrive as the groups field or, before 19.0, as virtual in_group_*/sel_groups_* keys.
        if "active" not in vals and not any("group" in key for key in vals):
            return super().write(vals)
        before = _connected_users(self.env, self)
        res = super().write(vals)
        revoke_lost(self.env, before, self)
        return res


class ResGroups(models.Model):
    _inherit = "res.groups"

    def write(self, vals):
        # Members (user_ids / users) and implications both change who is effectively in a connect group.
        if not any("user" in key or "implied" in key for key in vals):
            return super().write(vals)
        before = _connected_users(self.env)
        res = super().write(vals)
        revoke_lost(self.env, before)
        return res

    def unlink(self):
        before = _connected_users(self.env)
        res = super().unlink()
        revoke_lost(self.env, before)
        return res

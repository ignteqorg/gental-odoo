"""OAuth access/refresh tokens and authorization codes, stored only as SHA-256 hashes.

Callers (the OAuth controllers) must turn ``AccessDenied`` into an error *response* rather than
re-raising it: the request transaction then commits, so a burnt auth code or a family revoked on
refresh-token reuse stays burnt/revoked.
"""
import re
import uuid
from datetime import timedelta

from odoo import api, fields, models
from odoo.exceptions import AccessDenied, AccessError
from odoo.tools.translate import _lt

from ..compat import create_unique_index, private, translate
from ..core.crypto import consteq, hash_secret, new_secret, pkce_s256
from ..core.urls import normalize_url
from .res_users import _connected_users

ACCESS_TTL = 3600
REFRESH_TTL = 30 * 24 * 3600
CODE_TTL = 60
PKCE_VERIFIER_RE = re.compile(r"^[A-Za-z0-9\-._~]{43,128}$")  # RFC 7636 §4.1
ONLY_ADMIN_REBINDS = _lt("Only an administrator can change who a token or code belongs to.")


def _check_protected_vals(records, vals_list, protected):
    """Only superuser code and system administrators may set the fields that decide who a secret acts
    as (``protected``): otherwise a manager could re-bind their own token or code to the admin."""
    if records.env.su or records.env.user._is_system():
        return
    if any(protected & set(vals) for vals in vals_list):
        # A module-level function: ``_`` finds no language here, so translate in the records' language.
        raise AccessError(translate(records.env.lang, ONLY_ADMIN_REBINDS))


class GentalMcpToken(models.Model):
    _name = "gental.mcp.token"
    _description = "Gental MCP OAuth Token"
    _order = "id desc"

    client_id = fields.Many2one("gental.mcp.client", required=True, ondelete="cascade", index=True)
    user_id = fields.Many2one("res.users", required=True, ondelete="cascade", index=True)
    kind = fields.Selection([("access", "Access"), ("refresh", "Refresh")], required=True)
    token_hash = fields.Char(required=True, groups="base.group_no_one")
    family = fields.Char(required=True, index=True)
    expires_at = fields.Datetime(required=True)
    revoked = fields.Boolean(index=True)
    rotated = fields.Boolean()
    scope = fields.Char()
    resource = fields.Char()

    PROTECTED_FIELDS = frozenset({"user_id", "client_id", "token_hash", "resource", "family", "kind"})

    def init(self):
        create_unique_index(self.env.cr, "gental_mcp_token_token_hash_uniq", self._table, ["token_hash"])

    @api.model_create_multi
    def create(self, vals_list):
        _check_protected_vals(self, vals_list, self.PROTECTED_FIELDS)
        return super().create(vals_list)

    def write(self, vals):
        _check_protected_vals(self, [vals], self.PROTECTED_FIELDS)
        return super().write(vals)

    @private
    @api.model
    def issue_pair(self, client, user, resource, family=None):
        """Issue an access + refresh token pair; returns the RFC 6749 token response body."""
        now = fields.Datetime.now()
        family = family or uuid.uuid4().hex
        access, refresh = new_secret("gmat"), new_secret("gmrt")
        common = {"client_id": client.id, "user_id": user.id, "family": family, "resource": resource}
        self.sudo().create([
            dict(common, kind="access", token_hash=hash_secret(access), expires_at=now + timedelta(seconds=ACCESS_TTL)),
            dict(common, kind="refresh", token_hash=hash_secret(refresh), expires_at=now + timedelta(seconds=REFRESH_TTL)),
        ])
        return {"access_token": access, "refresh_token": refresh, "expires_in": ACCESS_TTL, "token_type": "Bearer"}

    def _live_domain(self):
        return [
            ("revoked", "=", False),
            ("expires_at", ">", fields.Datetime.now()),
            ("client_id.state", "=", "active"),
            ("user_id.active", "=", True),
        ]

    @private
    @api.model
    def find_access(self, plaintext):
        """The live access token matching ``plaintext`` (sudo), or an empty recordset."""
        if not plaintext or not plaintext.startswith("gmat_"):
            return self.sudo().browse()
        return self.sudo().search(
            [("kind", "=", "access"), ("token_hash", "=", hash_secret(plaintext))] + self._live_domain(), limit=1
        )

    @private
    @api.model
    def is_known_access(self, plaintext):
        """The access token (sudo) this database issued for ``plaintext``, live or not; else empty (falsy)."""
        if not plaintext or not plaintext.startswith("gmat_"):
            return self.sudo().browse()
        return self.sudo().search([("kind", "=", "access"), ("token_hash", "=", hash_secret(plaintext))], limit=1)

    @private
    @api.model
    def rotate_refresh(self, plaintext, client, resource=None):
        """Exchange a refresh token for a new pair in the same family.

        Reusing an already rotated token revokes the whole family (RFC 9700 reuse detection).
        Returns the RFC 6749 token response body plus ``user_id`` (an int, for the Inbox; not sent).
        Each rotation issues a fresh 30-day refresh token, so the refresh expiry slides with use.
        When ``resource`` (the current MCP URL) is given, a token bound to another URL is refused, so a
        client whose server moved signs in again instead of refreshing tokens /mcp would reject.
        """
        if not plaintext or not plaintext.startswith("gmrt_"):
            raise AccessDenied()
        token = self.sudo().search([("kind", "=", "refresh"), ("token_hash", "=", hash_secret(plaintext))], limit=1)
        if not token or token.client_id != client:
            raise AccessDenied()
        if token.rotated:
            self.revoke_family(token.family)
            raise AccessDenied()
        if not token.filtered_domain(self._live_domain()):
            raise AccessDenied()
        if resource is not None and normalize_url(token.resource) != normalize_url(resource):
            raise AccessDenied()
        if not _connected_users(self.env, token.user_id):  # left the connect groups without a hook seeing it
            self.revoke_family(token.family)
            raise AccessDenied()
        token.rotated = True
        body = self.issue_pair(token.client_id, token.user_id, token.resource, family=token.family)
        return dict(body, user_id=token.user_id.id)

    @private
    @api.model
    def revoke_family(self, family):
        self.sudo().search([("family", "=", family), ("revoked", "=", False)]).write({"revoked": True})

    @private
    @api.model
    def revoke_token(self, plaintext, client_id=None):
        """Revoke the family of the access or refresh token ``plaintext`` (RFC 7009).

        When ``client_id`` is given, a token issued to another client is left alone. Unknown tokens are ignored.
        Returns the revoked token (sudo), or an empty recordset.
        """
        if not isinstance(plaintext, str) or not plaintext.startswith(("gmat_", "gmrt_")):
            return self.sudo().browse()
        token = self.sudo().search([("token_hash", "=", hash_secret(plaintext))], limit=1)
        if token and (not client_id or token.client_id.client_id == client_id):
            self.revoke_family(token.family)
            return token
        return self.sudo().browse()

    @private
    @api.model
    def revoke_for_user(self, user):
        """Cut every way ``user`` can reach MCP: tokens, pending auth codes and API keys."""
        self.sudo().search([("user_id", "in", user.ids), ("revoked", "=", False)]).write({"revoked": True})
        self.env["gental.mcp.auth.code"].sudo().search([
            ("user_id", "in", user.ids), ("used", "=", False),
        ]).write({"used": True})
        self.env["gental.mcp.client"].sudo().search([
            ("kind", "=", "api_key"), ("user_id", "in", user.ids), ("state", "=", "active"),
        ]).action_revoke()


class GentalMcpAuthCode(models.Model):
    _name = "gental.mcp.auth.code"
    _description = "Gental MCP Authorization Code"
    _order = "id desc"

    code_hash = fields.Char(required=True, groups="base.group_no_one")
    client_id = fields.Many2one("gental.mcp.client", required=True, ondelete="cascade", index=True)
    user_id = fields.Many2one("res.users", required=True, ondelete="cascade", index=True)
    redirect_uri = fields.Char(required=True)
    code_challenge = fields.Char(required=True)
    resource = fields.Char()
    expires_at = fields.Datetime(required=True)
    used = fields.Boolean()

    PROTECTED_FIELDS = frozenset({
        "user_id", "client_id", "code_hash", "resource", "redirect_uri", "code_challenge",
    })

    def init(self):
        create_unique_index(self.env.cr, "gental_mcp_auth_code_code_hash_uniq", self._table, ["code_hash"])

    @api.model_create_multi
    def create(self, vals_list):
        _check_protected_vals(self, vals_list, self.PROTECTED_FIELDS)
        return super().create(vals_list)

    def write(self, vals):
        _check_protected_vals(self, [vals], self.PROTECTED_FIELDS)
        return super().write(vals)

    @private
    @api.model
    def issue(self, client, user, redirect_uri, code_challenge, resource):
        """Create a single-use code (S256 PKCE challenge) valid for 60 s; returns the plaintext."""
        plaintext = new_secret("gmac")
        self.sudo().create({
            "code_hash": hash_secret(plaintext),
            "client_id": client.id,
            "user_id": user.id,
            "redirect_uri": redirect_uri,
            "code_challenge": code_challenge,
            "resource": resource,
            "expires_at": fields.Datetime.now() + timedelta(seconds=CODE_TTL),
        })
        return plaintext

    @private
    @api.model
    def consume(self, plaintext, client, redirect_uri, verifier):
        """Redeem a code. The code is burnt before any check, so it is single use even on failure."""
        if not plaintext or not plaintext.startswith("gmac_"):
            raise AccessDenied()
        code = self.sudo().search([("code_hash", "=", hash_secret(plaintext))], limit=1)
        if not code or code.used:
            raise AccessDenied()
        code.used = True
        code.flush_recordset(["used"])
        challenge = pkce_s256(verifier) if isinstance(verifier, str) and PKCE_VERIFIER_RE.match(verifier) else ""
        if (
            code.client_id != client
            or client.state != "active"
            or not code.user_id.active
            or code.redirect_uri != redirect_uri
            or code.expires_at <= fields.Datetime.now()
            or not challenge
            or not consteq(challenge, code.code_challenge)
        ):
            raise AccessDenied()
        return code

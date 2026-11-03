import logging
from datetime import timedelta
from urllib.parse import urlsplit

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, ValidationError

from ..compat import check_write_access, create_unique_index, private
from ..core.crypto import hash_secret, new_secret
from .mcp_log import _bookkeeping_env

_logger = logging.getLogger(__name__)

API_KEY_PREFIX = "gmcp"
CLIENT_ID_PREFIX = "gmci"
CLIENT_NAME_MAX = 100
MAX_REDIRECT_URIS = 10
LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}
METADATA_URL_MAX = 2048
TOUCH_EVERY = timedelta(minutes=1)


def is_metadata_url(client_id):
    """True when ``client_id`` is a Client ID Metadata Document URL.

    That is an https URL with a host and a path, no user-info, fragment or dot segments, at most 2048 chars.
    """
    if not isinstance(client_id, str) or not client_id or len(client_id) > METADATA_URL_MAX:
        return False
    try:
        parts = urlsplit(client_id)
        parts.port  # noqa: B018 - raises ValueError on an invalid port
    except ValueError:
        return False
    segments = parts.path.split("/")
    return (
        parts.scheme == "https"
        and bool(parts.hostname)
        and parts.username is None
        and parts.password is None
        and "#" not in client_id
        and parts.path not in ("", "/")
        and not any(seg in (".", "..") for seg in segments)
        and not any(c.isspace() for c in client_id)
    )


def check_redirect_uris(uris):
    """Return ``uris`` if it is a non-empty list of acceptable OAuth redirect URIs, else raise ``ValueError``.

    Each URI is ``https://host/...`` or ``http://`` on a loopback host (RFC 8252), with no fragment and
    no user-info.
    """
    if not isinstance(uris, list) or not uris or len(uris) > MAX_REDIRECT_URIS:
        raise ValueError(f"redirect_uris must be a list of 1 to {MAX_REDIRECT_URIS} URIs")
    for uri in uris:
        if not isinstance(uri, str) or any(c.isspace() for c in uri):
            raise ValueError("redirect_uris must be strings without spaces")
        parts = urlsplit(uri)
        host = parts.hostname
        if parts.fragment or "#" in uri:
            raise ValueError(f"redirect URI has a fragment: {uri}")
        if parts.username is not None or parts.password is not None:
            raise ValueError(f"redirect URI has user info: {uri}")
        parts.port  # noqa: B018 - raises ValueError on an invalid port
        if parts.scheme == "https" and host:
            continue
        if parts.scheme == "http" and host in LOOPBACK_HOSTS:
            continue
        raise ValueError(f"redirect URI must use https, or http on a loopback host: {uri}")
    return uris


class GentalMcpClient(models.Model):
    """An AI assistant allowed to connect: an OAuth client or an API key bound to one user."""

    _name = "gental.mcp.client"
    _description = "Gental MCP Client"
    _order = "id desc"

    name = fields.Char(required=True)
    kind = fields.Selection([("oauth", "OAuth"), ("api_key", "API Key")], required=True, default="oauth")
    user_id = fields.Many2one(
        "res.users", string="User", ondelete="cascade", index=True,
        help="The user an API key acts as. OAuth clients act as the user who approved them.",
    )
    client_id = fields.Char(string="Client ID", index=True, help="OAuth only; a URL for metadata-document clients.")
    redirect_uris = fields.Text(string="Redirect URIs", help="One per line.")
    cimd = fields.Boolean(string="Metadata Document Client")
    registration = fields.Selection(
        [("dcr", "Dynamic Registration"), ("cimd", "Metadata Document"), ("manual", "Manual")], default="manual"
    )
    key_hash = fields.Char(groups="base.group_no_one", index=True, copy=False)
    key_hint = fields.Char(string="Key Ends With", copy=False)
    expires_at = fields.Datetime(string="Expires")
    last_used_at = fields.Datetime(string="Last Used", copy=False)
    state = fields.Selection(
        [("active", "Active"), ("revoked", "Revoked")], required=True, default="active", copy=False, index=True
    )
    revoked_at = fields.Datetime(copy=False)
    cimd_fetched_at = fields.Datetime(string="Metadata Fetched", copy=False)

    def init(self):
        create_unique_index(self.env.cr, "gental_mcp_client_client_id_uniq", self._table, ["client_id"])

    @api.constrains("kind", "user_id")
    def _check_api_key_user(self):
        if any(rec.kind == "api_key" and not rec.user_id for rec in self):
            raise ValidationError(_("An API key must belong to a user."))

    @api.model_create_multi
    def create(self, vals_list):
        self._check_owner_vals(vals_list)
        return super().create(vals_list)

    def write(self, vals):
        self._check_owner_vals([vals])
        return super().write(vals)

    def _may_act_for_others(self):
        """True for code running as superuser and for system administrators."""
        return self.env.su or self.env.user._is_system()

    def _check_owner_vals(self, vals_list):
        """A manager who is not a system administrator may only bind clients to themselves, and never sets a
        key hash directly: otherwise they could mint a key that acts as an administrator."""
        if self._may_act_for_others():
            return
        for vals in vals_list:
            if "key_hash" in vals or vals.get("user_id") not in (None, False, self.env.uid):
                raise AccessError(_("Only an administrator can create an API key for another user."))

    def redirect_uri_list(self):
        self.ensure_one()
        return [u.strip() for u in (self.redirect_uris or "").splitlines() if u.strip()]

    @private
    @api.model
    def create_api_key(self, name, user, expires_at=None):
        """Create an API key client for ``user``. Returns ``(client, plaintext)``; the plaintext is never stored.

        Only a system administrator (or code running as superuser) may create a key for another user.
        """
        if user != self.env.user and not self._may_act_for_others():
            raise AccessError(_("Only an administrator can create an API key for another user."))
        plaintext = new_secret(API_KEY_PREFIX)
        client = self.sudo().create({
            "name": name,
            "kind": "api_key",
            "user_id": user.id,
            "registration": "manual",
            "key_hash": hash_secret(plaintext),
            "key_hint": plaintext[-4:],
            "expires_at": expires_at,
        })
        return client, plaintext

    @private
    @api.model
    def find_by_api_key(self, plaintext):
        """The active, unexpired API key client matching ``plaintext`` (sudo), or an empty recordset."""
        if not plaintext or not plaintext.startswith(API_KEY_PREFIX + "_"):
            return self.sudo().browse()
        client = self.sudo().search([
            ("kind", "=", "api_key"),
            ("key_hash", "=", hash_secret(plaintext)),
            ("state", "=", "active"),
            ("user_id.active", "=", True),
            "|", ("expires_at", "=", False), ("expires_at", ">", fields.Datetime.now()),
        ], limit=1)
        return client

    @private
    @api.model
    def is_known_api_key(self, plaintext):
        """The API key client (sudo) this database issued for ``plaintext``, live or not; else empty (falsy)."""
        if not plaintext or not plaintext.startswith(API_KEY_PREFIX + "_"):
            return self.sudo().browse()
        return self.sudo().search([("kind", "=", "api_key"), ("key_hash", "=", hash_secret(plaintext))], limit=1)

    @private
    def touch_last_used(self):
        """Record ``last_used_at`` at most once a minute, on its own cursor; never breaks the request."""
        self.ensure_one()
        now = fields.Datetime.now()
        if self.last_used_at and self.last_used_at > now - TOUCH_EVERY:
            return
        try:
            with _bookkeeping_env(self.env) as benv:
                benv[self._name].browse(self.id).write({"last_used_at": now})
        except Exception:  # noqa: BLE001 - e.g. a concurrent update; the next call retries
            _logger.info("Could not record last use of MCP client %s", self.id, exc_info=True)

    @private
    @api.model
    def find_oauth_client(self, client_id, include_revoked=False):
        """The OAuth client with this ``client_id`` (sudo), or an empty recordset."""
        domain = [("kind", "=", "oauth"), ("client_id", "=", client_id)]
        if not include_revoked:
            domain.append(("state", "=", "active"))
        return self.sudo().search(domain, limit=1)

    @private
    def may_receive_error_redirects(self):
        """True when an authorization error may be redirected to this client (RFC 9700 section 4.11.2).

        A dynamically registered client that no user has approved yet could be anyone's open redirector,
        so its errors are shown on a page instead; manual and metadata-document clients are trusted.
        """
        self.ensure_one()
        if self.registration != "dcr":
            return True
        return bool(
            self.env["gental.mcp.auth.code"].sudo().search_count([("client_id", "=", self.id)], limit=1)
            or self.env["gental.mcp.token"].sudo().search_count([("client_id", "=", self.id)], limit=1)
        )

    @private
    @api.model
    def register_dynamic(self, name, redirect_uris):
        """Create a public OAuth client by Dynamic Client Registration (RFC 7591).

        Raises ``ValueError`` on invalid redirect URIs or name.
        """
        check_redirect_uris(redirect_uris)
        if not isinstance(name, str) or not name.strip() or len(name) > CLIENT_NAME_MAX:
            raise ValueError("invalid client name")
        return self.sudo().create({
            "name": name.strip(),
            "kind": "oauth",
            "registration": "dcr",
            "client_id": new_secret(CLIENT_ID_PREFIX, 16),
            "redirect_uris": "\n".join(redirect_uris),
        })

    @private
    @api.model
    def upsert_metadata_client(self, client_id, name, redirect_uris):
        """Create or refresh the client described by the metadata document at ``client_id`` (a URL).

        Raises ``ValueError`` unless ``client_id`` is a metadata URL that is new or already a metadata client.
        """
        if not is_metadata_url(client_id):
            raise ValueError("client_id is not a metadata document URL")
        check_redirect_uris(redirect_uris)
        existing = self.sudo().with_context(active_test=False).search([("client_id", "=", client_id)], limit=1)
        if existing and (existing.kind != "oauth" or existing.registration != "cimd"):
            raise ValueError("client_id belongs to a client that is not a metadata document client")
        vals = {
            "name": name,
            "kind": "oauth",
            "registration": "cimd",
            "cimd": True,
            "client_id": client_id,
            "redirect_uris": "\n".join(redirect_uris),
            "cimd_fetched_at": fields.Datetime.now(),
        }
        if existing:
            existing.write(vals)
            return existing
        return self.sudo().create(vals)

    def action_revoke(self):
        """Revoke the clients and every token issued to them (a manager button; needs write access)."""
        check_write_access(self)
        clients = self.sudo().filtered(lambda c: c.state != "revoked")
        clients.write({"state": "revoked", "revoked_at": fields.Datetime.now()})
        self.env["gental.mcp.token"].sudo().search([
            ("client_id", "in", self.ids), ("revoked", "=", False),
        ]).write({"revoked": True})
        self.env["gental.mcp.auth.code"].sudo().search([
            ("client_id", "in", self.ids), ("used", "=", False),
        ]).write({"used": True})
        return True

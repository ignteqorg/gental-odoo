"""Model access (ACLs) of every ``gental.mcp.*`` model, and the record rules of the webhook models, checked on the
installed database.

The source declares them in ``security/ir.model.access.csv`` and ``security/webhook_rules.xml``; 20.0 gets them
through ``build_tree.py``'s ``ir.access.csv`` transform (the rules become the rows' domains). This pins the result,
not the files: the same tables must hold on 17.0-20.0.
"""
from unittest.mock import patch

from odoo import Command
from odoo.exceptions import AccessError
from odoo.tests import tagged

from .common import GentalMcpCase

OPERATIONS = ("read", "write", "create", "unlink")
R, RW, CRUD = {"read"}, {"read", "write"}, set(OPERATIONS)
# Manager rights per model. Everyone else (connect, plain internal, portal, public) gets nothing: the module
# reads its own bookkeeping with sudo, never through the acting user's rights.
MANAGER = {
    "gental.mcp.config": RW,
    "gental.mcp.origin": CRUD,
    "gental.mcp.exposed.model": CRUD,
    "gental.mcp.exposed.method": CRUD,
    "gental.mcp.client": CRUD,
    "gental.mcp.token": R,
    "gental.mcp.auth.code": R,
    "gental.mcp.log": R,
    "gental.mcp.rate": RW,
    "gental.mcp.api.key.wizard": CRUD,
    "gental.mcp.action.key": R,
    "gental.mcp.tool.setting": RW,
    "gental.mcp.identity.secret.wizard": CRUD,
    "gental.mcp.document": R,
    "gental.mcp.webhook.event": R,
    "gental.mcp.webhook.subscription": CRUD,
    "gental.mcp.webhook.delivery": R,
    # Scan bookkeeping: no views, reached only through sudo, and it holds record ids and states as the run-as
    # user sees them, so nobody reads it directly.
    "gental.mcp.webhook.watch": set(),
    "gental.mcp.webhook.snapshot": set(),
}


def record_allows(record, operation):
    """Whether ``record``'s user may apply ``operation`` to it: model access and record rules (``has_access`` on
    18+, else 17.0's ``check_access_rights`` + ``check_access_rule``)."""
    if hasattr(record, "has_access"):
        return record.has_access(operation)
    try:
        record.check_access_rights(operation)
        record.check_access_rule(operation)
    except AccessError:
        return False
    return True


def model_allows(model, operation):
    """The model-level right (ACL) of ``model``'s user for ``operation``: ``has_access`` (18+), else 17.0's
    ``check_access_rights``. On an empty recordset no record rule can refuse."""
    if hasattr(model, "has_access"):
        return model.browse().has_access(operation)
    return model.check_access_rights(operation, raise_exception=False)


@tagged("post_install", "-at_install", "gental_mcp")
class TestAccessMatrix(GentalMcpCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.internal_user = cls._make_user("gmcp_internal", "MCP Internal", ["base.group_user"])
        cls.portal_user = cls._make_user("gmcp_portal_acl", "MCP Portal", ["base.group_portal"])
        cls.users = {
            "manager": cls.manager_user,
            "connect": cls.agent_user,
            "internal": cls.internal_user,
            "portal": cls.portal_user,
            "public": cls.env.ref("base.public_user"),
        }

    def test_every_model_is_in_the_table(self):
        models = {name for name, model in self.env.registry.items()
                  if name.startswith("gental.mcp.") and not model._abstract}
        self.assertEqual(models, set(MANAGER))

    def test_access_matrix(self):
        for role, user in self.users.items():
            for model_name, manager_ops in MANAGER.items():
                model = self.env[model_name].with_user(user)
                expected = manager_ops if role == "manager" else set()
                for operation in OPERATIONS:
                    with self.subTest(role=role, model=model_name, operation=operation):
                        self.assertIs(model_allows(model, operation), operation in expected)

    def test_system_admin_has_manager_rights(self):
        admin = self.env.ref("base.user_admin")
        for model_name, manager_ops in MANAGER.items():
            model = self.env[model_name].with_user(admin)
            for operation in OPERATIONS:
                with self.subTest(model=model_name, operation=operation):
                    self.assertIs(model_allows(model, operation), operation in manager_ops)

    def test_webhook_rows_follow_their_run_as_user(self):
        """Managers reach only the subscriptions (and deliveries) that run as them; administrators reach all."""
        event = self.env.ref("gental_mcp.webhook_event_contact_created")
        with patch("odoo.addons.gental_mcp.services.net_guard.socket.getaddrinfo",
                   return_value=[(2, 1, 6, "", ("93.184.216.34", 443))]):
            subs = self.env["gental.mcp.webhook.subscription"].create([
                {"name": name, "url": "https://hooks.example.com/h", "run_as_user_id": user.id,
                 "event_ids": [Command.set(event.ids)]}
                for name, user in (("own", self.manager_user), ("other", self.agent_user))
            ])
        Delivery = self.env["gental.mcp.webhook.delivery"]
        deliveries = Delivery.browse([Delivery.enqueue(sub, event.code, "res.partner", 1, {}).id for sub in subs])
        admin = self.env.ref("base.user_admin")
        sub_ops = MANAGER["gental.mcp.webhook.subscription"]
        delivery_ops = MANAGER["gental.mcp.webhook.delivery"]
        expected = {
            # role: (subscription ops on [own, other], delivery ops on [own, other])
            "manager": ((sub_ops, set()), (delivery_ops, set())),
            "admin": ((sub_ops, sub_ops), (delivery_ops, delivery_ops)),
            "connect": ((set(), set()), (set(), set())),
            "internal": ((set(), set()), (set(), set())),
        }
        users = {**self.users, "admin": admin}
        for role, (sub_expected, delivery_expected) in expected.items():
            user = users[role]
            for records, per_record in ((subs, sub_expected), (deliveries, delivery_expected)):
                for record, ops in zip(records, per_record):
                    for operation in OPERATIONS:
                        with self.subTest(role=role, record=record, operation=operation):
                            self.assertIs(record_allows(record.with_user(user), operation), operation in ops)
        Sub = self.env["gental.mcp.webhook.subscription"]
        self.assertEqual(Sub.with_user(self.manager_user).search([("id", "in", subs.ids)]), subs[0])
        self.assertEqual(Delivery.with_user(self.manager_user).search([("id", "in", deliveries.ids)]), deliveries[0])
        self.assertEqual(Sub.with_user(admin).search([("id", "in", subs.ids)]), subs)
        self.assertEqual(Delivery.with_user(admin).search([("id", "in", deliveries.ids)]), deliveries)

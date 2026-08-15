import json

from psycopg2 import IntegrityError

from odoo import Command, fields
from odoo.exceptions import AccessError
from odoo.tests import tagged
from odoo.tests.common import TransactionCase


@tagged("post_install", "-at_install")
class TestConnectMcp(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.allowed_partner = cls.env["res.partner"].create(
            {"name": "Allowed Partner", "email": "allowed@example.com"}
        )
        cls.denied_partner = cls.env["res.partner"].create(
            {"name": "Denied Partner", "email": "denied@example.com"}
        )
        partner_model = cls.env["ir.model"]._get("res.partner")
        partner_fields = cls.env["ir.model.fields"].search(
            [
                ("model_id", "=", partner_model.id),
                ("name", "in", ["name", "email"]),
            ]
        )
        cls.name_field = partner_fields.filtered(lambda field: field.name == "name")
        cls.mcp_user = cls.env["res.users"].create(
            {
                "name": "MCP Test User",
                "login": "mcp-test-user@example.com",
                "group_ids": [
                    Command.set(
                        [
                            cls.env.ref("base.group_user").id,
                            cls.env.ref("base.group_partner_manager").id,
                        ]
                    )
                ],
            }
        )
        cls.profile = cls.env["connect.mcp.profile"].create(
            {
                "name": "Test MCP Profile",
                "code": "test_profile",
                "max_records_per_call": 10,
                "max_batch_size": 5,
            }
        )
        cls.policy = cls.env["connect.mcp.model.policy"].create(
            {
                "profile_id": cls.profile.id,
                "model_id": partner_model.id,
                "allow_read": True,
                "allow_create": True,
                "allow_write": True,
                "allow_unlink": False,
                "forced_domain_json": json.dumps(
                    [["id", "=", cls.allowed_partner.id]]
                ),
                "read_field_ids": [Command.set(cls.name_field.ids)],
                "write_field_ids": [Command.set(cls.name_field.ids)],
            }
        )
        cls.access = cls.env["connect.mcp.access"].create(
            {
                "name": "Test user access",
                "profile_id": cls.profile.id,
                "user_id": cls.mcp_user.id,
            }
        )
        cls.token = cls.env["res.users.apikeys"].with_user(cls.mcp_user)._generate(
            "mcp",
            "MCP test key",
            fields.Datetime.add(fields.Datetime.now(), days=1),
        )
        cls.service = cls.env["connect.mcp.service"]

    def _request(self, operation, params=None, request_id="00000000-0000-4000-8000-000000000001"):
        return self.service.execute_request(
            self.access,
            operation,
            params or {},
            request_id,
            remote_ip="127.0.0.1",
            user_agent="odoo-test",
        )

    def _approve(self, approval_id):
        approval = self.env["connect.mcp.approval"].search(
            [("request_uid", "=", approval_id)]
        )
        approval.action_approve()
        return approval

    def test_mcp_api_key_and_access_resolution(self):
        global_token = self.env["res.users.apikeys"].with_user(self.mcp_user)._generate(
            None,
            "Global test key",
            fields.Datetime.add(fields.Datetime.now(), days=1),
        )
        user_id = self.env["res.users.apikeys"]._check_mcp_credentials(self.token)
        global_user_id = self.env["res.users.apikeys"]._check_mcp_credentials(global_token)
        rpc_user_id = self.env["res.users.apikeys"]._check_credentials(
            scope="rpc",
            key=self.token,
        )
        access, error = self.env["connect.mcp.access"]._for_user(self.mcp_user)

        self.assertEqual(user_id, self.mcp_user.id)
        self.assertFalse(global_user_id)
        self.assertFalse(rpc_user_id)
        self.assertFalse(error)
        self.assertEqual(access, self.access)

    def test_event_ticket_is_hashed_and_bound_to_access(self):
        Ticket = self.env["connect.mcp.event.ticket"]
        token = Ticket._issue(self.access)
        ticket = Ticket.sudo().search(
            [("token_hash", "=", Ticket._digest(token))],
            limit=1,
        )
        expires_at = ticket.expires_at

        self.assertTrue(ticket)
        self.assertNotEqual(ticket.token_hash, token)
        self.assertEqual(Ticket._check(token).access_id, self.access)
        ticket.invalidate_recordset(["expires_at"])
        self.assertEqual(ticket.expires_at, expires_at)
        self.assertFalse(Ticket._check("invalid-ticket"))

    def test_resource_update_uses_private_channel_and_version(self):
        previous_version = self.access.event_version
        version = self.access._publish_resource_update(
            "odoo://approval/00000000-0000-4000-8000-000000000001"
        )
        messages = self.env.cr.precommit.data["bus.bus.values"]
        wire_message = json.loads(messages[-1]["message"])

        self.assertEqual(version, previous_version + 1)
        self.assertEqual(
            json.loads(messages[-1]["channel"]),
            [self.env.cr.dbname, self.access.event_channel],
        )
        self.assertEqual(wire_message["type"], "connect_mcp_resource_updated")
        self.assertEqual(wire_message["payload"]["version"], version)
        self.assertEqual(
            wire_message["payload"]["uri"],
            "odoo://approval/00000000-0000-4000-8000-000000000001",
        )

    def test_one_access_assignment_per_user(self):
        with self.env.cr.savepoint(), self.assertRaises(IntegrityError):
            self.env["connect.mcp.access"].create(
                {
                    "name": "Duplicate access",
                    "profile_id": self.profile.id,
                    "user_id": self.mcp_user.id,
                }
            )

    def test_forced_domain_and_field_policy(self):
        body, status = self._request(
            "records.search",
            {
                "model": "res.partner",
                "domain": [],
                "fields": ["name"],
                "limit": 10,
            },
        )
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertEqual(
            [record["id"] for record in body["data"]["records"]],
            [self.allowed_partner.id],
        )

        body, status = self._request(
            "records.search",
            {
                "model": "res.partner",
                "domain": [["id", "=", self.denied_partner.id]],
                "fields": ["name"],
                "limit": 10,
            },
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["data"]["records"], [])

        body, status = self._request(
            "records.read",
            {
                "model": "res.partner",
                "ids": [self.allowed_partner.id],
                "fields": ["email"],
            },
        )
        self.assertEqual(status, 403)
        self.assertEqual(body["error"]["code"], "field_denied")

    def test_preview_approval_and_exactly_once_execution(self):
        body, status = self._request(
            "changes.preview",
            {
                "action": "record.update",
                "payload": {
                    "model": "res.partner",
                    "ids": [self.allowed_partner.id],
                    "values": {"name": "Changed by MCP"},
                },
                "idempotency_key": "update-partner-0001",
            },
        )
        self.assertEqual(status, 200)
        approval_id = body["data"]["approval_id"]
        self.assertEqual(body["data"]["state"], "pending")

        body, status = self._request(
            "changes.execute",
            {"approval_id": approval_id},
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["data"]["state"], "pending")
        self.assertEqual(self.allowed_partner.name, "Allowed Partner")

        approval = self.env["connect.mcp.approval"].search(
            [("request_uid", "=", approval_id)]
        )
        approval.action_approve()
        body, status = self._request(
            "changes.execute",
            {"approval_id": approval_id},
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["data"]["state"], "executed")
        self.assertEqual(self.allowed_partner.name, "Changed by MCP")

        first_result = body["data"]["result"]
        body, status = self._request(
            "changes.execute",
            {"approval_id": approval_id},
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["data"]["result"], first_result)
        self.assertEqual(approval.state, "executed")

    def test_idempotency_conflict(self):
        common = {
            "action": "record.update",
            "idempotency_key": "update-partner-conflict",
        }
        body, status = self._request(
            "changes.preview",
            {
                **common,
                "payload": {
                    "model": "res.partner",
                    "ids": [self.allowed_partner.id],
                    "values": {"name": "First"},
                },
            },
        )
        self.assertEqual(status, 200)
        first_id = body["data"]["approval_id"]

        body, status = self._request(
            "changes.preview",
            {
                **common,
                "payload": {
                    "model": "res.partner",
                    "ids": [self.allowed_partner.id],
                    "values": {"name": "Second"},
                },
            },
        )
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "idempotency_conflict")

        body, status = self._request(
            "changes.preview",
            {
                **common,
                "payload": {
                    "model": "res.partner",
                    "ids": [self.allowed_partner.id],
                    "values": {"name": "First"},
                },
            },
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["data"]["approval_id"], first_id)

    def test_audit_is_redacted_and_immutable(self):
        self._request(
            "records.count",
            {"model": "res.partner", "domain": []},
            request_id="00000000-0000-4000-8000-000000000099",
        )
        audit = self.env["connect.mcp.audit.log"].search(
            [("request_id", "=", "00000000-0000-4000-8000-000000000099")]
        )
        self.assertTrue(audit)
        self.assertNotIn(self.token, audit.input_summary or "")
        with self.assertRaises(AccessError):
            audit.write({"duration_ms": 0})
        with self.assertRaises(AccessError):
            audit.unlink()

    def test_inactive_access_is_rejected(self):
        self.access.active = False
        access, error = self.env["connect.mcp.access"]._for_user(self.mcp_user)
        self.assertFalse(access)
        self.assertEqual(error, "inactive_mcp_access")
        self.access.active = True

    def test_expired_mcp_api_key(self):
        token = self.env["res.users.apikeys"].with_user(self.mcp_user)._generate(
            "mcp",
            "Expired MCP test key",
            fields.Datetime.add(fields.Datetime.now(), days=1),
        )
        expired_at = fields.Datetime.subtract(fields.Datetime.now(), seconds=1)
        self.env.cr.execute(
            "UPDATE res_users_apikeys SET expiration_date = %s WHERE index = %s",
            [expired_at, token[:8]],
        )
        user_id = self.env["res.users.apikeys"]._check_mcp_credentials(token)
        self.assertFalse(user_id)

    def test_rate_limit_is_enforced_per_access(self):
        self.profile.rate_limit_per_minute = 1
        first, first_status = self._request("system.info")
        second, second_status = self._request("system.info")

        self.assertEqual(first_status, 200)
        self.assertTrue(first["ok"])
        self.assertEqual(second_status, 429)
        self.assertEqual(second["error"]["code"], "rate_limit_exceeded")

    def test_create_cannot_escape_forced_domain(self):
        body, status = self._request(
            "changes.preview",
            {
                "action": "record.create",
                "payload": {
                    "model": "res.partner",
                    "values": {"name": "Outside MCP scope"},
                },
                "idempotency_key": "create-outside-scope",
            },
        )
        self.assertEqual(status, 200)
        approval = self._approve(body["data"]["approval_id"])

        body, status = self._request(
            "changes.execute",
            {"approval_id": approval.request_uid},
        )

        self.assertEqual(status, 403)
        self.assertEqual(body["error"]["code"], "policy_postcondition_failed")
        self.assertFalse(
            self.env["res.partner"].search([("name", "=", "Outside MCP scope")])
        )

    def test_update_cannot_move_record_outside_forced_domain(self):
        self.policy.forced_domain_json = json.dumps([["name", "ilike", "Allowed"]])
        body, status = self._request(
            "changes.preview",
            {
                "action": "record.update",
                "payload": {
                    "model": "res.partner",
                    "ids": [self.allowed_partner.id],
                    "values": {"name": "Escaped scope"},
                },
                "idempotency_key": "update-outside-scope",
            },
        )
        self.assertEqual(status, 200)
        approval = self._approve(body["data"]["approval_id"])

        body, status = self._request(
            "changes.execute",
            {"approval_id": approval.request_uid},
        )

        self.assertEqual(status, 403)
        self.assertEqual(body["error"]["code"], "policy_postcondition_failed")
        self.assertEqual(self.allowed_partner.name, "Allowed Partner")

    def test_method_policy_rejects_unapproved_argument_shapes(self):
        self.env["connect.mcp.method.policy"].create(
            {
                "profile_id": self.profile.id,
                "model_id": self.policy.model_id.id,
                "method_name": "toggle_active",
                "max_record_count": 1,
            }
        )
        denied, denied_status = self._request(
            "changes.preview",
            {
                "action": "method.call",
                "payload": {
                    "model": "res.partner",
                    "ids": [self.allowed_partner.id],
                    "method": "toggle_active",
                    "args": ["unexpected"],
                },
                "idempotency_key": "method-denied-args",
            },
        )
        allowed, allowed_status = self._request(
            "changes.preview",
            {
                "action": "method.call",
                "payload": {
                    "model": "res.partner",
                    "ids": [self.allowed_partner.id],
                    "method": "toggle_active",
                },
                "idempotency_key": "method-allowed-noargs",
            },
        )

        self.assertEqual(denied_status, 403)
        self.assertEqual(denied["error"]["code"], "policy_denied")
        self.assertEqual(allowed_status, 200)
        self.assertEqual(allowed["data"]["state"], "pending")

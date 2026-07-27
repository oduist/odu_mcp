import json

from odoo import Command, fields
from odoo.exceptions import AccessError
from odoo.tests import tagged
from odoo.tests.common import TransactionCase


@tagged("post_install", "-at_install")
class TestMcpControl(TransactionCase):
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
        cls.profile = cls.env["odoo.mcp.profile"].create(
            {
                "name": "Test MCP Profile",
                "code": "test_profile",
                "max_records_per_call": 10,
                "max_batch_size": 5,
            }
        )
        cls.policy = cls.env["odoo.mcp.model.policy"].create(
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
        cls.credential = cls.env["odoo.mcp.credential"].create(
            {
                "name": "Test connector",
                "profile_id": cls.profile.id,
                "user_id": cls.env.ref("base.user_admin").id,
            }
        )
        cls.token = cls.credential._generate_secret()
        cls.service = cls.env["odoo.mcp.service"]

    def _request(self, operation, params=None, request_id="00000000-0000-4000-8000-000000000001"):
        return self.service.execute_request(
            self.credential,
            operation,
            params or {},
            request_id,
            remote_ip="127.0.0.1",
            user_agent="odoo-test",
        )

    def _approve(self, approval_id):
        approval = self.env["odoo.mcp.approval"].search(
            [("request_uid", "=", approval_id)]
        )
        approval.action_approve()
        return approval

    def test_credential_authentication_and_ip_allowlist(self):
        credential, error = self.env["odoo.mcp.credential"]._authenticate(
            self.token,
            "127.0.0.1",
        )
        self.assertFalse(error)
        self.assertEqual(credential, self.credential)

        invalid, error = self.env["odoo.mcp.credential"]._authenticate(
            f"{self.token}x",
            "127.0.0.1",
        )
        self.assertFalse(invalid)
        self.assertEqual(error, "invalid_credential")

        self.credential.allowed_ip_networks = "10.0.0.0/8"
        allowed, error = self.env["odoo.mcp.credential"]._authenticate(
            self.token,
            "10.10.20.30",
        )
        self.assertEqual(allowed, self.credential)
        self.assertFalse(error)
        denied, error = self.env["odoo.mcp.credential"]._authenticate(
            self.token,
            "192.0.2.1",
        )
        self.assertFalse(denied)
        self.assertEqual(error, "ip_not_allowed")
        self.credential.allowed_ip_networks = False

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

        approval = self.env["odoo.mcp.approval"].search(
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
        audit = self.env["odoo.mcp.audit.log"].search(
            [("request_id", "=", "00000000-0000-4000-8000-000000000099")]
        )
        self.assertTrue(audit)
        self.assertNotIn(self.token, audit.input_summary or "")
        with self.assertRaises(AccessError):
            audit.write({"duration_ms": 0})
        with self.assertRaises(AccessError):
            audit.unlink()

    def test_revoke_secret(self):
        self.credential.action_revoke()
        credential, error = self.env["odoo.mcp.credential"]._authenticate(
            self.token,
            "127.0.0.1",
        )
        self.assertFalse(credential)
        self.assertEqual(error, "invalid_credential")

    def test_expired_credential(self):
        expired_at = fields.Datetime.subtract(fields.Datetime.now(), seconds=1)
        self.env.cr.execute(
            "UPDATE odoo_mcp_credential SET expires_at = %s WHERE id = %s",
            [expired_at, self.credential.id],
        )
        self.credential.invalidate_recordset(["expires_at"])

        credential, error = self.env["odoo.mcp.credential"]._authenticate(
            self.token,
            "127.0.0.1",
        )

        self.assertFalse(credential)
        self.assertEqual(error, "expired_credential")

    def test_rate_limit_is_enforced_per_credential(self):
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
        self.env["odoo.mcp.method.policy"].create(
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

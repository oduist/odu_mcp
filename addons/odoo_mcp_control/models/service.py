import base64
import binascii
import hashlib
import json
import logging
import re
import time
from datetime import date, datetime

from markupsafe import Markup, escape

from odoo import _, api, fields, models, release
from odoo.exceptions import AccessError, MissingError, UserError, ValidationError
from odoo.fields import Domain


_logger = logging.getLogger(__name__)

MODEL_NAME_RE = re.compile(r"^[a-zA-Z0-9_.]+$")
ORDER_PART_RE = re.compile(r"^([a-zA-Z0-9_]+)(?:\s+(asc|desc))?$", re.IGNORECASE)
SENSITIVE_NAME_RE = re.compile(
    r"(password|passwd|secret|token|api.?key|private|credential|authorization)",
    re.IGNORECASE,
)
AGGREGATORS = {"sum", "avg", "min", "max", "count", "count_distinct"}
WRITE_MAGIC_FIELDS = {
    "id",
    "create_uid",
    "create_date",
    "write_uid",
    "write_date",
    "__last_update",
    "display_name",
}


class McpServiceError(Exception):
    def __init__(self, code, message, *, status=400, retryable=False):
        super().__init__(message)
        self.code = code
        self.message = str(message)
        self.status = status
        self.retryable = retryable


class OdooMcpService(models.AbstractModel):
    _name = "odoo.mcp.service"
    _description = "MCP Connector Service"

    @api.model
    def execute_request(
        self,
        credential,
        operation,
        params,
        request_id,
        *,
        remote_ip="",
        user_agent="",
    ):
        started = time.monotonic()
        params = params if isinstance(params, dict) else {}
        input_json = self._canonical_json({"operation": operation, "params": params})
        input_hash = hashlib.sha256(input_json.encode()).hexdigest()
        status = 200
        error_code = False
        error_class = False
        outcome = "success"
        approval = self.env["odoo.mcp.approval"]

        try:
            quota_error = credential._check_quota()
            if quota_error:
                raise McpServiceError(
                    quota_error,
                    _("The connector request quota has been exceeded."),
                    status=429,
                    retryable=True,
                )
            with self.env.cr.savepoint():
                data = self._dispatch(credential, operation, params)
            if isinstance(data, dict) and data.get("approval_id"):
                approval = (
                    self.env["odoo.mcp.approval"]
                    .sudo()
                    .search(
                        [
                            ("credential_id", "=", credential.id),
                            ("request_uid", "=", data["approval_id"]),
                        ],
                        limit=1,
                    )
                )
            body = {
                "ok": True,
                "request_id": request_id,
                "data": self._json_safe(data),
                "meta": {
                    "profile": credential.profile_id.code,
                    "duration_ms": int((time.monotonic() - started) * 1000),
                },
            }
        except McpServiceError as exc:
            status = exc.status
            error_code = exc.code
            error_class = type(exc).__name__
            outcome = "denied" if status in {401, 403, 429} else "error"
            body = self._error_body(request_id, exc.code, exc.message, exc.retryable)
        except (AccessError, MissingError) as exc:
            status = 403
            error_code = "access_denied"
            error_class = type(exc).__name__
            outcome = "denied"
            body = self._error_body(
                request_id,
                error_code,
                _("Access was denied by Odoo security or the MCP policy."),
                False,
            )
        except (ValidationError, UserError, ValueError, TypeError) as exc:
            status = 422
            error_code = "validation_error"
            error_class = type(exc).__name__
            outcome = "error"
            body = self._error_body(request_id, error_code, str(exc), False)
        except Exception as exc:  # noqa: BLE001 - protocol boundary
            status = 500
            error_code = "internal_error"
            error_class = type(exc).__name__
            outcome = "error"
            _logger.exception("Unhandled MCP connector error request_id=%s", request_id)
            body = self._error_body(
                request_id,
                error_code,
                _("The request could not be completed."),
                True,
            )

        duration_ms = int((time.monotonic() - started) * 1000)
        body.setdefault("meta", {})["duration_ms"] = duration_ms
        model_name = params.get("model") if isinstance(params.get("model"), str) else False
        target_ids = self._safe_target_ids(params.get("ids") or params.get("id"))
        self._audit(
            credential,
            request_id=request_id,
            operation=operation or "invalid",
            model_name=model_name,
            input_hash=input_hash,
            input_summary=self._input_summary(operation, params),
            target_ids=target_ids,
            outcome=outcome,
            status_code=status,
            error_code=error_code,
            error_class=error_class,
            duration_ms=duration_ms,
            remote_ip=remote_ip,
            user_agent=user_agent,
            approval=approval,
        )
        credential._record_use(status < 400)
        return body, status

    @api.model
    def _dispatch(self, credential, operation, params):
        handlers = {
            "capabilities": self._op_capabilities,
            "system.info": self._op_system_info,
            "identity.whoami": self._op_whoami,
            "models.list": self._op_models_list,
            "models.describe": self._op_models_describe,
            "records.search": self._op_records_search,
            "records.read": self._op_records_read,
            "records.count": self._op_records_count,
            "records.aggregate": self._op_records_aggregate,
            "attachments.read": self._op_attachment_read,
            "reports.render": self._op_report_render,
            "changes.preview": self._op_change_preview,
            "changes.status": self._op_change_status,
            "changes.execute": self._op_change_execute,
        }
        handler = handlers.get(operation)
        if not handler:
            raise McpServiceError("unknown_operation", _("Unknown connector operation."), status=404)
        return handler(credential, params)

    @api.model
    def _op_capabilities(self, credential, params):
        return credential.profile_id._capabilities()

    @api.model
    def _op_system_info(self, credential, params):
        user_env = self._business_env(credential)
        user = user_env.user
        return {
            "odoo_version": release.version,
            "module_version": "19.0.1.0.0",
            "profile": credential.profile_id.code,
            "user": {"id": user.id, "name": user.name, "login": user.login},
            "companies": [
                {"id": company.id, "name": company.name}
                for company in user_env["res.company"].browse(user_env.context["allowed_company_ids"])
            ],
            "capabilities": credential.profile_id._capabilities()["features"],
        }

    @api.model
    def _op_whoami(self, credential, params):
        user_env = self._business_env(credential)
        user = user_env.user
        return {
            "id": user.id,
            "name": user.name,
            "login": user.login,
            "language": user.lang,
            "timezone": user.tz,
            "company": {"id": user_env.company.id, "name": user_env.company.name},
            "allowed_companies": [
                {"id": company.id, "name": company.name}
                for company in user_env["res.company"].browse(user_env.context["allowed_company_ids"])
            ],
            "profile": credential.profile_id.code,
        }

    @api.model
    def _op_models_list(self, credential, params):
        user_env = self._business_env(credential)
        result = []
        for policy in credential.profile_id.policy_ids.filtered("active").sorted(
            key=lambda item: item.model_id.model
        ):
            model_name = policy.model_id.model
            if model_name not in user_env:
                continue
            Model = user_env[model_name]
            operations = [
                operation
                for operation in policy._operation_names()
                if self._has_model_access(Model, operation)
            ]
            if operations:
                result.append(
                    {
                        "model": model_name,
                        "name": policy.model_id.name,
                        "operations": operations,
                        "max_records": policy._record_limit(),
                    }
                )
        return {"models": result, "count": len(result)}

    @api.model
    def _op_models_describe(self, credential, params):
        if not credential.profile_id.allow_schema:
            raise McpServiceError("policy_denied", _("Schema access is disabled."), status=403)
        model_name = self._model_name(params)
        Model, policy = self._model_policy(credential, model_name, "read")
        allowed = policy._allowed_field_names("read", Model)
        descriptions = Model.fields_get(
            allfields=sorted(allowed),
            attributes=[
                "type",
                "string",
                "help",
                "required",
                "readonly",
                "relation",
                "selection",
                "store",
            ],
        )
        writable = policy._allowed_field_names("write", Model) if policy.allow_write else set()
        for name, description in descriptions.items():
            description["mcp_writable"] = name in writable
        return {
            "model": model_name,
            "name": policy.model_id.name,
            "operations": policy._operation_names(),
            "fields": descriptions,
        }

    @api.model
    def _op_records_search(self, credential, params):
        model_name = self._model_name(params)
        Model, policy = self._model_policy(credential, model_name, "read")
        fields_list = self._read_fields(policy, Model, params.get("fields"))
        domain = self._combined_domain(policy, params.get("domain", []))
        limit = self._limit(policy, params.get("limit"))
        offset = self._offset(params.get("offset"))
        order = self._parse_order(Model, policy, params.get("order"))
        records = Model.search(domain, offset=offset, limit=limit + 1, order=order)
        has_more = len(records) > limit
        records = records[:limit]
        return {
            "model": model_name,
            "records": records.read(fields_list),
            "page": {
                "offset": offset,
                "limit": limit,
                "returned": len(records),
                "has_more": has_more,
            },
        }

    @api.model
    def _op_records_read(self, credential, params):
        model_name = self._model_name(params)
        Model, policy = self._model_policy(credential, model_name, "read")
        ids = self._parse_ids(params.get("ids"), max_count=policy._record_limit())
        fields_list = self._read_fields(policy, Model, params.get("fields"))
        records = self._records_in_policy(Model, policy, ids, "read")
        return {"model": model_name, "records": records.read(fields_list)}

    @api.model
    def _op_records_count(self, credential, params):
        model_name = self._model_name(params)
        Model, policy = self._model_policy(credential, model_name, "read")
        domain = self._combined_domain(policy, params.get("domain", []))
        return {"model": model_name, "count": Model.search_count(domain)}

    @api.model
    def _op_records_aggregate(self, credential, params):
        model_name = self._model_name(params)
        Model, policy = self._model_policy(credential, model_name, "aggregate")
        if not credential.profile_id.allow_aggregate:
            raise McpServiceError("policy_denied", _("Aggregation is disabled."), status=403)
        fields_list = params.get("fields") or []
        groupby = params.get("groupby") or []
        if not isinstance(fields_list, list) or not isinstance(groupby, list):
            raise McpServiceError("invalid_aggregate", _("Aggregate fields and groupby must be lists."))
        readable = policy._allowed_field_names("read", Model)
        for item in fields_list:
            if not isinstance(item, str):
                raise McpServiceError("invalid_aggregate", _("Invalid aggregate field."))
            parts = item.split(":", 1)
            name = parts[0]
            aggregator = parts[1] if len(parts) == 2 else None
            if name not in readable or (aggregator and aggregator not in AGGREGATORS):
                raise McpServiceError("policy_denied", _("Aggregate field is not allowed."), status=403)
            field = Model._fields.get(name)
            if not field or not field.store:
                raise McpServiceError("invalid_aggregate", _("Aggregate fields must be stored."))
        for item in groupby:
            name = item.split(":", 1)[0] if isinstance(item, str) else ""
            if name not in readable or name not in Model._fields:
                raise McpServiceError("policy_denied", _("Group-by field is not allowed."), status=403)
        limit = self._limit(policy, params.get("limit"))
        domain = self._combined_domain(policy, params.get("domain", []))
        rows = Model.read_group(
            domain,
            fields_list,
            groupby,
            limit=limit,
            lazy=False,
        )
        return {"model": model_name, "groups": rows, "limit": limit}

    @api.model
    def _op_attachment_read(self, credential, params):
        profile = credential.profile_id
        if not profile.allow_attachments:
            raise McpServiceError("policy_denied", _("Attachment access is disabled."), status=403)
        attachment_id = self._positive_int(params.get("attachment_id"), "attachment_id")
        user_env = self._business_env(credential)
        attachment = user_env["ir.attachment"].browse(attachment_id)
        attachment.check_access("read")
        if not attachment.exists() or not attachment.res_model or not attachment.res_id:
            raise McpServiceError("not_found", _("Attachment was not found."), status=404)
        Model, policy = self._model_policy(credential, attachment.res_model, "read")
        self._records_in_policy(Model, policy, [attachment.res_id], "read")
        data = attachment.datas or b""
        if isinstance(data, bytes):
            encoded = data.decode()
        else:
            encoded = data
        size = self._decoded_size(encoded)
        if size > self._max_binary_bytes():
            raise McpServiceError("response_too_large", _("Attachment exceeds the configured limit."), status=413)
        return {
            "attachment_id": attachment.id,
            "name": attachment.name,
            "mimetype": attachment.mimetype,
            "size": size,
            "content_base64": encoded,
        }

    @api.model
    def _op_report_render(self, credential, params):
        if not credential.profile_id.allow_reports:
            raise McpServiceError("policy_denied", _("Report rendering is disabled."), status=403)
        report_ref = params.get("report")
        if not isinstance(report_ref, str) or "." not in report_ref:
            raise McpServiceError("invalid_report", _("A report XML ID is required."))
        user_env = self._business_env(credential)
        report = user_env.ref(report_ref, raise_if_not_found=False)
        if not report or report._name != "ir.actions.report":
            raise McpServiceError("not_found", _("Report was not found."), status=404)
        Model, policy = self._model_policy(credential, report.model, "read")
        ids = self._parse_ids(params.get("ids"), max_count=min(20, policy._record_limit()))
        records = self._records_in_policy(Model, policy, ids, "read")
        content, content_type = user_env["ir.actions.report"]._render_qweb_pdf(
            report,
            res_ids=records.ids,
        )
        if len(content) > self._max_binary_bytes():
            raise McpServiceError("response_too_large", _("Rendered report exceeds the configured limit."), status=413)
        return {
            "report": report_ref,
            "content_type": content_type,
            "size": len(content),
            "content_base64": base64.b64encode(content).decode(),
        }

    @api.model
    def _op_change_preview(self, credential, params):
        action = params.get("action")
        payload = params.get("payload")
        idempotency_key = params.get("idempotency_key")
        if not isinstance(payload, dict):
            raise McpServiceError("invalid_payload", _("Change payload must be an object."))
        if not isinstance(idempotency_key, str) or not (8 <= len(idempotency_key) <= 128):
            raise McpServiceError(
                "invalid_idempotency_key",
                _("An idempotency key between 8 and 128 characters is required."),
            )
        normalized, preview = self._prepare_action(credential, action, payload)
        payload_json = self.env["odoo.mcp.approval"]._canonical_payload(normalized)
        payload_hash = self.env["odoo.mcp.approval"]._payload_digest(payload_json)
        Approval = self.env["odoo.mcp.approval"].sudo()
        existing = Approval.search(
            [
                ("credential_id", "=", credential.id),
                ("idempotency_key", "=", idempotency_key),
            ],
            limit=1,
        )
        if existing:
            if existing.payload_hash != payload_hash:
                raise McpServiceError(
                    "idempotency_conflict",
                    _("The idempotency key is already bound to another change plan."),
                    status=409,
                )
            return existing._public_dict()

        risk = preview["risk_level"]
        auto_approved = (
            credential.profile_id.auto_approve_low_risk
            and risk == "low"
            and action in {"message.post", "activity.schedule"}
        )
        now = fields.Datetime.now()
        approval = Approval.create(
            {
                "credential_id": credential.id,
                "action": action,
                "model_name": normalized["model"],
                "payload_json": payload_json,
                "payload_hash": payload_hash,
                "idempotency_key": idempotency_key,
                "risk_level": risk,
                "summary": preview["summary"],
                "target_count": preview["target_count"],
                "diff_json": self._canonical_json(preview["diff"]),
                "state": "approved" if auto_approved else "pending",
                "expires_at": fields.Datetime.add(
                    now,
                    minutes=credential.profile_id.approval_ttl_minutes,
                ),
                "approved_at": now if auto_approved else False,
            }
        )
        return approval._public_dict()

    @api.model
    def _op_change_status(self, credential, params):
        approval = self._approval_for_credential(credential, params.get("approval_id"))
        if approval.state in {"pending", "approved"} and approval.expires_at <= fields.Datetime.now():
            approval._system_write({"state": "expired"})
        return approval._public_dict()

    @api.model
    def _op_change_execute(self, credential, params):
        approval = self._approval_for_credential(credential, params.get("approval_id"))
        approval.lock_for_update()
        if approval.state == "executed":
            return approval._public_dict()
        if approval.expires_at <= fields.Datetime.now():
            approval._system_write({"state": "expired"})
            return approval._public_dict()
        if approval.state != "approved":
            return approval._public_dict()
        payload_json = approval.payload_json
        if approval.payload_hash != approval._payload_digest(payload_json):
            approval._system_write(
                {"state": "failed", "error_message": _("Stored change plan integrity check failed.")}
            )
            raise McpServiceError("integrity_error", _("Change plan integrity check failed."), status=409)
        approval._system_write({"state": "executing"})
        try:
            with self.env.cr.savepoint():
                payload = json.loads(payload_json)
                result = self._execute_action(credential, approval.action, payload)
            approval._system_write(
                {
                    "state": "executed",
                    "executed_at": fields.Datetime.now(),
                    "result_json": approval._json_result(self._json_safe(result)),
                    "error_message": False,
                }
            )
        except Exception as exc:
            approval._system_write({"state": "failed", "error_message": str(exc)[:1000]})
            raise
        return approval._public_dict()

    @api.model
    def _prepare_action(self, credential, action, payload):
        handlers = {
            "record.create": self._prepare_create,
            "record.update": self._prepare_update,
            "record.delete": self._prepare_delete,
            "message.post": self._prepare_message,
            "activity.schedule": self._prepare_activity,
            "attachment.create": self._prepare_attachment,
            "method.call": self._prepare_method,
        }
        handler = handlers.get(action)
        if not handler:
            raise McpServiceError("unknown_action", _("Unknown change action."), status=404)
        return handler(credential, payload)

    @api.model
    def _prepare_create(self, credential, payload):
        model_name = self._model_name(payload)
        Model, policy = self._model_policy(credential, model_name, "create")
        values_list = payload.get("values")
        values_list = values_list if isinstance(values_list, list) else [values_list]
        if not values_list or not all(isinstance(values, dict) for values in values_list):
            raise McpServiceError("invalid_values", _("Create values must be an object or list of objects."))
        if len(values_list) > credential.profile_id.max_batch_size:
            raise McpServiceError("batch_too_large", _("Create batch exceeds the profile limit."), status=413)
        Model.browse().check_access("create")
        normalized_values = [
            self._write_values(policy, Model, values, "create")
            for values in values_list
        ]
        normalized = {"model": model_name, "values": normalized_values}
        return normalized, {
            "risk_level": "medium",
            "summary": _("Create %(count)s record(s) in %(model)s", count=len(values_list), model=model_name),
            "target_count": len(values_list),
            "diff": [
                {"record": index + 1, "fields": sorted(values)}
                for index, values in enumerate(normalized_values)
            ],
        }

    @api.model
    def _prepare_update(self, credential, payload):
        model_name = self._model_name(payload)
        Model, policy = self._model_policy(credential, model_name, "write")
        ids = self._parse_ids(payload.get("ids"), max_count=credential.profile_id.max_batch_size)
        values = self._write_values(policy, Model, payload.get("values"), "write")
        records = self._records_in_policy(Model, policy, ids, "write")
        old_rows = {row["id"]: row for row in records.read(list(values))}
        diff = []
        for record in records:
            changes = {}
            for name, new_value in values.items():
                changes[name] = {
                    "old": self._redact_value(name, old_rows[record.id].get(name)),
                    "new": self._redact_value(name, new_value),
                }
            diff.append({"id": record.id, "display_name": record.display_name, "changes": changes})
        normalized = {"model": model_name, "ids": records.ids, "values": values}
        return normalized, {
            "risk_level": "medium",
            "summary": _("Update %(count)s record(s) in %(model)s", count=len(records), model=model_name),
            "target_count": len(records),
            "diff": diff,
        }

    @api.model
    def _prepare_delete(self, credential, payload):
        model_name = self._model_name(payload)
        Model, policy = self._model_policy(credential, model_name, "unlink")
        ids = self._parse_ids(payload.get("ids"), max_count=credential.profile_id.max_batch_size)
        records = self._records_in_policy(Model, policy, ids, "unlink")
        normalized = {"model": model_name, "ids": records.ids}
        return normalized, {
            "risk_level": "high",
            "summary": _("Delete %(count)s record(s) from %(model)s", count=len(records), model=model_name),
            "target_count": len(records),
            "diff": [{"id": record.id, "display_name": record.display_name} for record in records],
        }

    @api.model
    def _prepare_message(self, credential, payload):
        if not credential.profile_id.allow_chatter:
            raise McpServiceError("policy_denied", _("Chatter actions are disabled."), status=403)
        model_name = self._model_name(payload)
        Model, policy = self._model_policy(credential, model_name, "write")
        record_id = self._positive_int(payload.get("id"), "id")
        record = self._records_in_policy(Model, policy, [record_id], "write")
        if not hasattr(record, "message_post"):
            raise McpServiceError("unsupported_model", _("The model does not support chatter."))
        body = payload.get("body")
        if not isinstance(body, str) or not body.strip() or len(body) > 20000:
            raise McpServiceError("invalid_message", _("Message body is empty or too long."))
        normalized = {"model": model_name, "id": record.id, "body": body}
        return normalized, {
            "risk_level": "low",
            "summary": _("Post a message on %(model)s #%(id)s", model=model_name, id=record.id),
            "target_count": 1,
            "diff": [{"id": record.id, "body_preview": body[:200]}],
        }

    @api.model
    def _prepare_activity(self, credential, payload):
        if not credential.profile_id.allow_activities:
            raise McpServiceError("policy_denied", _("Activity actions are disabled."), status=403)
        model_name = self._model_name(payload)
        Model, policy = self._model_policy(credential, model_name, "write")
        record_id = self._positive_int(payload.get("id"), "id")
        record = self._records_in_policy(Model, policy, [record_id], "write")
        if not hasattr(record, "activity_schedule"):
            raise McpServiceError("unsupported_model", _("The model does not support activities."))
        activity_type = payload.get("activity_type", "mail.mail_activity_data_todo")
        summary = payload.get("summary", "")
        note = payload.get("note", "")
        if not isinstance(activity_type, str) or "." not in activity_type:
            raise McpServiceError("invalid_activity", _("Activity type must be an XML ID."))
        if not isinstance(summary, str) or not isinstance(note, str):
            raise McpServiceError("invalid_activity", _("Activity summary and note must be strings."))
        normalized = {
            "model": model_name,
            "id": record.id,
            "activity_type": activity_type,
            "summary": summary[:512],
            "note": note[:20000],
            "date_deadline": payload.get("date_deadline"),
            "user_id": payload.get("user_id"),
        }
        return normalized, {
            "risk_level": "low",
            "summary": _("Schedule an activity on %(model)s #%(id)s", model=model_name, id=record.id),
            "target_count": 1,
            "diff": [{"id": record.id, "summary": summary[:200], "activity_type": activity_type}],
        }

    @api.model
    def _prepare_attachment(self, credential, payload):
        if not credential.profile_id.allow_attachments:
            raise McpServiceError("policy_denied", _("Attachment actions are disabled."), status=403)
        model_name = self._model_name(payload)
        Model, policy = self._model_policy(credential, model_name, "write")
        record_id = self._positive_int(payload.get("id"), "id")
        record = self._records_in_policy(Model, policy, [record_id], "write")
        name = payload.get("name")
        content = payload.get("content_base64")
        if not isinstance(name, str) or not name.strip() or len(name) > 255:
            raise McpServiceError("invalid_attachment", _("Attachment name is invalid."))
        if not isinstance(content, str):
            raise McpServiceError("invalid_attachment", _("Attachment content must be base64."))
        size = self._decoded_size(content)
        if size > self._max_binary_bytes():
            raise McpServiceError("payload_too_large", _("Attachment exceeds the configured limit."), status=413)
        normalized = {
            "model": model_name,
            "id": record.id,
            "name": name,
            "mimetype": payload.get("mimetype") or "application/octet-stream",
            "content_base64": content,
        }
        return normalized, {
            "risk_level": "medium",
            "summary": _("Attach %(name)s to %(model)s #%(id)s", name=name, model=model_name, id=record.id),
            "target_count": 1,
            "diff": [{"id": record.id, "name": name, "size": size}],
        }

    @api.model
    def _prepare_method(self, credential, payload):
        model_name = self._model_name(payload)
        method_name = payload.get("method")
        if not isinstance(method_name, str) or method_name.startswith("_"):
            raise McpServiceError("policy_denied", _("Private or invalid methods are forbidden."), status=403)
        method_policy = credential.profile_id.method_policy_ids.filtered(
            lambda policy: (
                policy.active
                and policy.model_id.model == model_name
                and policy.method_name == method_name
            )
        )[:1]
        if not method_policy:
            raise McpServiceError("policy_denied", _("Method is not explicitly allowed."), status=403)
        Model, model_policy = self._model_policy(credential, model_name, "read")
        ids = self._parse_ids(
            payload.get("ids", []),
            max_count=method_policy.max_record_count,
            allow_empty=True,
        )
        records = self._records_in_policy(Model, model_policy, ids, "read") if ids else Model
        if not hasattr(records, method_name):
            raise McpServiceError("not_found", _("Allowed method does not exist."), status=404)
        args = payload.get("args", [])
        kwargs = payload.get("kwargs", {})
        if not isinstance(args, list) or not isinstance(kwargs, dict):
            raise McpServiceError("invalid_arguments", _("Method args must be a list and kwargs an object."))
        normalized = {
            "model": model_name,
            "ids": ids,
            "method": method_name,
            "args": args,
            "kwargs": kwargs,
        }
        return normalized, {
            "risk_level": method_policy.risk_level,
            "summary": _("Call %(model)s.%(method)s on %(count)s record(s)", model=model_name, method=method_name, count=len(ids)),
            "target_count": len(ids),
            "diff": [{"method": method_name, "ids": ids, "argument_keys": sorted(kwargs)}],
        }

    @api.model
    def _execute_action(self, credential, action, payload):
        normalized, _preview = self._prepare_action(credential, action, payload)
        model_name = normalized["model"]
        if action == "record.create":
            Model, _policy = self._model_policy(credential, model_name, "create")
            records = Model.create(normalized["values"])
            return {"model": model_name, "ids": records.ids, "count": len(records)}
        if action == "record.update":
            Model, policy = self._model_policy(credential, model_name, "write")
            records = self._records_in_policy(Model, policy, normalized["ids"], "write")
            records.write(normalized["values"])
            return {"model": model_name, "ids": records.ids, "count": len(records)}
        if action == "record.delete":
            Model, policy = self._model_policy(credential, model_name, "unlink")
            records = self._records_in_policy(Model, policy, normalized["ids"], "unlink")
            ids = records.ids
            records.unlink()
            return {"model": model_name, "ids": ids, "count": len(ids)}
        if action == "message.post":
            Model, policy = self._model_policy(credential, model_name, "write")
            record = self._records_in_policy(Model, policy, [normalized["id"]], "write")
            message = record.message_post(
                body=Markup("<p>%s</p>") % escape(normalized["body"]),
                message_type="comment",
            )
            return {"model": model_name, "id": record.id, "message_id": message.id}
        if action == "activity.schedule":
            Model, policy = self._model_policy(credential, model_name, "write")
            record = self._records_in_policy(Model, policy, [normalized["id"]], "write")
            values = {}
            if normalized.get("user_id"):
                values["user_id"] = self._positive_int(normalized["user_id"], "user_id")
            activity = record.activity_schedule(
                act_type_xmlid=normalized["activity_type"],
                date_deadline=normalized.get("date_deadline"),
                summary=normalized.get("summary", ""),
                note=normalized.get("note", ""),
                **values,
            )
            return {"model": model_name, "id": record.id, "activity_ids": activity.ids}
        if action == "attachment.create":
            Model, policy = self._model_policy(credential, model_name, "write")
            record = self._records_in_policy(Model, policy, [normalized["id"]], "write")
            attachment = Model.env["ir.attachment"].create(
                {
                    "name": normalized["name"],
                    "mimetype": normalized["mimetype"],
                    "datas": normalized["content_base64"],
                    "res_model": model_name,
                    "res_id": record.id,
                }
            )
            return {"model": model_name, "id": record.id, "attachment_id": attachment.id}
        if action == "method.call":
            Model, policy = self._model_policy(credential, model_name, "read")
            records = (
                self._records_in_policy(Model, policy, normalized["ids"], "read")
                if normalized["ids"]
                else Model
            )
            result = getattr(records, normalized["method"])(
                *normalized["args"],
                **normalized["kwargs"],
            )
            return {"model": model_name, "method": normalized["method"], "result": self._json_safe(result)}
        raise McpServiceError("unknown_action", _("Unknown change action."), status=404)

    @api.model
    def _business_env(self, credential):
        company_ids = credential.profile_id._allowed_company_ids(credential.user_id)
        context = {
            **self.env.context,
            "allowed_company_ids": company_ids,
        }
        return self.env(
            user=credential.user_id.id,
            su=False,
            context=context,
        )

    @api.model
    def _model_policy(self, credential, model_name, operation):
        try:
            policy = credential.profile_id._get_policy(model_name, operation)
        except ValidationError as exc:
            raise McpServiceError("policy_denied", str(exc), status=403) from exc
        env = self._business_env(credential)
        if model_name not in env:
            raise McpServiceError("unknown_model", _("Model is not installed."), status=404)
        Model = env[model_name]
        access_operation = "read" if operation == "aggregate" else operation
        Model.browse().check_access(access_operation)
        return Model, policy

    @api.model
    def _has_model_access(self, Model, operation):
        access_operation = "read" if operation == "aggregate" else operation
        return Model.browse().has_access(access_operation)

    @api.model
    def _model_name(self, params):
        model_name = params.get("model")
        if not isinstance(model_name, str) or not MODEL_NAME_RE.fullmatch(model_name):
            raise McpServiceError("invalid_model", _("A valid model name is required."))
        return model_name

    @api.model
    def _combined_domain(self, policy, client_domain):
        if not isinstance(client_domain, list):
            raise McpServiceError("invalid_domain", _("Domain must be a JSON list."))
        try:
            return Domain(client_domain) & policy._forced_domain()
        except (TypeError, ValueError) as exc:
            raise McpServiceError("invalid_domain", _("Domain is malformed.")) from exc

    @api.model
    def _records_in_policy(self, Model, policy, ids, operation):
        records = Model.search(
            Domain("id", "in", ids) & policy._forced_domain(),
            limit=len(ids),
        )
        if set(records.ids) != set(ids):
            raise McpServiceError(
                "access_denied",
                _("One or more records are outside the allowed scope."),
                status=403,
            )
        records.check_access(operation)
        return records

    @api.model
    def _read_fields(self, policy, Model, requested):
        allowed = policy._allowed_field_names("read", Model)
        if requested is None:
            requested = sorted(allowed)
        if not isinstance(requested, list) or not all(isinstance(name, str) for name in requested):
            raise McpServiceError("invalid_fields", _("Fields must be a list of strings."))
        denied = set(requested) - allowed
        if denied:
            raise McpServiceError(
                "field_denied",
                _("One or more requested fields are not allowed."),
                status=403,
            )
        return list(dict.fromkeys(requested))

    @api.model
    def _write_values(self, policy, Model, values, operation):
        if not isinstance(values, dict) or not values:
            raise McpServiceError("invalid_values", _("Write values must be a non-empty object."))
        allowed = policy._allowed_field_names(operation, Model)
        denied = set(values) - allowed
        if denied or set(values) & WRITE_MAGIC_FIELDS:
            raise McpServiceError(
                "field_denied",
                _("One or more write fields are not allowed."),
                status=403,
            )
        for name in values:
            field = Model._fields.get(name)
            if not field:
                raise McpServiceError("invalid_fields", _("Unknown write field."))
            Model._check_field_access(field, "write")
            if field.readonly:
                raise McpServiceError("field_denied", _("Readonly fields cannot be written."), status=403)
        return values

    @api.model
    def _limit(self, policy, value):
        limit = policy._record_limit() if value is None else self._positive_int(value, "limit")
        return min(limit, policy._record_limit())

    @api.model
    def _offset(self, value):
        if value in (None, False):
            return 0
        if not isinstance(value, int) or isinstance(value, bool) or not (0 <= value <= 100000):
            raise McpServiceError("invalid_offset", _("Offset must be between 0 and 100000."))
        return value

    @api.model
    def _parse_order(self, Model, policy, value):
        if value in (None, ""):
            return None
        if not isinstance(value, str) or len(value) > 512:
            raise McpServiceError("invalid_order", _("Invalid order expression."))
        readable = policy._allowed_field_names("read", Model)
        normalized = []
        for part in value.split(","):
            match = ORDER_PART_RE.fullmatch(part.strip())
            if not match or match.group(1) not in readable:
                raise McpServiceError("invalid_order", _("Order contains a forbidden field."))
            normalized.append(
                f"{match.group(1)} {match.group(2).lower() if match.group(2) else 'asc'}"
            )
        return ", ".join(normalized)

    @api.model
    def _parse_ids(self, value, *, max_count, allow_empty=False):
        if value is None and allow_empty:
            return []
        if not isinstance(value, list):
            raise McpServiceError("invalid_ids", _("Record IDs must be a list."))
        if not value and not allow_empty:
            raise McpServiceError("invalid_ids", _("At least one record ID is required."))
        if len(value) > max_count:
            raise McpServiceError("batch_too_large", _("Record batch exceeds the allowed limit."), status=413)
        if any(not isinstance(item, int) or isinstance(item, bool) or item <= 0 for item in value):
            raise McpServiceError("invalid_ids", _("Record IDs must be positive integers."))
        if len(set(value)) != len(value):
            raise McpServiceError("invalid_ids", _("Duplicate record IDs are not allowed."))
        return value

    @api.model
    def _positive_int(self, value, name):
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise McpServiceError("invalid_integer", _("%s must be a positive integer.", name))
        return value

    @api.model
    def _approval_for_credential(self, credential, approval_id):
        if not isinstance(approval_id, str) or len(approval_id) > 64:
            raise McpServiceError("invalid_approval", _("A valid approval ID is required."))
        approval = (
            self.env["odoo.mcp.approval"]
            .sudo()
            .search(
                [
                    ("credential_id", "=", credential.id),
                    ("request_uid", "=", approval_id),
                ],
                limit=1,
            )
        )
        if not approval:
            raise McpServiceError("not_found", _("Approval was not found."), status=404)
        return approval

    @api.model
    def _decoded_size(self, value):
        try:
            return len(base64.b64decode(value, validate=True))
        except (binascii.Error, ValueError, TypeError) as exc:
            raise McpServiceError("invalid_base64", _("Binary content is not valid base64.")) from exc

    @api.model
    def _max_binary_bytes(self):
        return int(
            self.env["ir.config_parameter"].sudo().get_param(
                "odoo_mcp_control.max_binary_bytes",
                str(5 * 1024 * 1024),
            )
        )

    @api.model
    def _redact_value(self, field_name, value):
        if SENSITIVE_NAME_RE.search(field_name):
            return "[REDACTED]"
        safe = self._json_safe(value)
        if isinstance(safe, str) and len(safe) > 500:
            return f"{safe[:500]}…"
        return safe

    @api.model
    def _json_safe(self, value):
        if value is None or isinstance(value, (bool, int, float, str)):
            return value
        if isinstance(value, (date, datetime)):
            return fields.Datetime.to_string(value) if isinstance(value, datetime) else fields.Date.to_string(value)
        if isinstance(value, bytes):
            return base64.b64encode(value).decode()
        if isinstance(value, dict):
            return {str(key): self._json_safe(item) for key, item in value.items()}
        if isinstance(value, (list, tuple, set)):
            return [self._json_safe(item) for item in value]
        if hasattr(value, "_name") and hasattr(value, "ids"):
            return {"model": value._name, "ids": value.ids}
        return str(value)

    @api.model
    def _canonical_json(self, value):
        return json.dumps(
            self._json_safe(value),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )

    @api.model
    def _error_body(self, request_id, code, message, retryable):
        return {
            "ok": False,
            "request_id": request_id,
            "error": {
                "code": code,
                "message": str(message),
                "retryable": bool(retryable),
            },
        }

    @api.model
    def _input_summary(self, operation, params):
        model_name = params.get("model") if isinstance(params.get("model"), str) else ""
        keys = sorted(str(key) for key in params)[:30]
        return f"operation={operation}; model={model_name}; keys={','.join(keys)}"[:512]

    @api.model
    def _safe_target_ids(self, value):
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            return [value]
        if isinstance(value, list):
            return [
                item
                for item in value[:100]
                if isinstance(item, int) and not isinstance(item, bool) and item > 0
            ]
        return []

    @api.model
    def _audit(
        self,
        credential,
        *,
        request_id,
        operation,
        model_name,
        input_hash,
        input_summary,
        target_ids,
        outcome,
        status_code,
        error_code,
        error_class,
        duration_ms,
        remote_ip,
        user_agent,
        approval,
    ):
        values = {
            "request_id": request_id,
            "credential_id": credential.id,
            "profile_id": credential.profile_id.id,
            "connector_user_id": credential.user_id.id,
            "approval_id": approval.id if approval else False,
            "operation": operation,
            "model_name": model_name,
            "input_hash": input_hash,
            "input_summary": input_summary,
            "target_ids_json": self._canonical_json(target_ids),
            "outcome": outcome,
            "status_code": status_code,
            "error_code": error_code,
            "error_class": error_class,
            "duration_ms": duration_ms,
            "remote_ip": (remote_ip or "")[:64],
            "user_agent": (user_agent or "")[:512],
        }
        self.env["odoo.mcp.audit.log"].sudo().with_context(
            mcp_audit_system_create=True
        ).create(values)

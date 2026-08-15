from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

MODEL_RE = re.compile(r"^[a-zA-Z0-9_.]+$")
FIELD_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class ModelRequest(StrictModel):
    model: str

    @field_validator("model")
    @classmethod
    def valid_model(cls, value: str) -> str:
        if not MODEL_RE.fullmatch(value):
            raise ValueError("Invalid Odoo model name.")
        return value


class SearchRequest(ModelRequest):
    domain: list[Any] = Field(default_factory=list)
    fields: list[str] | None = None
    offset: int = Field(default=0, ge=0, le=100_000)
    limit: int = Field(default=100, ge=1, le=1000)
    order: str | None = Field(default=None, max_length=512)

    @field_validator("fields")
    @classmethod
    def valid_fields(cls, value: list[str] | None) -> list[str] | None:
        if value is not None and (
            len(value) > 200 or any(not FIELD_RE.fullmatch(name) for name in value)
        ):
            raise ValueError("Invalid field list.")
        return value


class ReadRequest(ModelRequest):
    ids: list[int] = Field(min_length=1, max_length=100)
    fields: list[str] | None = None

    @field_validator("ids")
    @classmethod
    def positive_unique_ids(cls, value: list[int]) -> list[int]:
        if any(item <= 0 for item in value) or len(set(value)) != len(value):
            raise ValueError("IDs must be unique positive integers.")
        return value


class AggregateRequest(ModelRequest):
    domain: list[Any] = Field(default_factory=list)
    fields: list[str] = Field(default_factory=list, max_length=50)
    groupby: list[str] = Field(default_factory=list, max_length=10)
    limit: int = Field(default=100, ge=1, le=1000)


class PreviewBase(ModelRequest):
    idempotency_key: str = Field(min_length=8, max_length=128)


class PreviewCreate(PreviewBase):
    values: dict[str, Any] | list[dict[str, Any]]


class PreviewUpdate(PreviewBase):
    ids: list[int] = Field(min_length=1, max_length=100)
    values: dict[str, Any]


class PreviewDelete(PreviewBase):
    ids: list[int] = Field(min_length=1, max_length=100)


class PreviewMethod(PreviewBase):
    ids: list[int] = Field(default_factory=list, max_length=100)
    method: str = Field(pattern=r"^[a-zA-Z][a-zA-Z0-9_]*$")
    args: list[Any] = Field(default_factory=list, max_length=100)
    kwargs: dict[str, Any] = Field(default_factory=dict)


class PreviewMessage(PreviewBase):
    id: int = Field(gt=0)
    body: str = Field(min_length=1, max_length=20_000)


class PreviewActivity(PreviewBase):
    id: int = Field(gt=0)
    activity_type: str = "mail.mail_activity_data_todo"
    summary: str = Field(default="", max_length=512)
    note: str = Field(default="", max_length=20_000)
    date_deadline: str | None = None
    user_id: int | None = Field(default=None, gt=0)


class PreviewAttachment(PreviewBase):
    id: int = Field(gt=0)
    name: str = Field(min_length=1, max_length=255)
    content_base64: str
    mimetype: str = Field(default="application/octet-stream", max_length=255)


class ApprovalRequest(StrictModel):
    approval_id: str = Field(min_length=8, max_length=64)


class AttachmentReadRequest(StrictModel):
    attachment_id: int = Field(gt=0)


class ReportRequest(StrictModel):
    report: str = Field(pattern=r"^[a-zA-Z0-9_]+[.][a-zA-Z0-9_.]+$")
    ids: list[int] = Field(min_length=1, max_length=20)


class DomainSummaryRequest(StrictModel):
    company_ids: list[int] | None = None
    date_from: str | None = None
    date_to: str | None = None
    limit: int = Field(default=20, ge=1, le=100)

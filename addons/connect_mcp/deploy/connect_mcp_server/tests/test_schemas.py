from __future__ import annotations

import pytest
from pydantic import ValidationError

from connect_mcp_server.schemas import ReadRequest, SearchRequest


@pytest.mark.parametrize("fields", [["invalid field"], [f"field_{index}" for index in range(201)]])
def test_search_request_rejects_invalid_fields(fields: list[str]) -> None:
    with pytest.raises(ValidationError, match="Invalid field list"):
        SearchRequest(model="res.partner", fields=fields)


@pytest.mark.parametrize("ids", [[0], [1, 1]])
def test_read_request_requires_unique_positive_ids(ids: list[int]) -> None:
    with pytest.raises(ValidationError, match="unique positive"):
        ReadRequest(model="res.partner", ids=ids)

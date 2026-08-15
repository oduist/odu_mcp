from __future__ import annotations

import asyncio
import json
import socket
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
import pytest
import uvicorn
from fastmcp import Client, FastMCP
from fastmcp.server.auth import AccessToken, TokenVerifier
from mcp.client.subscriptions import listen

from connect_mcp_server.config import Settings
from connect_mcp_server.server import create_server
from connect_mcp_server.subscriptions import SubscriptionPublisher


class _SubscriptionVerifier(TokenVerifier):
    def __init__(self) -> None:
        super().__init__(base_url="http://127.0.0.1", required_scopes=["mcp"])

    async def verify_token(self, token: str) -> AccessToken | None:
        if token not in {"alice-key", "bob-key"}:
            return None
        return AccessToken(
            token=token,
            client_id=token,
            subject=token.removesuffix("-key"),
            scopes=["mcp"],
        )


@asynccontextmanager
async def _serve(app) -> AsyncIterator[str]:
    server_socket = socket.socket()
    server_socket.bind(("127.0.0.1", 0))
    server_socket.listen(128)
    port = server_socket.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", lifespan="on"))
    task = asyncio.create_task(server.serve(sockets=[server_socket]))
    while not server.started:
        if task.done():
            await task
        await asyncio.sleep(0.01)
    try:
        yield f"http://127.0.0.1:{port}/mcp"
    finally:
        server.should_exit = True
        await task
        server_socket.close()


@pytest.mark.asyncio
async def test_http_mcp_two_users_and_approval_flow() -> None:
    users = {
        "alice-key": {"user_id": 7, "login": "alice", "profile": "sales"},
        "bob-key": {"user_id": 8, "login": "bob", "profile": "readonly"},
    }
    approvals: dict[str, dict[str, object]] = {}
    forwarded: list[tuple[str, str]] = []

    def odoo_handler(request: httpx.Request) -> httpx.Response:
        authorization = request.headers.get("Authorization", "")
        token = authorization.removeprefix("Bearer ")
        user = users.get(token)
        if user is None:
            return httpx.Response(401, json={"ok": False}, request=request)
        if request.url.path == "/connect_mcp/v1/identity":
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "data": {"database": "test", **user},
                },
                request=request,
            )

        payload = json.loads(request.content)
        operation = payload["operation"]
        forwarded.append((token, operation))
        params = payload["params"]
        if operation == "identity.whoami":
            data = {"user_id": user["user_id"], "login": user["login"]}
        elif operation == "changes.preview":
            approval_id = str(uuid.uuid4())
            approvals[approval_id] = {
                "owner": token,
                "state": "pending",
                "action": params["action"],
            }
            data = {"approval_id": approval_id, "state": "pending"}
        elif operation in {"changes.status", "changes.execute"}:
            approval = approvals[params["approval_id"]]
            if approval["owner"] != token:
                return httpx.Response(
                    403,
                    json={
                        "ok": False,
                        "error": {
                            "code": "not_found",
                            "message": "Approval was not found.",
                            "retryable": False,
                        },
                    },
                    request=request,
                )
            if operation == "changes.execute" and approval["state"] == "approved":
                approval["state"] = "executed"
            data = {"approval_id": params["approval_id"], "state": approval["state"]}
        else:
            raise AssertionError(f"Unexpected operation: {operation}")
        return httpx.Response(
            200,
            json={"ok": True, "request_id": str(uuid.uuid4()), "data": data},
            request=request,
        )

    transport = httpx.MockTransport(odoo_handler)
    server = create_server(
        Settings(
            odoo_url="http://odoo.test",
            tool_groups=frozenset({"core", "write"}),
            events_enabled=False,
        ),
        client_transport=transport,
        auth_transport=transport,
    )
    app = server.http_app(path="/mcp", stateless_http=True, json_response=True)

    async with _serve(app) as url:
        async with Client(url, auth="alice-key") as alice, Client(url, auth="bob-key") as bob:
            alice_identity = await alice.call_tool("odoo_whoami", {})
            bob_identity = await bob.call_tool("odoo_whoami", {})
            assert alice_identity.data["user_id"] == 7
            assert bob_identity.data["user_id"] == 8

            preview = await alice.call_tool(
                "odoo_preview_create",
                {
                    "model": "res.partner",
                    "values": {"name": "New partner"},
                    "idempotency_key": "http-e2e-create-1",
                },
            )
            approval_id = preview.data["approval_id"]
            denied = await bob.call_tool(
                "odoo_get_change_status",
                {"approval_id": approval_id},
                raise_on_error=False,
            )
            assert denied.is_error is True
            assert "not_found" in denied.content[0].text

            approvals[approval_id]["state"] = "approved"
            executed = await alice.call_tool(
                "odoo_execute_approved_change",
                {"approval_id": approval_id},
            )
            assert executed.data["state"] == "executed"

    assert ("alice-key", "identity.whoami") in forwarded
    assert ("bob-key", "identity.whoami") in forwarded
    assert ("alice-key", "changes.execute") in forwarded
    assert ("bob-key", "changes.status") in forwarded


@pytest.mark.asyncio
async def test_http_mcp_subscriptions_listen_end_to_end() -> None:
    server = FastMCP("subscription-e2e", auth=_SubscriptionVerifier())
    publisher = SubscriptionPublisher()
    publisher.install(server)
    app = server.http_app(path="/mcp", stateless_http=True, json_response=True)

    try:
        async with (
            _serve(app) as url,
            Client(url, auth="alice-key") as alice,
            Client(url, auth="bob-key") as bob,
        ):
            async with (
                listen(
                    alice.session,
                    resource_subscriptions=["odoo://approval/example"],
                ) as alice_subscription,
                listen(
                    bob.session,
                    resource_subscriptions=["odoo://approval/example"],
                ) as bob_subscription,
            ):
                bob_event = asyncio.create_task(anext(bob_subscription))
                await publisher.publish("alice", "odoo://approval/example")
                event = await asyncio.wait_for(anext(alice_subscription), timeout=2)
                assert event.uri == "odoo://approval/example"
                await asyncio.sleep(0)
                assert not bob_event.done()
                await publisher.publish("bob", "odoo://approval/example")
                bob_notification = await asyncio.wait_for(bob_event, timeout=2)
                assert bob_notification.uri == "odoo://approval/example"
                publisher.close()
    finally:
        publisher.close()

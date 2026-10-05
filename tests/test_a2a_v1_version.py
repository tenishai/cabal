"""The A2A-Version gate (#265): the v1.0 path accepts 1.0 (any patch) and no
other version, legacy paths
accept requests that send no A2A-Version and refuse a well-formed request
that sends a non-empty one. Everywhere the version is checked after the body
is validated (the JSON-RPC envelope, or the TaskRequest model on /a2a/tasks)."""

from __future__ import annotations

import json

import pytest
from httpx import AsyncClient
from starlette.requests import Request

from cabal.core.a2a.version import is_v1, major_minor, requested_version

V1_PATH = "/agents/echo/a2a"
CALL = {"jsonrpc": "2.0", "id": 7, "method": "SendMessage", "params": {}}


def _request(headers: dict[str, str] | None = None, query: str = "") -> Request:
    raw = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    return Request({"type": "http", "headers": raw, "query_string": query.encode()})


# ---- the version parameter -------------------------------------------------


def test_header_then_query_param_then_none() -> None:
    assert requested_version(_request({"A2A-Version": "1.0"})) == "1.0"
    assert requested_version(_request(query="A2A-Version=1.0")) == "1.0"
    assert requested_version(_request({"A2A-Version": "1.0"}, "A2A-Version=0.3")) == "1.0"
    assert requested_version(_request({"A2A-Version": "  "}, "A2A-Version=1.0")) == "1.0"
    assert requested_version(_request({"A2A-Version": ""})) is None
    assert requested_version(_request()) is None


@pytest.mark.parametrize(
    "version, v1",
    [("1.0", True), ("1.0.3", True), ("0.3", False), ("1.1", False), ("2.0", False), (None, False)],
)
def test_is_v1_ignores_patch(version, v1) -> None:
    assert is_v1(version) is v1


def test_major_minor() -> None:
    assert major_minor("1.0.7") == "1.0"
    assert major_minor("1") == "1"


# ---- the v1.0 path -----------------------------------------------------------


def _version_error(body: dict) -> dict:
    assert body["jsonrpc"] == "2.0"
    err = body["error"]
    assert err["code"] == -32009
    (info,) = err["data"]
    assert info["reason"] == "VERSION_NOT_SUPPORTED"
    assert info["domain"] == "a2a-protocol.org"
    return err


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "headers, query",
    [({"A2A-Version": "1.0"}, ""), ({}, "?A2A-Version=1.0"), ({"A2A-Version": "1.0.2"}, "")],
)
async def test_v1_path_passes_1_0_to_the_handler(client: AsyncClient, headers, query) -> None:
    # Past the version gate, the next check is the bearer token: no token is a 401.
    r = await client.post(V1_PATH + query, json=CALL, headers=headers)
    assert r.status_code == 401
    assert r.headers["www-authenticate"].startswith("Bearer")


@pytest.mark.asyncio
@pytest.mark.parametrize("version", [None, "", "0.3", "1.1", "2.0"])
async def test_v1_path_refuses_every_other_version(client: AsyncClient, version) -> None:
    headers = {} if version is None else {"A2A-Version": version}
    r = await client.post(V1_PATH, json=CALL, headers=headers)
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/json")
    err = _version_error(r.json())
    assert r.json()["id"] == 7
    assert err["data"][0]["metadata"]["requestedVersion"] == (version or "")


@pytest.mark.asyncio
async def test_v1_path_accepts_a_trailing_slash(client: AsyncClient) -> None:
    r = await client.post(V1_PATH + "/", json=CALL, headers={"A2A-Version": "1.0"})
    assert r.status_code == 401
    assert r.headers["www-authenticate"].startswith("Bearer")


@pytest.mark.asyncio
async def test_v1_path_parse_error_and_invalid_envelope(client: AsyncClient) -> None:
    r = await client.post(
        V1_PATH,
        content=b"{not json",
        headers={"A2A-Version": "1.0", "Content-Type": "application/json"},
    )
    assert r.json()["error"]["code"] == -32700
    r = await client.post(
        V1_PATH, json={"id": 1, "method": "SendMessage"}, headers={"A2A-Version": "1.0"}
    )
    assert r.json()["error"]["code"] == -32600  # no "jsonrpc": "2.0"


# ---- the legacy paths --------------------------------------------------------


@pytest.mark.asyncio
async def test_legacy_jsonrpc_refuses_a_version_header(client: AsyncClient) -> None:
    call = {"jsonrpc": "2.0", "id": 3, "method": "message/send", "params": {}}
    r = await client.post("/agents/echo", json=call, headers={"A2A-Version": "1.0"})
    assert r.status_code == 200
    err = _version_error(r.json())
    assert "a2a" in err["message"]


@pytest.mark.asyncio
async def test_legacy_jsonrpc_without_a_version_is_unchanged(client: AsyncClient) -> None:
    """No header: the request goes on to the legacy auth check, as before."""
    call = {"jsonrpc": "2.0", "id": 3, "method": "message/send", "params": {}}
    r = await client.post("/agents/echo", json=call)
    assert r.json()["error"]["code"] == -32001  # legacy "missing Authorization", unchanged


@pytest.mark.asyncio
async def test_legacy_stream_refuses_a_version_header(client: AsyncClient) -> None:
    call = {"jsonrpc": "2.0", "id": 4, "method": "message/stream", "params": {}}
    r = await client.post("/agents/echo/stream", json=call, headers={"A2A-Version": "1.0"})
    assert r.headers["content-type"].startswith("text/event-stream")
    data_lines = [ln for ln in r.text.splitlines() if ln.startswith("data:")]
    assert any(json.loads(ln[5:])["error"]["code"] == -32009 for ln in data_lines)


@pytest.mark.asyncio
async def test_tasks_route_refuses_a_version_header(client: AsyncClient) -> None:
    task = {"id": "t1", "status": {"state": "submitted"}}
    body = {"target": "echo", "task": task}
    r = await client.post("/a2a/tasks", json=body, headers={"A2A-Version": "1.0"})
    assert r.status_code == 400
    assert "A2A-Version" in r.json()["detail"]
    # Without the header the same body passes the version check and reaches
    # the bearer check, as before.
    r = await client.post("/a2a/tasks", json=body)
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_v1_path_checks_the_envelope_before_the_version(client: AsyncClient) -> None:
    """Parse, envelope, version: the same order as the legacy routes."""
    r = await client.post(
        V1_PATH, json={"id": 1, "method": "SendMessage"}, headers={"A2A-Version": "0.3"}
    )
    assert r.json()["error"]["code"] == -32600
    r = await client.post(
        V1_PATH,
        content=b"{not json",
        headers={"A2A-Version": "0.3", "Content-Type": "application/json"},
    )
    assert r.json()["error"]["code"] == -32700


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body, echoed_id",
    [
        ({"jsonrpc": "2.0", "id": 1, "method": 123}, 1),  # method not a string; the id is valid
        ({"jsonrpc": "2.0", "id": 1, "method": None}, 1),
        ({"jsonrpc": "2.0", "id": {"a": 1}, "method": "SendMessage"}, None),  # invalid id
        ({"jsonrpc": "2.0", "id": True, "method": "SendMessage"}, None),  # a bool is not a number
        ({"jsonrpc": "2.0", "id": 1, "method": "SendMessage", "params": "x"}, 1),
        ({"jsonrpc": "2.0", "id": 1, "method": "SendMessage", "params": None}, 1),  # null params
        ({"id": 1, "method": "SendMessage"}, 1),  # no "jsonrpc"
        (["not", "an", "object"], None),
    ],
)
async def test_v1_path_rejects_an_invalid_jsonrpc_request(
    client: AsyncClient, body, echoed_id
) -> None:
    r = await client.post(V1_PATH, json=body, headers={"A2A-Version": "1.0"})
    out = r.json()
    assert out["error"]["code"] == -32600
    if echoed_id is None:
        assert out["id"] is None  # the invalid id was dropped, not echoed
    else:
        # type() as well as ==, because True == 1 in Python.
        assert out["id"] == echoed_id and type(out["id"]) is type(echoed_id)

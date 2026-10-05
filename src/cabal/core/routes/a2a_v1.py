"""The A2A v1.0 JSON-RPC endpoint for each persona, `/agents/{name}/a2a` (epic #272).

This step (#265) is the version gate: the path accepts `A2A-Version: 1.0` and
answers anything else, including no version at all (which spec 3.6.2 defines
as 0.3), with VersionNotSupportedError (-32009). Behind the gate is a stub
that answers every method with MethodNotFound until #267 adds `SendMessage`
and `GetTask`.

The path is distinct from the legacy `POST /agents/{name}` and from the
`/a2a` prefix of `POST /a2a/tasks`. A trailing slash is accepted, because
clients such as the A2A compatibility kit post to the interface URL plus "/".
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Path, Request
from fastapi.responses import JSONResponse

from cabal.core.a2a.v1_errors import (
    INVALID_REQUEST,
    JSON_PARSE_ERROR,
    METHOD_NOT_FOUND,
    VERSION_NOT_SUPPORTED,
    a2a_error,
    error_response,
    jsonrpc_error,
)
from cabal.core.a2a.version import is_v1, requested_version

router = APIRouter(prefix="/agents", tags=["a2a-v1"])


def _reply(body: dict[str, Any]) -> JSONResponse:
    return JSONResponse(body, media_type="application/json")


def _valid_id(value: Any) -> bool:
    """JSON-RPC 2.0: an id is a string, a number or null (a bool is not a number)."""
    return value is None or (isinstance(value, (str, int, float)) and not isinstance(value, bool))


def _is_jsonrpc_request(body: Any) -> bool:
    """A JSON-RPC 2.0 request object: `"jsonrpc": "2.0"`, a string `method`,
    a valid `id` when present, and `params` an object or array when present
    (JSON-RPC 2.0 lets `params` be omitted, not null)."""
    return (
        isinstance(body, dict)
        and body.get("jsonrpc") == "2.0"
        and isinstance(body.get("method"), str)
        and _valid_id(body.get("id"))
        and ("params" not in body or isinstance(body["params"], (dict, list)))
    )


@router.post("/{agent_name}/a2a")
@router.post("/{agent_name}/a2a/", include_in_schema=False)
async def v1_jsonrpc(
    request: Request,
    agent_name: str = Path(..., min_length=1, max_length=64),
) -> JSONResponse:
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 - any undecodable body is a parse error
        return _reply(error_response(None, jsonrpc_error(JSON_PARSE_ERROR, "Invalid JSON payload")))
    request_id = body.get("id") if isinstance(body, dict) else None
    if not _valid_id(request_id):
        request_id = None  # never echo back an id that JSON-RPC does not allow

    # Same order as the legacy routes: parse, then the envelope, then the version.
    if not _is_jsonrpc_request(body):
        return _reply(
            error_response(
                request_id, jsonrpc_error(INVALID_REQUEST, "Request payload validation error")
            )
        )
    version = requested_version(request)
    if not is_v1(version):
        return _reply(
            error_response(
                request_id,
                a2a_error(
                    VERSION_NOT_SUPPORTED,
                    message=f"this interface serves A2A 1.0, not {version or '0.3 (no A2A-Version)'}",
                    metadata={"requestedVersion": version or "", "supportedVersion": "1.0"},
                ),
            )
        )

    # Stub until #267: no v1.0 method is served yet.
    return _reply(
        error_response(
            request_id,
            jsonrpc_error(METHOD_NOT_FOUND, f"method not found: {body.get('method')!r}"),
        )
    )

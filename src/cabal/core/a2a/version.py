"""The A2A-Version service parameter (#265).

A2A v1.0, spec 3.6: a client sends `A2A-Version: <Major.Minor>` as an HTTP
header, or as an `A2A-Version` request parameter. An empty value means 0.3.
An agent processes the request with the semantics of the requested version,
and answers a version its interface does not serve with
VersionNotSupportedError (-32009). Patch numbers are not used and must not be
considered (spec 3.6), so `1.0.2` is read as `1.0`.

cabal's v1.0 interface is its v1.0 path (`/agents/{name}/a2a`, being built
under epic #272). The legacy JSON-RPC and task
routes serve cabal's pre-1.0 dialect, which is no declared A2A version, so
they accept requests that send no A2A-Version (every current client) and
refuse a well-formed request that sends a non-empty one. The version is
checked after the body, so a malformed body gets its own error first: a
JSON-RPC parse or envelope error, or FastAPI's 422 on /a2a/tasks.
"""

from __future__ import annotations

from fastapi import Request

HEADER = "A2A-Version"
V1 = "1.0"


def requested_version(request: Request) -> str | None:
    """The A2A-Version a request asks for, from the header or the query
    parameter (the header wins), or None when neither is set or both are empty."""
    value = request.headers.get(HEADER)
    if value is None or not value.strip():
        value = request.query_params.get(HEADER)
    if value is None:
        return None
    value = value.strip()
    return value or None


def major_minor(version: str) -> str:
    """`Major.Minor` of a version string; any patch part is dropped."""
    return ".".join(version.split(".")[:2])


def is_v1(version: str | None) -> bool:
    """True when the requested version is A2A 1.0 (any patch)."""
    return version is not None and major_minor(version) == V1

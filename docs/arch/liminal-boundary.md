---
card: arch-card:cabal-liminal-boundary
updated: 2026-10-07
anchor: src/cabal/core/tools/mcp_proxy.py sha=f2ffe7aa3cccbead1f17c31bab7884ca9f1265e5
anchor: src/cabal/core/persona_factory.py sha=e1825f818414b99c8b2d60f46f21a6792230f534
anchor: src/cabal/handlers/__init__.py sha=2e5f64bd92cf389a3ccaffb7bf7c44fdeef49580
anchor: pyproject.toml sha=45ff9ca88089254324486b671d2aa87c85481798
---
# liminal-boundary — cabal ↔ liminal (MCP gateway), three directions

WHAT / SEAM: liminal is the MCP gateway; cabal talks to it three ways.
1. OUTBOUND tools (cabal → liminal): `MCPToolProxy` (core/tools/mcp_proxy.py) wraps `liminal.client.AsyncMCPClient`; `build_proxy(...)` is called from core/persona_factory.py (the MCP block in the persona build) and sets `handler.tool_proxy` on an LLMPersona.
2. INBOUND handlers (liminal loads cabal code): pyproject `[project.entry-points."liminal.handlers"]` → patterns, documents, code, archivist = `cabal.handlers.<name>:get_handler`. Each returns a liminal `Handler` (tools / capability_card / dispatch) and imports `liminal.protocol` at module top.
3. CREDENTIALS (liminal → cabal HTTP): GET /admin/secrets/{persona}/{handler}; see secrets.md.
OPS / FLOW (outbound): persona tool-use loop → `list_tools_for_llm()` renames `handler.tool` → `handler__tool` (OpenAI function shape) → LLM picks a tool → `call_tool` maps the FIRST `__` back to `.` → liminal JSON-RPC with a JWT minted per request: sub=<agent name>, aud=`[liminal] jwt_aud` (default "liminal"), persona=<persona>, scope ["dispatch"], `cabal_tool_extras` = `[personas.X.mcp].tool_extras` + `liminal.allowed_handlers`, TTL `[liminal] token_ttl_seconds` (3600).
INVARIANTS:
- Wired ONLY IF `[liminal].url` set AND `[personas.X.mcp].enabled` AND a signing key AND the shared httpx client exist; otherwise run tool-less (logging `personas.mcp_skipped` only when `mcp.enabled` is true). WHY: a stale `[limenal]` section is otherwise ignored; `load_identity` logs `identity.unknown_section` for it and `liminal.url` stays unset, so the log lines are the only signal.
- `[liminal].url` set but liminal not installed → startup RuntimeError (core/app.py, cabal#126). Fail at boot, not first dispatch.
- liminal is optional for cabal core (lazy `_require_liminal`; distribution `tenishai-liminal>=0.3`, extra `tenishai-cabal[mcp]`), but `cabal.handlers.*` hard-import it. WHY: they are only ever loaded by liminal.
- `allowed_handlers`: None = unrestricted (omitted from the token); [] = forwarded, so liminal refuses everything.
- Tool names must not already contain `__`, or the reverse mapping breaks (only the first `__` is converted).
- Handlers lazy-import techne and return `ToolResult.failure("techne not installed…")` instead of raising. Per-call DB path from `ctx.extra["<handler>.db_path"]` rejects `..`; fallbacks from env `CABAL_ARCHITECT_DB`, `CABAL_LIBRARIAN_DB`, `CABAL_ARCHIVIST_DB`.
- "OPERATOR-ONLY" TOOLS ARE GATED BY LIMINAL, NOT CABAL: `patterns.record` and `documents.refresh` set `requires_operator: True` in their card dicts (cabal#233, #245; pinned by tests/test_operator_only_handlers.py) and the handler bodies check nothing themselves (`DocumentsHandler._refresh` says so). Enforcement is liminal's `tools/call` gate, which is NOT verified here; the persona→liminal token carries scope ["dispatch"] only, so a persona-minted call should be refused. Do not add a scope check inside a handler; keep the flag.
- In SOVEREIGN mode liminal ignores token-embedded scope and tool_extras and resolves a grant by (issuer, persona) (liminal server/routes.py `_authenticate`); tool_extras only steer single-cabal mode.
CONSUMERS: agents/llm_persona.py (tool-use loop, `tool_use_max_steps`); liminal's entry-point loader (liminal src/liminal/protocol/handler.py); POST /auth/liminal-token mints 15-min gateway tokens for colleague agents (dispatch-auth.md).
NOTE: the token provider is the `_token_provider` closure in persona_factory; `mcp_proxy.build_token_provider` was removed as dead code.

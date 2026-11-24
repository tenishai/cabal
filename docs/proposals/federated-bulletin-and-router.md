# `federated` Bulletin Entries + Router "Retire Local" — spec

|  |  |
| --- | --- |
| **Status** | Proposal |
| **Authors** | Richard DiMartino + AI |
| **Created** | 2026-07-02 |
| **Implements** | `docs/proposals/federated-singletons.md` §1 (graduation = replacement) |
| **Touches** | `core/models.py` (Agent), `core/bulletin/store.py`, `core/broker/router.py`, `core/routes/tasks.py` (token aud), persona factory, a migration |

---

## Goal

When a singleton persona has been graduated to a sovereign service, the parent
cabal must (a) **route to it remotely**, (b) **not run a local copy** of it, and
(c) **forward the caller's home JWT with the sovereign service's audience** so the
remote can verify it. Today the router decides local-vs-remote purely by
"is a handler registered?" (`router.py:235`). This spec adds a first-class
`federated` bulletin kind so the decision is explicit and can't be shadowed by a
stale local registration.

---

## 1. Data model — a `kind` on the bulletin row

`core/models.py` `Agent` gains two nullable columns; `AgentRow`
(`bulletin/store.py:22`) mirrors them:

```python
kind: str                       # "member" (default) | "federated"
federation_audience: str | None # sovereign service id callers must mint into `aud`
```

- **`member`** — today's behavior: an agent this cabal owns (local `local://…`
  endpoint, or its own container). Default for every existing row.
- **`federated`** — a sovereign peer this cabal is a client of. `endpoint` is the
  sovereign HTTPS URL; `federation_audience` is its stable service id
  (e.g. `archivist@example`), used both for outbound token `aud` and local re-verify.

`AgentRow` additions:

```python
@dataclass(slots=True, frozen=True)
class AgentRow:
    ...
    kind: str = "member"
    federation_audience: str | None = None
```

`from_orm` maps the columns; `register_agent(...)` gains `kind="member"` and
`federation_audience=None` keyword args. A new helper registers federated rows:

```python
async def register_federated(
    session, *, name, persona_name, endpoint, federation_audience,
    service_card_json, service_card_kid, capabilities=None, safety_tier="approval-required",
) -> AgentRow:
    # status="active", role="singleton", kind="federated", generation=1
```

**Migration** (Alembic, additive):
`ALTER TABLE agents ADD COLUMN kind TEXT NOT NULL DEFAULT 'member';`
`ALTER TABLE agents ADD COLUMN federation_audience TEXT;`
Existing rows backfill to `member` by the default — no behavioral change.

---

## 2. Router — "remote defined → retire local"

Two mechanisms, belt and suspenders:

### 2a. Don't register the local handler (primary)

In the persona factory (`core/persona_factory.py`, where
`router.register_local(handler)` is called), skip registration when the persona has
an active `federated` bulletin row:

```python
row = await bulletin_store.get_active(session, persona_agent_name)
if row is not None and row.kind == "federated":
    log.info("persona.federated_skip_local", agent=persona_agent_name,
             endpoint=row.endpoint)
    continue   # do NOT register_local; dispatch falls through to remote
```

With no local handler, the existing `name not in self._local` check
(`router.py:235`) already routes remote. This is the doc's "goes dormant (not
registered)."

### 2b. Dispatch-time guard (defense in depth)

Harden the resolution so a stray local registration can never shadow a federated
row. Change `router.py:235`:

```python
# was: if ctx.target.name in self._local:
if ctx.target.name in self._local and ctx.target.kind != "federated":
    response = await self._dispatch_local(self._local[ctx.target.name], task)
else:
    response = await self._dispatch_remote(ctx.target, task, caller_jwt)
```

`DispatchContext` already carries `target: AgentRow` (`router.py:60`), so
`ctx.target.kind` is in hand.

### 2c. Audience for federated targets

`_prepare` (`router.py:398`) verifies the caller JWT with
`expected_audience=target_agent` (`router.py:420`). For a `federated` target the
same token is **forwarded to the remote** (`_dispatch_remote`, `router.py:450`), so
its `aud` must be the sovereign service id, and the local re-verify must expect the
same value. The row is already fetched before the verify, so:

```python
expected_aud = (
    row.federation_audience if row.kind == "federated" and row.federation_audience
    else target_agent
)
payload = verify(caller_jwt, key_set=self._jwks_provider(), expected_audience=expected_aud)
```

`_dispatch_remote` needs no change — it already POSTs `endpoint + /a2a/tasks` with
`Authorization: Bearer {caller_jwt}` (`router.py:445-451`), and a `federated`
endpoint is a normal HTTPS URL (the `local://` guard at `router.py:435` only trips
for misconfigured members).

### 2d. Token minting upstream (`routes/tasks.py`)

Per cabal `D-010`, operator/agent dispatch tokens are re-minted per-target in
`routes/tasks.py::_resolve_dispatch_token`. For a `federated` target the minted
token's `aud` must be `row.federation_audience`, not the bare agent name. The
mint site looks up the row (or receives `kind`/`federation_audience` from the
resolved `AgentRow`) and sets `aud` accordingly. This is the one change outside the
router; everything else composes.

> Net: the calling agent's **home-cabal JWT** (iss = this cabal, aud = sovereign id,
> persona/sub = the agent) is what reaches the sovereign liminal — exactly the input
> `MultiIssuerJWKSVerifier` + `PolicyStore` expect (from
> liminal's multi-issuer trust-registry proposal, which is not in this repo).

---

## 3. Graduation & absorption wiring

- **`cbl persona graduate <name> --endpoint <url> --as-parent`** →
  mints the sovereign service identity + audience, then calls `register_federated`
  to write the `federated` row (replacing/superseding any `member` row for that
  name), and triggers a persona-factory reload so the local handler is dropped
  (2a). Stands up the sovereign liminal's trust registry + seeds the parent policy
  row (that half lives in the liminal spec).
- **`cbl persona absorb <name>`** → the inverse: `mark_retired` the `federated`
  row, restore the `member` registration on reload. Data re-absorption (pulling the
  techne store back) is a separate migration step, flagged in the design doc.

Client (non-parent) cabals run the same `register_federated` with
`--endpoint`/`--audience` from the parent, minus `--as-parent`; their agents then
dispatch normally.

---

## 4. What does NOT change

- Bookkeeping, AbortGraph, trace propagation, session hooks — all upstream of the
  local-vs-remote branch, so federated dispatch gets them unchanged.
- Streaming/resume paths (`dispatch_streaming`, `resume_local`) are local-only by
  construction; a federated persona simply isn't a local streaming target (they
  already raise a clear `DispatchError` when no local handler exists,
  `router.py:298`). Federated streaming is out of scope for v1.
- AgentCard signature verification semantics — a `federated` row stores the
  sovereign service's own signed card (`service_card_json`) rather than a
  cabal-signed one; response-card verification against the sovereign's JWKS is a
  follow-up (the router doesn't verify response cards today).

---

## 5. Test plan

Against the existing broker test suite:

1. **Federated routes remote** — a `federated` row with an HTTPS endpoint dispatches
   via `_dispatch_remote` even when a local handler for the same name is registered
   (proves 2b).
2. **Factory skips local** — persona factory does not `register_local` a persona
   with an active federated row (proves 2a).
3. **Audience swap** — caller JWT with `aud=<federation_audience>` verifies for a
   federated target; `aud=<agent-name>` is rejected for it, and vice-versa for a
   member (proves 2c).
4. **Absorb restores local** — after `mark_retired` on the federated row + reload,
   the name resolves local again.
5. **Migration backfill** — existing rows read back as `kind="member"`,
   `federation_audience=None`; all current broker tests pass unchanged.

---

## 6. Open items

- **Response-card verification.** Pinning + verifying the sovereign service's signed
  AgentCard on responses (JWKS from the `federated` row) — deferred; the router
  verifies caller cards, not callee responses, today.
- **Heartbeat semantics for federated rows.** A sovereign peer isn't heartbeating
  into my bulletin board. Either exempt `kind="federated"` from the staleness sweep
  or repurpose the field as a reachability probe. Exempt for v1.
- **Discovery of the sovereign's audience/JWKS.** For v1 the operator supplies
  `--endpoint`/`--audience` explicitly; a `/.well-known` discovery on the sovereign
  service is a nicety, not required.

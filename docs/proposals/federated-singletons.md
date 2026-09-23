# Federated Singletons — Graduating a Persona to a Parented Sovereign Service

|  |  |
| --- | --- |
| **Status** | Proposal |
| **Authors** | Richard DiMartino + Raven (AI) |
| **Created** | 2026-07-02 |
| **Extends** | Singleton personas and the two deployment modes. NOTE: this line cited `docs/cabal.md`, which does not exist in this repo (#202). The surviving description of singleton personas is [architecture.md](../architecture.md); the deployment-mode discussion was never carried over. |
| **Relates to** | techne#507 (federated fleets), liminal (JWKS verify + scope), the team-scale open question — originally `docs/cabal.md` §735, a document this repo does not have (#202) — (team-scale bootstrap creds) |

---

## TL;DR

A singleton persona (Archivist, Librarian, …) starts life as a **member** of one
cabal — in-process in solo mode, a co-located container in scale mode, with Cabal
Core as its IdP. When a real federation need appears (agents from *another* owner's
cabal need to reach it), the persona can be **graduated** into a **sovereign
service**: it leaves its parent's trust domain, authenticates every caller —
*including its former owner* — as a third party, and owns its own access policy.

Graduation is not orphaning. The graduating cabal is recorded as the persona's
**parent** and keeps standing admin authority: it manages the third-party
allowlist, rotates keys, reconfigures the persona, reads the audit log, and can
**re-absorb** the service back into itself. Third parties are *peers* with scoped
operational access the parent grants; the parent is the *guardian* with root.

Trust uses **federated JWKS (model A)**: each caller presents its own home-cabal
JWT; the sovereign service verifies it against that cabal's published JWKS and maps
`(issuer, persona) → scope` in its own policy table. No caller carries a
service-issued credential; every cabal stays sovereign over its own agents'
identities, and the sovereign persona is sovereign only over *policy*.

Most of the machinery already exists (liminal JWKS verification + per-(persona,agent)
scope, the techne `→archivist` outbox seam, the bulletin-board remote-resolution
router). What's new is the **graduation boundary**, the **parent principal**, and a
**multi-issuer trust registry**.

---

## Motivation

The original design locked singleton personas (Q10) as members of a single cabal — recorded in `docs/cabal.md`, which this repo does not have (#202). Even
in "production / scale mode," where Archivist runs in its own container, it stays
inside the operator's trust domain: Cabal Core signs its tokens, the `grants` table
gates it, it is *the operator's* Archivist reachable over HTTP.

Two things push past that:

1. **The federation thesis (techne#507).** The differentiator is *sovereign,
   federated fleets* — several operators each run their own cabal. Shared
   memory across those fleets means an agent in bob's cabal must be able to deposit
   into / query another operator's Archivist. That is a **cross-owner trust boundary**, which
   the v1 design explicitly defers (`docs/cabal.md` §735 and the Director note — neither survives here, see #202).

2. **The operator's framing (2026-07-02).** *"If we spin out the Archivist, he
   becomes his own thing — I'd stop using the local persona if a remote one is
   defined, but he wouldn't be a direct member of my cabal anymore either; he'd
   manage his own permissions with third parties trying to connect to him."*

So the v1 singleton has an undescribed third life-stage beyond `member/local` and
`member/containerized`: **sovereign**. This proposal specifies it.

---

## Life-stages of a singleton persona

| Stage | Trust domain | IdP | Reached via | Who authz's callers |
|---|---|---|---|---|
| **member / local** | the cabal | Cabal Core (in-proc) | direct Python call | Cabal Core + `grants` |
| **member / containerized** | the cabal | Cabal Core (HTTP) | A2A over HTTP | Cabal Core + `grants` |
| **sovereign / graduated** | *its own* | *itself* (multi-issuer) | A2A over HTTP | its own policy table |

The first two were already in the original design ("Single Package, Two Deployment
Modes"). This doc adds the third. Graduation is **demand-driven** — you only
graduate when a third party genuinely needs the persona; a solo operator never has
to.

---

## Design

### 1. Graduation is replacement, not fan-out

When the parent's bulletin board carries a **sovereign endpoint** for a persona,
the router prefers it and the local in-process handler is **not registered** (goes
dormant). There is exactly one Archivist and it is the remote one — no
local/remote duplication, no write-splitting.

Router rule (extends the existing local-vs-remote check in
`core/broker/router.py`): *if a persona has a sovereign endpoint defined, resolve
remote and skip local handler registration for it.* This is a small addition to
machinery that already exists — the router already chooses local vs. HTTP by
bulletin lookup.

The parent's bulletin board gains a new entry **kind** for such a persona:

```
kind = "federated"           # vs the existing "member"
endpoint = "https://archivist.<tailnet>.ts.net"
service_identity_jwks = "https://archivist.<tailnet>.ts.net/jwks.json"   # to verify ITS responses/card
parent = true                # this cabal graduated it; holds admin standing
```

A `member` entry means "an agent I own and vouch for." A `federated` entry means
"a peer service I am a client of." The operator's own agents dispatch to it exactly
as before at the call site; only resolution and token audience differ (§3).

### 2. The trust inversion

Today Archivist trusts the operator (Cabal Core signs its tokens). A **sovereign**
Archivist trusts *no single cabal*. It authenticates **everyone as a third party,
including its parent's agents**, on the data plane. Its parent's privileges live on
a separate **admin plane** (§4), not as a data-plane bypass.

### 3. Trust model A — federated JWKS

The sovereign persona runs its **own liminal gate** in front of its techne store +
LiteLLM brain. Its trust config has two parts:

**(a) Issuer registry** — the cabals it federates with:

```toml
[[issuer]]
id       = "alice-cabal"                       # parent
jwks_uri = "https://cabal.alice.<tailnet>.ts.net/jwks.json"
parent   = true

[[issuer]]
id       = "bob-cabal"
jwks_uri = "https://cabal.bob.<tailnet>.ts.net/jwks.json"

[[issuer]]
id       = "carol-cabal"
jwks_uri = "https://cabal.carol.<tailnet>.ts.net/jwks.json"
```

**(b) Policy table** — `(issuer, persona) → scope set`, the sovereign service's own
authz, owned by no cabal:

```
alice-cabal / *          → mem:query, mem:append, mem:synthesize, admin:*     (parent, seeded)
bob-cabal    / rook       → mem:query, mem:append
carol-cabal    / *          → mem:query
```

**Caller flow.** An agent in bob's cabal dispatches to the sovereign Archivist
presenting its **normal home-cabal JWT** (RS256, minted by bob's Cabal Core, with
`aud` including the Archivist's service id — see below). The gate:

1. Reads `iss`; looks it up in the issuer registry; fetches/caches that cabal's JWKS.
2. Verifies signature + `exp` + `aud == <archivist service id>`.
3. Authorizes against the policy table by `(iss, persona)`; denies if scope missing.

No caller holds an Archivist-issued credential. Each cabal remains its own IdP;
the sovereign persona is sovereign over **policy**, not over identity. This reuses
liminal's `URLJWKSVerifier` and its per-(persona,agent) scope almost as-is; the new
piece is that verification is **multi-issuer** (a registry of JWKS URLs) rather than
single-cabal.

**Audience minting.** For a dispatch to a `federated` persona, the caller's Cabal
Core mints the inter-agent JWT with `aud` including the sovereign service id — the
same per-target token minting cabal already does ([CABAL-DECISIONS.md D-010](../../CABAL-DECISIONS.md)). The
sovereign endpoint's service id is known from the `federated` bulletin entry.

### 4. The parent — guardian, not owner

At graduation, the graduating cabal is recorded as the persona's **parent**: a
distinguished, permanent principal in the sovereign service's own config. The
parent issuer is seeded with `admin:*` scope, and that row is **special** — it
cannot be removed or downgraded by any non-parent principal.

Parent admin capabilities (exercised remotely via `cbl` against the sovereign
service's admin surface, authenticated by the parent's own JWT carrying `admin:*`):

- **Manage the federation** — grant/revoke third-party cabals and their scopes.
- **Rotate** the sovereign service's signing key and its issuer-trust registry.
- **Reconfigure** the persona — prompt, model defaults, LiteLLM budget.
- **Audit** — read the full append-only access log.
- **Re-absorb** — the inverse of graduation: pull the persona (and its data) back
  in-house to a `member` stage, retiring the sovereign endpoint.

So the parent keeps post-graduation control *without* the persona being a
subordinate member. Sovereignty governs third-party operation; the parent holds
root. Exactly one parent exists per sovereign persona; **parent transfer** and
**parent-key recovery** are flagged open (§ Open questions).

Crucially, the parent has **two distinct relationships** with the persona and
must not conflate them:

- **Data plane (operational):** the parent's agents authenticate like anyone else —
  home JWT → verified → scoped. The parent is a first-class *client*.
- **Admin plane (guardian):** the parent additionally wields `admin:*` to manage the
  service. This is a separate capability, not a data-plane shortcut.

### 5. Identity continuity on graduation

Graduation mints the persona a **stable service identity** (its own keypair + id),
distinct from any cabal's identity. This is what it signs its AgentCard and
responses with, and what clients pin (`service_identity_jwks` in the `federated`
entry). Its data — the techne `archivist.db` / federated-memory store — moves with
it (or stays put; it was already its own store). The techne outbox `→archivist`
adapter (the original design's "Agent-local memory" section, not carried into this repo — #202) now targets the sovereign endpoint,
authenticating with the home-cabal JWT.

---

## What exists vs. what's new

**Reuse (already built):**
- liminal `URLJWKSVerifier` — JWT verification against a JWKS URL.
- liminal per-(persona,agent) **scope** — becomes the policy table.
- techne outbox `→archivist` adapter seam — retarget to the sovereign endpoint.
- Bulletin-board remote resolution + router local-vs-remote (`core/broker/router.py`).
- Per-target inter-agent token minting with `aud` (cabal D-010).
- Cabal Core as RS256 IdP with `GET /jwks.json` — this is exactly the JWKS each
  federated cabal publishes for others to verify against.

**New:**
1. **Multi-issuer trust registry** in the sovereign service (set of `{issuer id → JWKS URL}`),
   where liminal today assumes a single cabal.
2. **Parent principal** — the graduation record, the permanent `admin:*` root row,
   and the remote **admin surface** (`cbl` subcommands operating against a sovereign service).
3. **Graduation / absorption lifecycle** — the `member ↔ sovereign` state transition,
   the `federated` bulletin-entry kind, the "remote defined → retire local" router rule.
4. **Sovereign service identity** issuance at graduation.
5. **Scope vocabulary** for a federated persona (`mem:query`, `mem:append`,
   `mem:synthesize`, `admin:*`, …).

---

## CLI surface (sketch)

```bash
# Parent graduates its Archivist into a sovereign service it still parents.
cbl persona graduate archivist \
    --endpoint https://archivist.<tailnet>.ts.net \
    --as-parent
# → mints service identity, records this cabal as parent (admin:* seeded),
#   stands up the sovereign trust config, writes a `federated` bulletin entry,
#   retires the local handler.

# Parent manages the federation (runs against the sovereign service; parent JWT).
cbl federation grant  archivist bob-cabal --scope mem:query,mem:append
cbl federation revoke archivist carol-cabal
cbl federation ls     archivist                 # who's allowed, what scope
cbl federation audit  archivist --since 24h

# Parent maintenance.
cbl persona rotate-key archivist
cbl persona reconfigure archivist --model opus --budget 50/day

# Inverse: pull it back in-house to a member stage.
cbl persona absorb archivist
```

A federated *client* cabal (shaz) simply adds a `federated` bulletin entry pointing
at the endpoint; its agents then dispatch normally. Its Cabal Core must publish
`/jwks.json` reachably so the sovereign service can verify its agents.

---

## Worked example — bob's rook deposits a finding into alice's Archivist

```
1. alice: cbl persona graduate archivist --endpoint https://archivist.alice.example --as-parent
2. alice: cbl federation grant archivist bob-cabal --scope mem:query,mem:append
3. bob adds a `federated` archivist entry (endpoint + service jwks) to their bulletin board.
4. bob-rook dispatches deposit_finding → bob Cabal Core mints JWT
   { iss: bob-cabal, sub: rook-primus, aud: [archivist.alice], scope:[...], persona: rook }
5. Sovereign Archivist gate: iss=bob-cabal → fetch bob JWKS → verify sig+aud+exp
   → policy (bob-cabal, rook) has mem:append → ALLOW → deposit into federated memory.
6. alice (parent) later: cbl federation audit archivist  → sees bob-rook's deposit.
7. If bob misbehaves: alice: cbl federation revoke archivist bob-cabal  → done, no bob involvement.
```

alice's own rook uses the identical path (home JWT, scoped), plus the separate
`admin:*` channel for management.

---

## Open questions

- **Deployment / reachability.** Where does a sovereign persona run, and how do
  cabals reach each other? At solo/handful scale, **Tailscale between cabals** is the
  natural fit (it's already the client→cabal transport). mTLS between cabal edges is
  the harder-boundary option — see `HA-traefik-mtls-PLAN.md` (techne#506). Pick the
  minimal reachable shape first.
- **Parent transfer & parent-key recovery.** Exactly one parent today. Transferring
  parenthood, surviving loss of the parent's signing key, or (deliberately) a
  multi-parent quorum are v-next. Flagged, not solved.
- **Which singletons graduate.** Graduation earns its complexity for **shared-write**
  singletons (Archivist; Librarian if a shared corpus). Architect (small, read-mostly
  pattern corpus, already v1) may never need it. Don't build graduation for personas
  that have no cross-owner write demand.
- **Revocation latency.** Home JWTs are short-lived (inter-agent access = 5m per
  cabal auth spec), so revoking an issuer/scope in the policy table takes effect
  within a token lifetime naturally; no revocation list needed for the common case.
- **Scope vocabulary.** The `mem:*` set here is illustrative; the real capability
  taxonomy should derive from Archivist's actual A2A/MCP surface
  (`memory_lookup`, `recent_decisions`, `synthesize_history`, `deposit_finding`, …).
- **Audit store.** The sovereign service keeps its own append-only access log
  (natural for Archivist). Whether that log is itself queryable federated memory or a
  separate store is TBD.

---

## Why this shape

- **Federated JWKS (A) over service-issued creds (B):** keeps each cabal sovereign
  over its own agents' identities; the sovereign persona is sovereign only over
  policy. No second identity per agent, no per-cabal onboarding handshake beyond
  publishing a JWKS URL. Reuses liminal's existing verifier.
- **Parent as guardian, not owner:** answers the operator's requirement directly —
  graduation must not orphan. Root stays with the graduating cabal; day-to-day
  third-party operation is delegated to the service's own policy. Separating the
  admin plane from the data plane keeps "the parent can manage it" from becoming "the
  parent silently bypasses everyone else's trust rules."
- **Replacement, not fan-out:** one source of truth for shared memory; no
  write-splitting between a local and remote Archivist.
- **Reuses the substrate:** graduation is mostly *configuration + a trust registry +
  an admin surface* on top of machinery that already exists, not a new subsystem.
```

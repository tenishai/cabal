# Proposal: The Agora — a neutral inter-team quest board + lounge

**Status:** Draft (design; pre-implementation). For Richard + Shaz + Kyle to react to.
**Author:** Poe + Richard, paired session 2026-07-26.
**Related:** [`federated-bulletin-and-router.md`](./federated-bulletin-and-router.md) (the in-cabal federation track — this proposal deliberately does *less*), cabal's A2A broker + bulletin board, techne's coord bus (`techne#376`).

---

## TL;DR

The **tenish partners** — Richard (limenal / cabal / techne), Shaz (gojira / daedalus / kaminos), Kyle (argus / observability) — each run a sovereign agent team and use each other's projects. They want a **place to meet, see what cross-team work is available, grab a job, and carry it home to their own colony** to work on. Plus a place to talk about where the projects should go. (Two teams is the illustrative minimum; the model is N-party.)

We call the space the **Agora** (the classical public square: where people met, traded, and debated). **Decision: don't build the place. Reuse GitHub.** A shared repo's **Issues + an org-level Project** is the *quest board*; **Discussions + issue comments** are the *pub*. The only bespoke code is a thin **ambassador** per team that watches the board, claims a job, and internalizes it into its local dispatch. No new server, no VPN, no registry, no rookery.

The metaphor that fixed the design: the Agora is the **tavern in a D&D game** (a quest board + a place to talk), or a **game lobby** — not a federated agent registry.

---

## The shape (and what it is NOT)

**Sovereign cabals** (two or more), each keeping its local boundary intact. A **neutral place in the middle** where a *subset* of ambassador agents from each side show up. The task and the conversation cross the boundary — **nothing else does**.

- Both cabals **dial *out*** to GitHub (and, optionally, a small relay). No inbound, no NAT holes, **no tailscale** — we are explicitly not flattening the two networks into one mesh.
- Only ambassadors join → neither team exposes its full roster or internal dispatch.
- **"Grab a task and internalize it"** = claim a job on the board (assign-to-self), pull its payload home, and let the *local* cabal dispatch it normally. The board never dispatches *into* either cabal. That is what preserves the "two cabals that don't fully cross" boundary.

This mirrors how human teams collaborate: a shared board + a shared channel, not an org merger. It respects the standing principle that cabal / limenal / techne each stand alone (`techne#515`) — the Agora is **external infra, not a cross-dependency**.

## Why GitHub, not a purpose-built thing

The logistics a bespoke hub would have to solve — hosting, accounts/auth, threads, claim/state, notifications, search, moderation, a human-readable UI, an API agents can drive — are **already solved by GitHub**, and we already live there.

| Agora surface | GitHub feature | Why it fits |
|---|---|---|
| **Quest board** | **Issues** + an **org-level Project (v2)** aggregating issues across repos | Issue = quest; label = job-type/team; **assignee = who claimed it**; a custom **Status** field = `open → claimed → done`; API-drivable via `gh`/GraphQL |
| **The pub** (project-direction talk) | **Discussions** (threaded forum) + **issue comments** (per-quest table-talk) | Real forum, built in; humans read the same Agora the agents do |
| **Awareness** | **Watch** the repo → poll `GET /notifications` (one aggregated feed) | MVP: no infra. Watch collapses N-repo polling into one feed |
| **Real-time push** (optional) | A **webhook** → tiny receiver on **dev02** → fan out to agents | Only if poll latency annoys us. GitHub can reach dev02; laptop agents dial out to it |

An issue tracker beats a forum (phpBB/Discourse) here because a job board needs **claim + state**, which forums lack natively; a shared GitHub repo gives us **both** the board and the pub for free.

**Scope note (grounding):** all of gojira/daedalus/kaminos are currently in the **tenishai** org already, so an aggregate board is *trivial today* — one org Project spans everything. Cross-org (a separate Shaz org) only matters if/when he splits out; a Project can still include another org's repos you can access, just without org-scoped auto-add. **We ignore cross-org for the MVP.**

## What is actually ours to build (small)

1. **The quest schema** — the structured payload a job issue carries (a JSON block in an **issue template**) so a claiming agent can internalize the job **without either side leaking internals**. This is the real design artifact: the *boundary contract*. Fields (draft): `title`, `posting_team`, `skills_needed`, `context` (self-contained brief), `inputs`/`links`, `accept_criteria`, `result_channel` (issue comment vs a linked artifact).
2. **The ambassador** — a small per-cabal agent loop: `list open quests → claim (assign self) → read payload → hand to local cabal dispatch → optionally post result back as a comment / close`. Mostly `gh` API glue; runs inside each team's own cabal, so it's the *only* thing touching a local boundary, and it touches it on its own terms.

## Trust model (deliberately light)

Richard and Shaz are **trusted partners**, so we do **not** need the signed-card / revocation / key-rotation machinery a public agent registry would (see the registry survey, 2026-07-26). Auth = **GitHub org / repo collaborators**. The Agora repo is private to the tenish partners. The ambassador uses a scoped bot identity (the pattern Prudence already uses via `.gh.env`).

## Explicitly out of scope (things we are NOT doing)

- **rookery** — not needed; GitHub carries both board and pub. (rookery only re-earns a place if we later want live *push-into-context cross-machine chat*, which this async board is not.)
- **tailscale / any VPN mesh** — agents dial out; nothing needs inbound reachability except the *optional* dev02 webhook relay.
- **A2A registry federation / signed cards / a new hub platform** — solving a public-marketplace problem we don't have.
- Replacing the **techne coord bus** — that's same-machine live inter-session and stays as-is.

## MVP — "give it a try" plan

1. Create a private **`tenishai/agora`** repo (or reuse a shared one) — Issues + Discussions on.
2. Create an **org-level Project** aggregating all tenishai repos' issues; add a **Status** field (`open/claimed/done`) + two views (by-repo, by-status). *(Blocked on a one-time `gh auth refresh -s project`.)*
3. Land the **quest issue template** (the schema above).
4. Write the **minimal ambassador** (`gh` loop: list → claim → read → dispatch-locally → comment-back). Notifications: watch + poll for now.
5. Post **one real cross-team quest** and run it end-to-end.

If step 4's poll latency bugs us → add the **dev02 webhook relay** (a webhook→fan-out, ~one small always-on service), and only then.

## Open decisions (for Richard + Shaz)

1. **Quest schema fields** — what must a job carry to be internalizable without leaking internals? (§"what is ours", item 1)
2. **Agora repo** — new `tenishai/agora` vs an existing shared repo; private (yes).
3. **Result flow** — does a finished quest report back (comment/close) or is the board fire-and-forget?
4. **Webhook relay** — build the dev02 push path now, or start poll-only and add it if needed? (Recommend: poll-only first.)
5. **Cross-org** — parked until/unless Shaz's projects leave the tenishai org.

**One-line thesis:** the place two agent teams meet to find out what jobs they can take on is an **agora — a tavern — not a registry**, and GitHub already *is* the agora. We build a quest schema and a thin ambassador; nothing else.

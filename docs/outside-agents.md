# Admitting an outside agent

An outside agent is an A2A client that is not one of the room's agents: another
team's agent, or a one-off client. This is the operator procedure #271 decided
on 2026-10-05. The operator approves each outside agent by name and gives it one
token for each persona it may call.

## Why one token per persona

The room (kobo) owns the dispatch-pairs table (D-018 in `CABAL-DECISIONS.md`)
and builds it only from its own agents, so it cannot grant an outside agent a
pair. A token whose audience is the target persona does not need a pair:
`_dispatch_auth.dispatch_token` uses a token already aimed at its target as it
is. Each grant is therefore a token. Revoking the outsider takes every grant
back at once.

## Admit

1. Pick the outsider's name and the personas it may call. Only this cabal's
   own personas: a federated persona runs on a peer cabal, has no card here,
   and expects its peer's audience, so an outsider gets access to it from that
   peer's operator.

   The name must not be one of the room's agents' names, or another
   outsider's. The room's colleague passes use the token subject
   `agent:<name>`, the same form outsiders get, and task visibility and
   revocation are keyed on the subject. An outsider named like a room agent
   would see, and could cancel, the tasks that agent sent with its colleague
   pass to any persona: reading tasks checks the subject, not the token's
   audience (see "What the outsider can do"). Revoking the outsider
   would also revoke that agent's colleague pass. Use the `ext-` prefix, as
   below, to keep the two apart.

   Everything below uses each persona's bulletin name, written `<agent>`: a
   `role = "primus"` persona is `<persona>-primus` (for example
   `mason-primus`), and any other persona is its own name (for example
   `archivist`).
2. For each `<agent>`, mint a token (you need an operator login, `cbl login`):

   ```sh
   cbl token mint-service --sub agent:ext-<name> --scope dispatch --aud <agent> --ttl-days 90
   ```

   It prints the token on stdout, and its subject, scope, audience and expiry on
   stderr. `--ttl-days` defaults to 30 and is capped at 365 (or `[cabal] service_token_max_ttl_days`, if lower).
3. If `[guardrails] delegation_edges = true`, also give the outsider an edge to
   each persona it may call, keyed by its full subject:

   ```toml
   [guardrails.caller_edges]
   "agent:ext-<name>" = ["<persona>"]
   ```

   Without that line every SendMessage it sends is refused: the
   guardrail finds no `caller_edges` entry for its subject (this applies only when
   `delegation_edges = true`). GetTask, ListTasks and
   CancelTask do not dispatch, so edges do not apply to them. Edges are read at startup.
4. Give the outsider, out of band, each token together with that agent's card
   URL, `{public_url}/agents/<agent>/.well-known/agent-card.json`. The card
   names the endpoint, `{public_url}/agents/<agent>/a2a`. Each call sends the
   token as `Authorization: Bearer <token>` and the header `A2A-Version: 1.0`.

## What the outsider can do

On each persona it holds a token for, it can call SendMessage. SendMessage to
any other persona is refused (403), and `/directory` is refused too, so the
roster stays private.

GetTask, ListTasks and CancelTask check who the caller is, not the token's
audience. With any of its tokens, on any persona's path, the outsider sees and
can cancel only the tasks it sent to that persona, plus tasks it sent to a
`[groups]` group that includes that persona; on a persona it never sent to, it
finds none. Its tasks are not visible to any other outside agent. The
operator sees every task on a persona, through that persona's path.

## Revoke

Revocation is per subject, so it stops every token the outsider holds:

```sh
curl -X POST "$CABAL_URL/admin/token/revoke" \
  -H "Authorization: Bearer $OPERATOR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"sub": "agent:ext-<name>"}'
```

To take away one persona and keep the others, revoke the subject and mint new
tokens for the personas it keeps. The revocation cutoff is the start of the next
second, so a token minted after that works; `mint-service` waits out the
current second if it has to.

## Not covered here

- Self-service access, where an outsider asks to be let in: that would be the first
  job of the porter persona proposed in #293.
- OAuth2 client credentials and OIDC from trusted issuers: #271 left both for
  a later caller class, such as another organisation with its own identity
  provider.

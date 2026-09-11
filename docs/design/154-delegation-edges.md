# cabal#154 — delegation edges: the design, and what shipped

**Status: implemented.** This was written before the code, as the design record,
and is kept because the reasoning is worth more than the diff. The issue's three
open questions are answered in place below; the two that were left for Richard
are answered at the bottom.

Every code fact below was verified against a clean worktree at `b08862b`. The
gap itself was re-run at `825838a`, after #150 and #153 merged, and still stands:
one hit, still the outbound header, now at `router.py:587`.

## The gap, re-verified

The issue's own probe, re-run on this base:

```
grep -rniE "authoriz|permit|may_ask|can_ask|allowed_targets|visib|edge" src/cabal/core/broker/
b08862b → router.py:528:  headers={"Authorization": f"Bearer {caller_jwt}"}
825838a → router.py:587:  headers={"Authorization": f"Bearer {caller_jwt}"}
```

One hit, and it is the *outbound* header. There is no caller-to-target
authorization at dispatch. The issue's claim holds exactly as written.

## Q2 — does the primus fan-out need edges?

**No, and not because it is exempt — because the guard never sees it.**

`_dispatch_subordinate` (`agents/primus.py:363`) is in-process. It fires its own
`DoomLoopGuardrail` at the subordinate boundary, and `app.py:151-153` states the
reason the router-level chain exists at all: *"The in-process Mason
DoomLoopGuardrail (primus.py) fires at the subordinate boundary and never reaches
a remote hop."* The two paths do not meet.

So an edge check placed on the router's input-guardrail chain does not apply to
primus→subordinate at all. Requiring every implementer/reviewer pair to be
declared would add friction to a path the check cannot even observe.

**The relation is already declared config anyway.** `PersonaConfig.subordinates`
(`config/identity.py:179`) is a `dict[str, SubordinateConfig]`, operator-authored
in `identity.toml`. The parent-child edge exists today. It is not agent-supplied,
which is the property the acceptance criteria actually care about.

## Q1 — where do edges live?

**`identity.toml`, beside the persona, following the `allowed_handlers` precedent.**

`PersonaMCPConfig.allowed_handlers` already solves this exact shape, and has shipped:

```toml
[personas.archivist.mcp]
allowed_handlers = ["memory", "notify"]
```

Its documented semantics are three-state — omit or null means unrestricted, `[]`
revokes everything, a list is an allowlist — and the config comment is explicit
that the value "folds into `cabal_tool_extras[...]` at JWT-mint time; liminal
filters `tools/list` AND refuses `tools/call` accordingly." That is an
operator-authored, never-agent-supplied allowlist enforced at two points. It is
the right precedent and it is already understood by operators.

**But the default must invert, and that is the whole decision.** `allowed_handlers`
omitted means *unrestricted*. #154's acceptance criteria require the opposite:
"A room with no declared relationships permits no delegation." Shipping both
defaults in one file, one permissive and one restrictive, is a genuine
operator-comprehension hazard. Name the new key so it cannot be read as the
sibling of `allowed_handlers` — and state the asymmetry in the config comment
rather than hoping it is inferred.

## Q3 — migration

Every existing room has zero declared edges, so a strict default-deny turns all
delegation off at once, at upgrade, with no warning. The three options in the
issue are the right three. What the code adds:

Edge synthesis from observed traffic is cheap here. `Bookkeeper.on_dispatch_start`
already writes a `Conversation` row per dispatch with caller and target
(`bookkeeping.py:52-56`), so the observed adjacency list is already in the
database. A `cbl edges suggest` that reads it and emits a TOML block for the
operator to paste, review, and edit is a small amount of work and turns a
breaking upgrade into a reviewable diff.

**SHIPPED:** enforcement default-off behind `[guardrails] delegation_edges`,
mirroring how `doom_loop` is opted out. The guard's OWN default is deny — a
persona with no `may_delegate_to` may delegate to nobody — so the control is
default-deny where it belongs and the migration is the flag. Flipping the flag's
default is a later release, once real rooms carry edge blocks.

**NOT shipped:** `cbl edges suggest`. The adjacency is in `Conversation` rows
and the command is small, but nothing needs it until someone is ready to flip
the default. Filed rather than built.

## The requirement the issue does not state

**Capability routing must be edge-aware at selection, not only at the guard.**

`select_by_capabilities` (`router.py:184`) resolves a target from
`required_capabilities` when the caller names none. It is called at
`routes/tasks.py:132`, i.e. *before* `dispatch()`. So the guard is safe — by the
time `task.metadata["cabal_target_agent"]` is set at `router.py:256`, the target
is already a concrete name, and an edge check reading it sees the real target.

The failure is one of behaviour, not of enforcement. On this path the caller
never named anyone; it asked for a capability and the system chose. If selection
ignores edges, the system picks a target the caller may not reach and then
refuses its own choice — an unreachable-by-policy agent shadows a reachable one
that could have served the request.

Selection must filter to edge-reachable targets first, then match capabilities.
That is a change at `routes/tasks.py:132`, separate from the guardrail.

**DEFERRED to cabal#162, and the reason matters.** There is no authorization
hole: the guard sees the resolved target, so capabilities cannot be used to
reach a forbidden colleague. It is an ergonomics defect — the system picks a
target the caller may not reach and then refuses its own choice — plus a
shadowing case, where an unreachable agent sorting first by name hides a
reachable one that would have served the request. I said earlier this belonged
in the acceptance criteria; on implementing it, the fix needs the route layer to
know the caller's edges, which is a different layer and a design question of its
own. The security property is complete without it.

## Answered

1. **The default.** Richard took the recommendation: ship opt-in, flip later. The
   guard is default-deny internally; `[guardrails] delegation_edges = false` is
   the migration.
2. **Naming.** `may_delegate_to`, deliberately unlike `allowed_handlers` so the
   two do not read as a pair — because their defaults are opposites. Both the
   field comment and `identity.toml.example` state the asymmetry outright rather
   than leaving it to be inferred.

## One thing the implementation added that the design did not anticipate

The caller had to become identifiable on a **typed field**. `GuardrailContext`
carried only `persona_name`, which is `caller_persona or caller_agent or ""` —
it collapses an agent and a human operator into one display string, so an
authorization decision made from it would have rested on parsing an
`"operator:"` prefix back out of a fallback chain. `GuardrailContext` now
carries `caller_persona`, which is None exactly when the caller is the operator,
by construction: `routes/auth.py:61` branches on `persona_name is None` and it
is the only mint site producing a token with no `cabal_persona` claim.

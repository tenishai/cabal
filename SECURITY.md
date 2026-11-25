# Security policy

## Reporting a vulnerability

Please don't open a public issue for a security problem.

Report it privately to **security@tenish.ai**.

If this repository's **Security** tab shows a **Report a vulnerability**
button, you can use that instead. GitHub offers it only on public
repositories where it has been turned on.

Include what you can of:

- what the problem is and what an attacker could do with it;
- the version, commit, or tag you tested;
- steps to reproduce, or a proof of concept;
- any configuration it depends on.

You don't need a complete write-up to report. A clear description of the
problem is enough to start.

## What happens next

- We acknowledge your report within **14 days**.
- We tell you whether we've confirmed the problem as soon as we know.
- We fix confirmed problems in the next release, or sooner for serious ones,
  and tell you before the fix is public.
- We credit you in the [release notes](CHANGELOG.md) unless you'd rather we didn't.

We ask that you give us a reasonable chance to fix a problem before disclosing
it publicly, and that you don't access or change data that isn't yours while
testing.

## Supported versions

cabal is pre-1.0. Security fixes go into the **latest release** only; older
releases are not patched. If you're running an older version, upgrade to get
fixes.

## Scope

cabal orchestrates agents and issues the tokens they use. In scope:

- authentication and authorization: token minting, verification, revocation,
  audiences and scopes, and the admin routes;
- delegation: anything that lets an agent act as, or dispatch to, an agent it
  wasn't granted;
- the A2A and task endpoints: anything that lets one caller read or change
  another's tasks;
- the bootstrap routes: anything that yields a token from `/auth/bootstrap`
  or `/auth/liminal-token` that the caller wasn't meant to get;
- the secrets store: anything that exposes a stored secret or signing key,
  which are encrypted under `CABAL_MASTER_KEY`, to a caller not meant to see
  it. Secrets supplied as `CABAL_SECRET_*` environment variables override the
  store and are not encrypted.

**Out of scope:** vulnerabilities in third-party dependencies (please report
those upstream, and tell us if we ship an affected version), and problems that
require an attacker to already control the machine or the operator account
cabal runs under.

## Known limits

These are gaps we know about and have not closed. Each names its issue, what it
takes to use it, and what limits it today. Reports that go further than what
is written here are in scope.

**Write access to the database is operator-equivalent** (issue #346). cabal
keeps one SQLite file. It holds the signing keys and stored secrets, encrypted
under `CABAL_MASTER_KEY` (which is not in the file), and also, in plain form,
the task checkpoints, scheduled tasks' caller claims, the dispatch-pairs table
and the revocation list. A checkpoint's messages and tool calls are replayed
with no integrity check when the operator resumes a task (`POST
/admin/tasks/{id}/resume`), so whoever can write the file can make a persona
run a tool call of their choosing with that persona's credentials. A scheduled
task starts under the claims stored with it, so the same writer can start a
task with forged claims. Protect the file, its directory and its backups
(Litestream replicas included) as you protect `CABAL_MASTER_KEY`.

**Tool policy is not inherited on delegation** (issue #155). A persona runs
with its own `mcp.allowed_handlers`, whoever asked. An agent with a narrow
grant that may dispatch to a persona with a wider one reaches the wider
grant's tools by asking. It needs a dispatch route to that persona: a token
aimed at it, or a colleague pass with a pair to it (an empty pairs table
allows every pair). What limits it is the delegation-edge guardrail, which
refuses a delegation that has no operator-declared edge, but only when
`[guardrails] delegation_edges = true`; it is off by default. Size a
persona's grant as the union of everything it may delegate to.

**The re-minted dispatch token drops the caller's scope** (issue #164). Every
door re-mints with `scope=["dispatch"]`, so operator scope does not reach the
router, and the delegation-edge guardrail exempts the operator by the
`operator:` prefix of `sub` instead of by scope. A token with an `operator:`
sub is exempt from edges without holding operator scope. Minting one takes
operator power already: `/auth/bootstrap` and `/auth/renew` mint it,
`mint-service` refuses an `operator:` sub (#403), and otherwise it takes the
signing key. With edges off, nothing depends on it.

**A dispatched token names the caller, not the agent acting for it** (issue
#184). The re-mint keeps the caller's `sub` and carries no `act` claim. When
the operator dispatches to an agent, that agent's token says
`operator:<id>`; if the agent passes it to a service of its own, that service
cannot tell the operator's request from the agent's work on it. A service that
grants extra access on an `operator:` sub must not accept a token an agent
forwarded. Inside cabal a forwarded token reaches only the agent it was minted
for: its scope is `dispatch` and its audience that agent, so cabal will not
re-mint it for another target. An agent that calls a colleague with its own
colleague pass is named as itself (`agent:<name>`). A token with no `sub` is
refused, not re-minted as the operator.

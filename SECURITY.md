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
- We credit you in the release notes unless you'd rather we didn't.

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

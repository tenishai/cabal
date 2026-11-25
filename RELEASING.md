# Releasing cabal

A cabal release is a version bump in a pull request, a tag on the merge commit, a GitHub
release carrying the wheel, and the wheel on the tenish package index. All of it is done by
hand: there is no release workflow, and nothing in CI reacts to a tag. This file records what
0.4.0 and 0.5.0rc1 actually did.

Release order across the suite: techne first, then liminal, then cabal. cabal's `mcp`,
`memory` and `full` extras depend on liminal and techne, so both must be released (and
published) before the cabal release that needs them.

## When to cut a release

Cut one when it has a reason to exist: name who will install it and how. Don't cut one
because a fix just merged; a fix on main waits for the next release with a reason. A release candidate (`X.Y.ZrcN`, as 0.5.0rc1 was) comes first when the code
should run somewhere real before the final tag.

Before you tag, make sure the release will not need a patch the same session has to cut.
Resolve the downstream version set against the index, with every extra, using the candidate
version:

```
echo 'tenish-cabal[<every extra in pyproject.toml>]==X.Y.Z' > release.in   # plus the downstream set's other pins
uv pip compile --index tenish=https://pypi.tenish.ai/simple release.in
```

It must resolve with no conflict. Then install the
result where it is going and run it. The index asks for a token during the limited release.

## The release PR

One pull request, titled `release: cabal X.Y.Z`, as #388 (0.4.0) and #415 (0.5.0rc1) were. It
carries:

- `version` in `pyproject.toml` (setuptools backend; `cabal.__version__` reads the installed
  metadata, so this is the only place the number lives). The rc is written `0.5.0rc1`.
- `CHANGELOG.md`: rename `## [Unreleased]` to `## [X.Y.Z] — YYYY-MM-DD` and put a fresh empty
  `## [Unreleased]` above it. Add an `### Upgrading` section: migrations (they apply at
  startup), config changes, and what an operator must run.
- `requirements-sources.txt`: the techne and liminal tags a non-dev install picks up. The
  0.4.0 PR moved them (techne v0.18.2 to v0.19.0, liminal v0.5.0 to v0.7.1) and said so in
  Upgrading. The Dockerfile pins liminal and techne wheels by version and sha256
  (`LIMINAL_VERSION`, `LIMINAL_WHEEL_SHA256`); 0.4.0 left them as they were and said that too.
- Docs that name the version: the `STATE:` line in `ARCHITECTURE.md`, and any other file that
  names the previous one (grep for it).
- Architecture-card anchors. `ARCHITECTURE.md` and `docs/arch/liminal-boundary.md` anchor
  `pyproject.toml` by blob sha, so the bump makes them stale. Run
  `python3 scripts/check_arch_cards.py`, fix each anchor, and expect
  `all architecture cards fresh`.

Check `git log --oneline v<previous>..HEAD` against `[Unreleased]`
and add whatever is missing.

Run the full suite before you open the PR. The PR needs the `ci` and `dco` checks, one
review, and a squash merge. `pyproject.toml` is code, so Prudence reviews it through the
code-owner rule, and the Fates review a release PR as they do any other (#415 took a round of
review fixes).
Prudence is the project's AI code reviewer, the code owner for everything but docs. The Fates
are its review pipeline: Temperance checks the assertions a change adds, then Prudence
reviews correctness and approves or asks for changes.

## Tag the merge commit

A maintainer tags the squash commit of that PR on main, annotated:

```
git checkout main && git pull
git tag -a vX.Y.Z -m "cabal X.Y.Z"
git push origin vX.Y.Z
```

The rulesets allow only maintainers to create `v*` tags and forbid moving or deleting one. A
bad release is followed by a new version, never a retag. v0.4.0 points at the release commit
(`a5cf11d`). v0.5.0rc1 points at `09c5139`, two commits after the release commit `b969b05`;
do not repeat that, because the tag should name the commit whose `pyproject.toml` says the
version.

## Publish

After the tag, by hand:

1. **The GitHub release**, named `cabal X.Y.Z`, with the CHANGELOG section as its body
   (including `### Upgrading`). Mark a candidate as a pre-release. The wheel and a
   `SHA256SUMS` file are attached (v0.4.0 and v0.5.0rc1 have both).
2. **The wheel on `https://pypi.tenish.ai/simple`.** A maintainer builds the wheel from the tag twice, with
   `SOURCE_DATE_EPOCH` set to the tag commit's time and checks that the hashes match; publishes it with the
   index's publish script (maintainers only); checks the sha256 in the
   index metadata. The index is immutable, so a mistake is fixed by a new version, and an
   rc costs only an rc number.

Whether cabal 0.5.0rc1 is on the index is not recorded in this repo; check before you tell
anyone to install it.

## After

- `[Unreleased]` is already fresh from the PR.
- Version sets that pin exact component versions move in their own pull requests, after the
  wheel is on the index.
- Bump the Dockerfile's liminal and techne wheel pins only when the image needs a newer
  library, in their own PR, with the sha256 of the wheel on the index.

## Not yet

No tag-triggered release workflow, scan gate or SBOM exists here, unlike kobo's.

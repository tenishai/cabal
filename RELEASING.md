# Releasing cabal

A cabal release is a version bump in a pull request, a tag on the merge commit, the image
on ghcr, a GitHub release carrying the wheel, and the wheel on the tenish package index. The
tag starts `.github/workflows/release.yml`, which builds and publishes the image and creates
the GitHub release; the wheel is still done by hand. This file records what 0.4.0 and
0.5.0rc1 actually did, plus the image workflow added after 0.5.0.

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
- `k8s/cabal-core.yaml`: the `image:` tag moves to `vX.Y.Z`. `tests/test_release_image.py`
  fails until it equals the new `version`.
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

The tag starts `release.yml`. It runs the whole of `test.yml` on the tagged commit and,
on a GitHub arm64 runner (`ubuntu-24.04-arm`), builds the arm64 image and runs it: it must
report `aarch64`, the version, and a working `cbl --help`. Nothing is pushed until both pass.
That runner is free only while the repository is public. Then it checks
the tag against `pyproject.toml`, builds the image (techne from its pinned tag), checks the
image reports the version and has no fixable high or critical vulnerability (apart from what `.grype.yaml` lists), and publishes it
to `ghcr.io/tenishai/cabal` under `vX.Y.Z` and the short sha, for amd64 and arm64, with an
SBOM and provenance. It then creates the GitHub release from the CHANGELOG section if none
exists, attaches the image SBOM and its checksum, and writes the image digest into the text.
A failed run is re-run from the tag with `gh workflow run release.yml --ref vX.Y.Z`. If
the image for that tag is already on ghcr, the re-run does not build or push it again (a
rebuild would get a new digest under the same tag). It checks the published image, scans it
(reporting, not failing, since nothing would be pushed), makes its SBOM, and finishes the
GitHub release with its digest. The existence check fails the run on any answer from ghcr but
"found" or "not found", so a registry error can't pass for a missing tag.

Before the tag, dry-run the workflow from main: `gh workflow run release.yml --ref main`. It
runs `test.yml` and the arm64 job, builds the image, checks the version inside it, scans it, makes the SBOM, and
runs the existence check against `v<version>` with the release's token, and publishes
nothing. The scan depends on that day's vulnerability feed, so do this the same
day as the tag; a failure found on the tag means a new version, since a tag is never moved.
A scan failure has two answers: move the package to a fixed version, or, when the vulnerable
code can't be reached in the image, add an entry to `.grype.yaml` that names the advisory, the
package and its exact version, with the reason and when to remove it (grype drops an ignored
match from its table, so that file is the only record).

The first time the workflow publishes, the package is private: an org owner sets
`ghcr.io/tenishai/cabal` to public in the package settings, or `k8s/` cannot pull it.

Then, by hand:

1. **The GitHub release.** Rename it `cabal X.Y.Z`, mark a candidate as a pre-release, and
   attach the wheel and its `SHA256SUMS` (v0.4.0 and v0.5.0rc1 have both). Keep the image
   line the workflow wrote.
2. **The wheel on `https://pypi.tenish.ai/simple`.** A maintainer builds the wheel from the tag twice, with
   `SOURCE_DATE_EPOCH` set to the tag commit's time and checks that the hashes match; publishes it with the
   index's publish script (maintainers only); checks the sha256 in the
   index metadata. The index is immutable, so a mistake is fixed by a new version, and an
   rc costs only an rc number.

Whether cabal 0.5.0rc1 is on the index is not recorded in this repo; check before you tell
anyone to install it.

## Moving the LiteLLM pin

litellm is pinned exactly in `pyproject.toml`, and the LiteLLM Proxy image by digest in
`docker-compose.yml` and `k8s/litellm.yaml`, all at one version (`tests/test_litellm_pin.py`).
LiteLLM shipped a credential stealer in 1.107.0 (2026-10-21), so a move is a supply-chain
review, in its own PR:

- Use a release from LiteLLM's post-incident workflow that has been out for a few days. Check
  that its wheel has a PyPI attestation from `BerriAI/project-releaser`:
  `curl -s https://pypi.org/integrity/litellm/<v>/litellm-<v>-cp310-abi3-manylinux_2_28_x86_64.whl/provenance`
- Verify the image's keyless signature before taking its digest:
  `cosign verify --certificate-identity https://github.com/BerriAI/project-releaser/.github/workflows/release.yml@refs/heads/main --certificate-oidc-issuer https://token.actions.githubusercontent.com ghcr.io/berriai/litellm:v<v>`
- Take the digest with `docker buildx imagetools inspect ghcr.io/berriai/litellm:v<v>`, and
  move all three lines together.

## After

- `[Unreleased]` is already fresh from the PR.
- Version sets that pin exact component versions move in their own pull requests, after the
  wheel is on the index.
- Bump the Dockerfile's liminal and techne wheel pins only when the image needs a newer
  library, in their own PR, with the sha256 of the wheel on the index.

## Not yet

The wheel is built and published by hand, and the image carries no liminal (an identity
that sets `[liminal] url` needs a build with `--build-context liminal=…` or `WITH_MCP=1`).

# Contributing to cabal

## Getting a change in

cabal's tests that touch techne or liminal want those two projects checked out
next to it. The unit suite does not need them; sibling tests skip themselves.

```bash
mkdir tenishai && cd tenishai
git clone https://github.com/tenishai/cabal.git
git clone https://github.com/tenishai/techne.git      # optional for unit tests
git clone https://github.com/tenishai/liminal.git     # optional for unit tests
cd cabal
python3 -m venv .venv && source .venv/bin/activate    # Debian/Ubuntu: sudo apt install python3-venv first
pip install -e ../techne -e ../liminal                # skip both for the unit suite alone
pip install -e ".[dev]"
pytest -q -m "not integration"   # the unit suite, as CI's `unit` job runs it
pytest -q                        # everything; needs the siblings above
ruff check src tests
```

Then: branch, commit with `git commit -s` (see [Developer Certificate of
Origin](#developer-certificate-of-origin)), open a PR. Keep the suite green: CI
runs ruff and the unit suite on Python 3.12 and 3.13 for every push and pull request.

Everyone taking part in this project is expected to follow the
[Code of Conduct](CODE_OF_CONDUCT.md). Report security problems privately, as
[SECURITY.md](SECURITY.md) describes, not in a public issue.

A few things worth knowing before you spend effort:

- **Tests live in `tests/`**, and a behaviour change wants a test that fails
  without it. Revert your change and watch the test fail; that is the cheap way
  to know it tests what you think.
- **`mypy src`** is configured in `pyproject.toml` and has no CI job; run it on
  the files you touch.
- **AGENTS.md** is the context file for AI coding agents, and `CLAUDE.md` is a
  one-line bridge to it. A rule an agent must follow belongs there, not only here.
  It also lists the repo's traps (for example, where the dispatch-token policy
  lives, and why a route must not grow its own copy).
- **Alembic migrations ship as package data.** Adding a revision under
  `src/cabal/migrations/versions/` is also a packaging change. So do the guides in
  `src/cabal/guides/`: a new one needs its `MANIFEST` entry.
- **`tests/_vendor` holds a copy of liminal's `protocol` package** for the unit suite.
  When liminal's `protocol` changes, re-copy it and bump `VENDORED_FROM` in
  `tests/_vendor/liminal/__init__.py`; `tests/test_vendored_liminal_protocol.py` fails
  against an installed liminal otherwise.
- **Docker.** `make up` builds the dev stack from sibling checkouts of techne and
  liminal and needs `docker buildx`; see the README.

Issues are the place for design discussion. A PR that changes behaviour is easier to
review when it says what it measured, not only what it intends.

## On macOS, put the GNU tools first

The scripts here, and the commands in this repo's docs, are written for the GNU versions of `sed`, `awk`, `grep`, `find`, `xargs`, `tar`, `date`, `stat`, `readlink`, `env` and `diff`, as on Linux. The BSD versions that ship with macOS take different flags. A common case is `sed -i`: BSD needs a backup suffix after it, GNU doesn't. Some commands fail outright, and some print something else and carry on. Install the GNU tools and put them ahead of `/usr/bin` on your `PATH`.

With MacPorts:

```bash
sudo port install coreutils findutils gsed gawk grep gnutar diffutils
export PATH="/opt/local/libexec/gnubin:$PATH"    # in ~/.profile or ~/.zshrc
```

With Homebrew:

```bash
brew install coreutils findutils gnu-sed gawk grep gnu-tar diffutils
for f in coreutils findutils gnu-sed gawk grep gnu-tar; do
  PATH="$(brew --prefix)/opt/$f/libexec/gnubin:$PATH"
done
export PATH                                      # in ~/.profile or ~/.zshrc
```

Check: `sed --version` should print `sed (GNU sed)`. Re-open your shell, or source the profile, after changing `PATH`.

## Developer Certificate of Origin

cabal uses the [Developer Certificate of Origin](https://developercertificate.org/)
(DCO) instead of a contributor license agreement. Every commit in a pull request
must carry a `Signed-off-by:` line with your name and email, and they must match
the commit's author. `git commit -s` adds it from your `user.name` and
`user.email`:

```
Signed-off-by: Jane Doe <jane@example.com>
```

By adding that line you certify the following, quoted from
<https://developercertificate.org/>:

```
Developer Certificate of Origin
Version 1.1

Copyright (C) 2004, 2006 The Linux Foundation and its contributors.

Everyone is permitted to copy and distribute verbatim copies of this
license document, but changing it is not allowed.


Developer's Certificate of Origin 1.1

By making a contribution to this project, I certify that:

(a) The contribution was created in whole or in part by me and I
    have the right to submit it under the open source license
    indicated in the file; or

(b) The contribution is based upon previous work that, to the best
    of my knowledge, is covered under an appropriate open source
    license and I have the right under that license to submit that
    work with modifications, whether created in whole or in part
    by me, under the same open source license (unless I am
    permitted to submit under a different license), as indicated
    in the file; or

(c) The contribution was provided directly to me by some other
    person who certified (a), (b) or (c) and I have not modified
    it.

(d) I understand and agree that this project and the contribution
    are public and that a record of the contribution (including all
    personal information I submit with it, including my sign-off) is
    maintained indefinitely and may be redistributed consistent with
    this project or the open source license(s) involved.
```

CI checks every non-merge commit between the pull request's base and head, and
fails naming each commit whose sign-off is missing or doesn't match its author.
To fix it:

```bash
# the last commit only
git commit --amend --signoff --no-edit

# every commit on your branch
git rebase --signoff origin/main

# sign-off present but under a different name or email than the author:
# set user.name / user.email to the identity you want, then
git commit --amend --reset-author --signoff --no-edit

git push --force-with-lease
```

Commits already on `main` from before this rule are not re-checked; the check
covers the commits a pull request adds. Commits authored by Dependabot are
exempt from the sign-off requirement — Dependabot signs off as `dependabot[bot]
<support@github.com>`, which never matches its noreply author email — but only
for same-repo dependency-update PRs; the check stays strict on PRs from forks.

## Using AI tools

AI-assisted contributions are welcome; we build this way ourselves. You are
responsible for every line you submit: understand it, test it, and be able to
explain it in review. Say in the pull request description which AI tools you
used. Crediting the agent with a `Co-Authored-By:` trailer is optional. Don't
submit code a tool reproduced from a source whose license isn't compatible with
the MIT license.

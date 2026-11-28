# syntax=docker/dockerfile:1.7
# Multi-stage build for Cabal Core.

# ----- techne: a sibling checkout, or a package index -------------------------
# Two ways in, same image either way:
#   --build-context techne=../techne                       (compose, dev: unchanged)
#   --build-arg EXTRA_INDEX_URL=https://<index>/simple/    (a package index)
#     [--secret id=ar_token,env=AR_TOKEN]  for a private Artifact Registry
#     index: a BuildKit secret, mounted in the one RUN that needs it. Never a
#     build arg: those are printed in the build log and kept in the build
#     stage's metadata and cache (cabal#250).
# techne itself is fetched from that index ALONE, then installed as a file, so
# only its dependencies come from PyPI. The name `techne` on PyPI belongs to
# someone else (0.0.2 as of 2026-10-01); with the index as an *extra* index,
# pip would take whichever index offers the highest version, and a later
# release there would land in this image.
# From the index, techne is pinned to one wheel by version AND sha256
# (TECHNE_VERSION, TECHNE_WHEEL_SHA256). Anything with write access to the
# index can publish a file under a version number, so the version alone names
# nothing: on 2026-10-02 the index's 0.18.0 turned out to be a build from
# before the v0.18.0 tag, 16 package files behind it. When this step runs, a
# wheel whose hash does not match stops the build. A warm build cache can skip
# the step and reuse the layer an earlier run built, so the check runs on a
# cold build or after the args change. Moving to a new techne means changing
# both args together, to a wheel built from that version's tag.
# The arg is not called PIP_EXTRA_INDEX_URL on purpose: pip reads that name
# from the environment, and a build ARG is environment for every later RUN of
# the stage, so the second install would stop to ask for a password.
# `--build-context techne=` overrides the empty stage below by name. Without
# it the stage is empty, the copy yields nothing, and the install falls
# through to the index. Neither given: the build stops and says so.
FROM scratch AS techne

# ----- liminal: a sibling checkout, optional ---------------------------------
# cabal imports liminal for its MCP tool proxy, and startup fails fast
# ("the liminal package is not installed", cabal#126) when identity.toml sets
# `[liminal] url` without it. The dev and production compose files and the
# example identity all set that URL, so both compose files pass
#   --build-context liminal=../liminal
# and the package lands in the image. Without the context this stage is empty
# and the image has no liminal: right for an identity with no `[liminal]`
# block, and a startup error naming the missing package for one that has it.
# Without a sibling checkout, --build-arg WITH_MCP=1 (with EXTRA_INDEX_URL)
# fetches the pinned liminal wheel from the index instead.
# The release image (release.yml) is built with a checkout of liminal at the
# LIMINAL_VERSION tag below, so the published image carries liminal.
FROM scratch AS liminal

# ----- Build stage ----------------------------------------------------------
FROM python:3.12-slim AS build

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Build deps for cryptography (cffi); slim image lacks them.
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        libffi-dev \
        && rm -rf /var/lib/apt/lists/*

WORKDIR /build

# See CABAL-DECISIONS D-008/D-012 for why techne is a build input. When it
# lands on PyPI, drop the techne stage and the install branch below and add a
# `tenish-techne>=` pin at the current TECHNE_VERSION to pyproject.toml's
# dependencies.
COPY --from=techne / /build/techne
COPY --from=liminal / /build/liminal
ARG EXTRA_INDEX_URL=""
# techne v0.20.0 (tag at 773f7e4), the py3-none-any wheel; the hash is the one
# in that release's SHA256SUMS asset. From 0.18.2 the distribution is
# tenish-techne; the import package is still techne.
ARG TECHNE_VERSION=0.20.0
ARG TECHNE_WHEEL_SHA256=1e9371c5fa89351da16a3e703cf0178a79eb250dc87fd732b05c2d4badb24b7b
# A sibling checkout must report TECHNE_VERSION too. That catches a checkout of
# another release; it cannot catch techne's main between releases, which reports
# the last released version. scripts/check-techne-sibling.sh compares commits
# (`make build` / `make up` run it). TECHNE_ANY_VERSION=1 skips this check, for
# building against whatever the sibling has checked out.
ARG TECHNE_ANY_VERSION=""
# liminal from the index too, when the image must carry the [mcp] extra
# (identity.toml sets `[liminal] url`, cabal#126): --build-arg WITH_MCP=1.
# Fetched from the index ALONE and pinned, for the same name-squatting
# reason as techne; its own dependencies are public names and come from
# PyPI. A sibling checkout (--build-context liminal=…) still wins.
ARG WITH_MCP=""
# liminal v0.11.0 (tag at 49bf892). The wheel builds reproducibly from the tag
# with SOURCE_DATE_EPOCH set to the tag commit's time; this is that build's hash,
# and the wheel attached to the GitHub release has it (its SHA256SUMS asset). Upload that asset to your
# index rather than rebuilding: a build without SOURCE_DATE_EPOCH has the same
# contents but a different hash. An index that already holds another build can
# pass its hash with --build-arg LIMINAL_WHEEL_SHA256=<sha256>.
ARG LIMINAL_VERSION=0.11.0
ARG LIMINAL_WHEEL_SHA256=fead5418e0d7e1e4c8ea86e7059b768b23f1c5ab0006723bbdfefb83dba532ab

# Layer-cache: copy pyproject.toml first, install all deps (and a stub package
# so the build system is satisfied), then copy the real source last so a
# code-only change doesn't bust the dependency install layer.
COPY pyproject.toml README.md LICENSE ./
RUN --mount=type=secret,id=ar_token \
    python3 -m venv /venv \
 && if [ -f /build/techne/pyproject.toml ]; then \
      sib="$(sed -n 's/^version = "\(.*\)"/\1/p' /build/techne/pyproject.toml | head -1)"; \
      if [ "$sib" != "$TECHNE_VERSION" ] && [ "$TECHNE_ANY_VERSION" != "1" ]; then \
        echo "techne: the sibling checkout is $sib, but TECHNE_VERSION pins $TECHNE_VERSION. Check out v$TECHNE_VERSION, or pass --build-arg TECHNE_ANY_VERSION=1." >&2; exit 1; \
      fi \
      && /venv/bin/pip install --no-cache-dir /build/techne; \
    elif [ -n "$EXTRA_INDEX_URL" ]; then \
      if [ -r /run/secrets/ar_token ] && [ -s /run/secrets/ar_token ]; then AR_TOKEN="$(cat /run/secrets/ar_token)"; EXTRA_INDEX_URL="$(echo "$EXTRA_INDEX_URL" | sed "s#://#://oauth2accesstoken:${AR_TOKEN}@#")"; unset AR_TOKEN; fi; \
      /venv/bin/pip download --no-cache-dir --no-deps --only-binary :all: --index-url "$EXTRA_INDEX_URL" -d /tmp/techne-dist "tenish-techne==${TECHNE_VERSION}" \
      && echo "${TECHNE_WHEEL_SHA256}  /tmp/techne-dist/tenish_techne-${TECHNE_VERSION}-py3-none-any.whl" | sha256sum -c - \
      && /venv/bin/pip install --no-cache-dir /tmp/techne-dist/tenish_techne-${TECHNE_VERSION}-py3-none-any.whl \
      && rm -rf /tmp/techne-dist; \
    else \
      echo "techne: pass --build-context techne=../techne or --build-arg EXTRA_INDEX_URL=… (see the techne stage)" >&2; exit 1; \
    fi \
 && if [ -f /build/liminal/pyproject.toml ]; then \
      /venv/bin/pip install --no-cache-dir /build/liminal; \
    elif [ "$WITH_MCP" = "1" ] && [ -n "$EXTRA_INDEX_URL" ]; then \
      /venv/bin/pip download --no-cache-dir --no-deps --only-binary :all: --index-url "$EXTRA_INDEX_URL" -d /tmp/liminal-dist "tenish-liminal==${LIMINAL_VERSION}" \
      && echo "${LIMINAL_WHEEL_SHA256}  /tmp/liminal-dist/tenish_liminal-${LIMINAL_VERSION}-py3-none-any.whl" | sha256sum -c - \
      && /venv/bin/pip install --no-cache-dir /tmp/liminal-dist/tenish_liminal-${LIMINAL_VERSION}-py3-none-any.whl \
      && rm -rf /tmp/liminal-dist; \
    elif [ "$WITH_MCP" = "1" ]; then \
      echo "liminal: WITH_MCP=1 needs --build-context liminal=../liminal or --build-arg EXTRA_INDEX_URL=…" >&2; exit 1; \
    fi \
 && mkdir -p src/cabal && touch src/cabal/__init__.py \
 && /venv/bin/pip install --no-cache-dir . \
 && rm -rf src build

# MIGRATIONS ARRIVE VIA THE PACKAGE, NOT VIA A COPY (cabal#79). They live at
# src/cabal/migrations and ship as package-data, so `COPY src ./src` carries
# them and the `pip install` below puts them in /venv alongside the code. The
# two lines that used to copy `migrations/` and `alembic.ini` from the repo
# root are gone because neither path exists any more -- and a COPY with an
# absent source fails the build outright rather than skipping.
COPY src ./src
# rm -rf build: the dep-caching install above leaves setuptools artifacts
# built from the stub package; the stub __init__.py is newer by mtime than
# the real COPY'd source, so a stale build/ dir ships an empty cabal
# __init__ (no __version__) and the daemon dies at import.
#
# NOTE the two COPY lines that used to sit above this (`migrations`,
# `alembic.ini`) are gone -- #79 moved migrations under src/cabal/ and ships
# them as package-data, so `COPY src ./src` carries them and this install puts
# them in /venv. Keeping them here would fail the build outright, the source
# paths no longer existing.
RUN rm -rf build && /venv/bin/pip install --no-cache-dir --no-deps . \
 && /venv/bin/python -c "import cabal; assert cabal.__version__"

# ----- Runtime stage --------------------------------------------------------
FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    CABAL_DB_URL="sqlite+aiosqlite:////data/cabal.db" \
    CABAL_DATA_DIR="/data" \
    PATH="/venv/bin:$PATH" \
    VIRTUAL_ENV="/venv"
# NO CABAL_ALEMBIC_* OVERRIDES, and dropping them is load-bearing rather than
# tidy (#79 review, Critical). `_migrations_dir()` consults the override FIRST
# and returns it unconditionally, so leaving CABAL_ALEMBIC_MIGRATIONS pointed
# at /app/migrations would keep the container on a path the package no longer
# fills -- it would never reach the importlib.resources resolution at all, and
# would die at `upgrade head` with "Path doesn't exist". The override exists
# for a deployment that genuinely needs to relocate them; this one does not.

# The upgrade pass pulls base-image packages (libc6, zlib1g, libpcre2, ...)
# up to current Debian security revisions WHEN THIS LAYER REBUILDS. Docker
# caches a RUN step by its parent layer and its text, not by what it downloads,
# so a plain rebuild on a warm builder reuses the last upgrade and goes stale
# exactly like the base layer. APT_REFRESH breaks that: pass a new value, e.g.
# `--build-arg APT_REFRESH=$(date +%F)`, and the upgrade runs again.
# `make build` / `make up` export the date (compose passes it and pulls the
# base), and CI's docker job does the same. A bare `docker compose build`
# without APT_REFRESH set reuses the cached layer.
ARG APT_REFRESH=unset
RUN echo "apt refresh: ${APT_REFRESH}" && apt-get update && apt-get -y upgrade && apt-get install -y --no-install-recommends \
        curl \
        ca-certificates \
        && rm -rf /var/lib/apt/lists/*

# Non-root user for the daemon. UID 10001 matches our k8s manifests.
RUN groupadd --gid 10001 cabal && \
    useradd --uid 10001 --gid cabal --home-dir /home/cabal --create-home cabal

COPY --from=build /venv /venv

WORKDIR /app

# Data + state live on a mounted volume.
RUN mkdir -p /data && chown -R cabal:cabal /data /app
VOLUME ["/data"]

USER cabal

EXPOSE 7400

HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://127.0.0.1:7400/healthz || exit 1

# Identity file is mounted in from compose; CABAL_MASTER_KEY is required.
CMD ["cbl", "daemon", "up", "--host", "0.0.0.0", "--port", "7400", "--config", "/app/identity.toml"]

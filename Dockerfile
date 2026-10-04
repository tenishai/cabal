# syntax=docker/dockerfile:1.7
# Multi-stage build for Cabal Core.

# ----- techne: a sibling checkout, or a package index -------------------------
# Two ways in, same image either way:
#   --build-context techne=../techne                       (compose, dev: unchanged)
#   --build-arg EXTRA_INDEX_URL=https://<index>/simple/    (a package index)
#     [--build-arg AR_TOKEN=…]  for a private Artifact Registry index; the
#     token Gojira's generated CI passes to every build (2026-09-30).
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
# `techne>=0.18.0` pin to pyproject.toml's dependencies.
COPY --from=techne / /build/techne
COPY --from=liminal / /build/liminal
ARG EXTRA_INDEX_URL=""
ARG AR_TOKEN=""
# techne v0.18.0 (tag at 87bdcf1), the py3-none-any wheel.
ARG TECHNE_VERSION=0.18.0
ARG TECHNE_WHEEL_SHA256=7fb9ec7163c08ea9ab04a1492f2dcd73afaf8411aeaf877a0ee36fc71cd42433
# A sibling checkout must report TECHNE_VERSION too. That catches a checkout of
# another release; it cannot catch techne's main between releases, which reports
# the last released version. scripts/check-techne-sibling.sh compares commits
# (`make build` / `make up` run it). TECHNE_ANY_VERSION=1 skips this check, for
# building against whatever the sibling has checked out.
ARG TECHNE_ANY_VERSION=""

# Layer-cache: copy pyproject.toml first, install all deps (and a stub package
# so the build system is satisfied), then copy the real source last so a
# code-only change doesn't bust the dependency install layer.
COPY pyproject.toml README.md LICENSE ./
RUN python3 -m venv /venv \
 && if [ -f /build/techne/pyproject.toml ]; then \
      sib="$(sed -n 's/^version = "\(.*\)"/\1/p' /build/techne/pyproject.toml | head -1)"; \
      if [ "$sib" != "$TECHNE_VERSION" ] && [ "$TECHNE_ANY_VERSION" != "1" ]; then \
        echo "techne: the sibling checkout is $sib, but TECHNE_VERSION pins $TECHNE_VERSION. Check out v$TECHNE_VERSION, or pass --build-arg TECHNE_ANY_VERSION=1." >&2; exit 1; \
      fi \
      && /venv/bin/pip install --no-cache-dir /build/techne; \
    elif [ -n "$EXTRA_INDEX_URL" ]; then \
      if [ -n "$AR_TOKEN" ]; then EXTRA_INDEX_URL="$(echo "$EXTRA_INDEX_URL" | sed "s#://#://oauth2accesstoken:${AR_TOKEN}@#")"; fi; \
      /venv/bin/pip download --no-cache-dir --no-deps --only-binary :all: --index-url "$EXTRA_INDEX_URL" -d /tmp/techne-dist "techne==${TECHNE_VERSION}" \
      && echo "${TECHNE_WHEEL_SHA256}  /tmp/techne-dist/techne-${TECHNE_VERSION}-py3-none-any.whl" | sha256sum -c - \
      && /venv/bin/pip install --no-cache-dir /tmp/techne-dist/techne-${TECHNE_VERSION}-py3-none-any.whl \
      && rm -rf /tmp/techne-dist; \
    else \
      echo "techne: pass --build-context techne=../techne or --build-arg EXTRA_INDEX_URL=… (see the techne stage)" >&2; exit 1; \
    fi \
 && if [ -f /build/liminal/pyproject.toml ]; then \
      /venv/bin/pip install --no-cache-dir /build/liminal; \
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

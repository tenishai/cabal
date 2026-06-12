# syntax=docker/dockerfile:1.7
# Multi-stage build for Cabal Core.

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

# techne is brought in via BuildKit `--build-context techne=../techne`
# (set by docker-compose.yml's additional_contexts). See CABAL-DECISIONS D-008/D-012.
# When techne lands on PyPI, drop both COPY+install lines and add a `techne>=0.6.0`
# pin to pyproject.toml's dependencies — then `pip install .` brings it in for free.
COPY --from=techne / /build/techne

# Layer-cache: copy pyproject.toml first, install all deps (and a stub package
# so the build system is satisfied), then copy the real source last so a
# code-only change doesn't bust the dependency install layer.
COPY pyproject.toml README.md LICENSE ./
RUN python3 -m venv /venv \
 && /venv/bin/pip install --no-cache-dir /build/techne \
 && mkdir -p src/cabal && touch src/cabal/__init__.py \
 && /venv/bin/pip install --no-cache-dir . \
 && rm -rf src

COPY src ./src
COPY migrations ./migrations
COPY alembic.ini ./
RUN /venv/bin/pip install --no-cache-dir --no-deps .

# ----- Runtime stage --------------------------------------------------------
FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    CABAL_DB_URL="sqlite+aiosqlite:////data/cabal.db" \
    PATH="/venv/bin:$PATH" \
    VIRTUAL_ENV="/venv" \
    CABAL_ALEMBIC_INI="/app/alembic.ini" \
    CABAL_ALEMBIC_MIGRATIONS="/app/migrations"

RUN apt-get update && apt-get install -y --no-install-recommends \
        curl \
        ca-certificates \
        && rm -rf /var/lib/apt/lists/*

# Non-root user for the daemon. UID 10001 matches our k8s manifests.
RUN groupadd --gid 10001 cabal && \
    useradd --uid 10001 --gid cabal --home-dir /home/cabal --create-home cabal

COPY --from=build /venv /venv
COPY --from=build /build/migrations /app/migrations
COPY --from=build /build/alembic.ini /app/alembic.ini

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

# syntax=docker/dockerfile:1
#
# Superset 6.1.0 for development and the acceptance tests (docker-compose.yaml): upstream's image
# with the frontend patches in patches/, plus the Postgres driver for the Warehouse database.
# Taken from superset-git-plugin's superset/ (see README.md).
#
# This folder is the whole build context; it needs nothing else from the
# repository. Every patch is a frontend change, so the image is upstream's,
# with only its built frontend assets (superset/static/assets) replaced:
#
#   1. Fetch superset-frontend/ at the commit upstream's image was built
#      from (its "sha" label).
#   2. Apply patches/*.patch in name order. A patch that doesn't apply
#      fails the build.
#   3. Build the frontend as upstream's Dockerfile does.
#   4. Swap the built assets into apache/superset:6.1.0, and add psycopg2.
#
# Python, the superset CLI, the user (superset, uid 1000), entrypoints and
# config handling are upstream's, untouched.

# Upstream's image and the commit it was built from. They go together:
# change neither without the other, and check the patches still apply.
ARG SUPERSET_IMAGE=apache/superset:6.1.0@sha256:16b50bbef6648912a79e3293d418fefa743ce555d36c5af6b85d859119ed7f88
ARG SUPERSET_COMMIT=c83fb2bb1dcfac41ac51bcebd82471f4a7180d18

######################################################################
# The frontend, patched, built as upstream's superset-node stage does.
# Same Node image as upstream's 6.1.0 Dockerfile. The assets don't
# depend on the platform, so this stage runs natively.
######################################################################
FROM --platform=$BUILDPLATFORM node:20-trixie-slim AS frontend

# git to fetch the source and apply patches; the rest are for node-gyp,
# as upstream installs them.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates git build-essential python3 zstd \
    && rm -rf /var/lib/apt/lists/*

# Only superset-frontend/: a sparse, shallow fetch of the pinned commit.
ARG SUPERSET_COMMIT
WORKDIR /app
RUN git init -q . \
    && git remote add origin https://github.com/apache/superset.git \
    && git sparse-checkout set superset-frontend \
    && git fetch -q --depth 1 --filter=blob:none origin "$SUPERSET_COMMIT" \
    && git -c advice.detachedHead=false checkout -q FETCH_HEAD \
    && test "$(git rev-parse HEAD)" = "$SUPERSET_COMMIT"

WORKDIR /app/superset-frontend

# Dependencies before patches, so changing a patch keeps this layer.
RUN --mount=type=cache,target=/root/.npm \
    --mount=type=cache,target=/root/.cache \
    npm ci

COPY patches/ /patches/
RUN set -e; \
    cd /app; \
    for patch in /patches/*.patch; do \
        echo "Applying $(basename "$patch")"; \
        git apply --verbose "$patch"; \
    done

# Upstream's settings for this build: production, no translations
# (upstream's image builds them separately, and we keep its own).
# webpack writes to ../superset/static/assets.
ENV BUILD_TRANSLATIONS=false \
    PUPPETEER_SKIP_CHROMIUM_DOWNLOAD=true
RUN mkdir -p /app/superset/static/assets /app/superset/translations \
    && npm run build

######################################################################
# Upstream's image with the patched assets.
######################################################################
FROM ${SUPERSET_IMAGE}

USER root
# Replace the directory outright, so none of upstream's hashed bundles
# linger beside ours. Owned by root, as upstream's are.
RUN rm -rf /app/superset/static/assets
COPY --from=frontend /app/superset/static/assets /app/superset/static/assets
# The Postgres driver, for the Warehouse database (the postgres service).
RUN uv pip install --python /app/.venv/bin/python --no-cache psycopg2-binary
USER superset

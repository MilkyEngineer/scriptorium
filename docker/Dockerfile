# syntax=docker/dockerfile:1.7
#
# One image, two entrypoints: `serve` (the MCP server, default) and `worker`.
# Build from the repository root:
#   docker buildx build -f docker/Dockerfile --platform linux/amd64,linux/arm64 .
# Runs as a non-root user with a read-only root filesystem and a tmpfs on /tmp;
# all state lives under /data.

ARG PYTHON_IMAGE=python:3.12-slim
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.13.1

FROM ${UV_IMAGE} AS uv

# ---- builder: resolve the locked environment into /app/.venv -------------------------------
FROM ${PYTHON_IMAGE} AS builder

# git fetches the pinned remarkable-mcp fork, a git dependency in uv.lock. Builder only.
RUN apt-get update \
 && apt-get install -y --no-install-recommends git ca-certificates \
 && rm -rf /var/lib/apt/lists/*

COPY --from=uv /uv /usr/local/bin/uv

# Compile bytecode now: the runtime root filesystem is read-only.
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PYTHON=/usr/local/bin/python3.12 \
    UV_PROJECT_ENVIRONMENT=/app/.venv

WORKDIR /src

# Dependencies first, so source edits do not invalidate this layer.
COPY pyproject.toml uv.lock README.md LICENSE ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project

COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable

# ---- runtime ---------------------------------------------------------------------------------
FROM ${PYTHON_IMAGE} AS runtime

# The default user is uid/gid 1000; set PUID/PGID to match the owner of /data on the host.
ARG PUID=1000
ARG PGID=1000

# System libraries for WeasyPrint (Markdown/HTML to PDF, Architecture §9) and fonts.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
    # Pango: WeasyPrint's text layout engine
    libpango-1.0-0 \
    # Pango's FreeType/fontconfig backend, which finds the installed fonts
    libpangoft2-1.0-0 \
    # HarfBuzz subsetting: embeds only the glyphs used in generated PDFs
    libharfbuzz-subset0 \
    # DejaVu: default Latin, Greek and Cyrillic fonts
    fonts-dejavu-core \
    # Noto CJK: Chinese, Japanese and Korean glyphs for pushed documents
    fonts-noto-cjk \
 && rm -rf /var/lib/apt/lists/*

RUN (getent group "${PGID}" >/dev/null || groupadd --gid "${PGID}" scriptorium) \
 && (getent passwd "${PUID}" >/dev/null \
     || useradd --uid "${PUID}" --gid "${PGID}" --home-dir /app --no-create-home \
        --shell /usr/sbin/nologin scriptorium) \
 && mkdir -p /data \
 && chown "${PUID}:${PGID}" /data

# The environment is owned by root: the service user can run it but not modify it.
COPY --from=builder /app/.venv /app/.venv
COPY --chmod=0755 docker/entrypoint.sh /app/entrypoint.sh

ENV PATH=/app/.venv/bin:${PATH} \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    XDG_CACHE_HOME=/tmp/fontconfig

LABEL org.opencontainers.image.title="scriptorium" \
      org.opencontainers.image.description="Self-hosted MCP server and sync worker for reMarkable libraries" \
      org.opencontainers.image.source="https://github.com/MilkyEngineer/scriptorium" \
      org.opencontainers.image.licenses="MIT"

WORKDIR /app
# Numeric, so it works even when PUID/PGID name an existing user or group.
USER ${PUID}:${PGID}
EXPOSE 8000

# Probes /healthz on the local listener. The worker has no listener: compose
# overrides the healthcheck for the worker service with `healthcheck --worker`.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["scriptorium", "healthcheck"]

ENTRYPOINT ["/app/entrypoint.sh"]
CMD ["serve"]

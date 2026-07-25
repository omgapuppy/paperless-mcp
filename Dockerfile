# syntax=docker/dockerfile:1.7
FROM python:3.12.11-slim-bookworm AS builder

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

COPY --from=ghcr.io/astral-sh/uv:0.7.21 /uv /uvx /bin/
WORKDIR /app

COPY pyproject.toml uv.lock README.md LICENSE ./
COPY src ./src
RUN uv sync --locked --no-dev --no-editable

FROM python:3.12.11-slim-bookworm AS runtime

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PAPERLESS_MCP_AUDIT_DIR=/data/audit

RUN groupadd --system --gid 10001 paperless \
    && useradd --system --uid 10001 --gid paperless --home-dir /nonexistent paperless \
    && install -d -o paperless -g paperless /data/audit

WORKDIR /app
COPY --from=builder --chown=paperless:paperless /app/.venv /app/.venv

USER 10001:10001

# Stdio is the only transport, so no network port or container healthcheck applies.
# Keep stdin attached (`docker run -i`) for the lifetime of the MCP session.
ENTRYPOINT ["paperless-mcp"]
CMD ["mcp"]

# Node agent image: small and rarely changing. Runs as root because it talks to
# the Docker socket (whose group id differs between hosts).
#   docker build -f deploy/docker/agent.Dockerfile -t pos-agent .
ARG PYTHON_IMAGE=python:3.12-slim-trixie

FROM ${PYTHON_IMAGE} AS build
COPY --from=ghcr.io/astral-sh/uv:0.11.32 /uv /bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv
WORKDIR /src
COPY pyproject.toml uv.lock ./
COPY shared shared
COPY coordinator coordinator
COPY worker worker
COPY agent agent
RUN uv sync --frozen --no-dev --no-editable --package pos-agent

FROM ${PYTHON_IMAGE}
ARG VERSION=dev
LABEL org.opencontainers.image.title="price-of-size node agent" \
      org.opencontainers.image.source="https://github.com/angeeinstein/price-of-size" \
      org.opencontainers.image.version="${VERSION}"
COPY --from=build /app/.venv /app/.venv
# The exchange directory belongs to uid 10001 (the service user in the other
# images), so a fresh volume is writable by the service whoever mounts it first.
RUN install -d -o 10001 -g 10001 /exchange && install -d -m 0700 /data
ENV PATH=/app/.venv/bin:$PATH PYTHONUNBUFFERED=1
CMD ["pos-agent"]

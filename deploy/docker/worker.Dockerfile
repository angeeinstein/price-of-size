# Worker image: the same image runs on every node (amd64 and arm64).
#   docker build -f deploy/docker/worker.Dockerfile -t pos-worker .
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
RUN uv sync --frozen --no-dev --no-editable --package pos-worker

FROM ${PYTHON_IMAGE}
ARG VERSION=dev
LABEL org.opencontainers.image.title="price-of-size worker" \
      org.opencontainers.image.source="https://github.com/angeeinstein/price-of-size" \
      org.opencontainers.image.version="${VERSION}"
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*
# Volumes mounted here are initialised with this ownership on first use.
RUN groupadd --gid 10001 pos && useradd --uid 10001 --gid pos --no-create-home --shell /usr/sbin/nologin pos \
    && install -d -o pos -g pos /var/lib/pos-worker /var/lib/pos-worker-exchange
COPY --from=build /app/.venv /app/.venv
ENV PATH=/app/.venv/bin:$PATH PYTHONUNBUFFERED=1 \
    POS_WORKER_STATE_DIR=/var/lib/pos-worker
WORKDIR /var/lib/pos-worker
USER pos
CMD ["pos-worker"]

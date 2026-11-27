# Coordinator image: API, planner, LLM service; runs `pos-coordinator serve`.
# Build from the repository root:
#   docker build -f deploy/docker/coordinator.Dockerfile -t pos-coordinator .
ARG PYTHON_IMAGE=python:3.12-slim-trixie

FROM ${PYTHON_IMAGE} AS build
COPY --from=ghcr.io/astral-sh/uv:0.11.32 /uv /bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv
WORKDIR /src
# The whole workspace is needed for the lockfile; only this package gets installed.
COPY pyproject.toml uv.lock ./
COPY shared shared
COPY coordinator coordinator
COPY worker worker
COPY agent agent
RUN uv sync --frozen --no-dev --no-editable --package pos-coordinator

FROM ${PYTHON_IMAGE}
ARG VERSION=dev
LABEL org.opencontainers.image.title="price-of-size coordinator" \
      org.opencontainers.image.source="https://github.com/angeeinstein/price-of-size" \
      org.opencontainers.image.version="${VERSION}"
# pg_dump must be at least the server's major version. Debian 13 ships 17,
# which dumps servers up to 17 (the database role installs 16 or 17).
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates postgresql-client-17 \
    && rm -rf /var/lib/apt/lists/*
RUN groupadd --gid 10001 pos && useradd --uid 10001 --gid pos --no-create-home --shell /usr/sbin/nologin pos \
    && install -d -o pos -g pos /var/lib/pos /var/lib/pos/coordinator /var/lib/pos/raw \
       /var/lib/pos-exchange /backups
COPY --from=build /app/.venv /app/.venv
COPY alembic.ini /app/alembic.ini
COPY migrations /app/migrations
ENV PATH=/app/.venv/bin:$PATH PYTHONUNBUFFERED=1 POS_ALEMBIC_INI=/app/alembic.ini
WORKDIR /app
USER pos
EXPOSE 8000
CMD ["pos-coordinator", "serve"]

# syntax=docker/dockerfile:1
FROM golang:1.26-bookworm AS go-build
WORKDIR /source
COPY go.mod go.sum ./
RUN go mod download
COPY cmd ./cmd
COPY internal ./internal
RUN CGO_ENABLED=0 go build -trimpath -o /out/nexo-api ./cmd/api \
 && CGO_ENABLED=0 go build -trimpath -o /out/nexo-worker ./cmd/worker

FROM node:22-bookworm AS ui-build
ARG SOURCE_ROOT=/source
RUN apt-get update && apt-get install -y --no-install-recommends python3 \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /source
COPY . .
RUN npm ci --prefix explorer && python3 scripts/build_explorer.py --source-root "$SOURCE_ROOT"

FROM python:3.12-slim-bookworm AS runtime
COPY --from=ghcr.io/astral-sh/uv:0.12.11 /uv /usr/local/bin/uv
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PATH=/app/.venv/bin:$PATH
COPY pyproject.toml uv.lock ./
COPY src ./src
COPY migrations ./migrations
RUN uv sync --frozen --no-dev --no-editable \
 && groupadd --gid 10001 nexo \
 && useradd --uid 10001 --gid 10001 --no-create-home nexo
COPY --from=go-build /out/nexo-api /out/nexo-worker /app/
COPY --from=ui-build /source/src/nexo/static /app/src/nexo/static
USER 10001:10001
EXPOSE 8080 2222
CMD ["/app/nexo-api"]

# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Repository layout

This is a **Home Assistant custom add-on repository**. The root holds the HA repository manifest; each add-on lives in its own subdirectory.

```
repository.yaml        # HA repository manifest (name, description, maintainer)
koserver/              # The KoServer add-on
  config.yaml          # HA add-on manifest (slug, arch, ingress, options schema)
  build.yaml           # Base image per arch (ghcr.io/home-assistant/*-base-python:3.12)
  Dockerfile           # Builds from BUILD_FROM arg supplied by HA supervisor
  requirements.txt
  app/                 # FastAPI application (WORKDIR /app inside container)
    main.py            # Entrypoint: mounts static files, registers service routers, runs lifespan init
    config.py          # Settings: reads /data/options.json, falls back to env vars
    auth.py            # Two auth dependencies: require_ha_auth and require_api_key
    services/
      kobooks/         # KoBooks service module
        router.py      # All routes for this service + Jinja2 template config
        storage.py     # SQLite CRUD (synchronous sqlite3, WAL mode)
        models.py      # Book and Character dataclasses
        templates/     # Service-specific Jinja2 templates (extend base.html)
    templates/
      base.html        # Shared sidebar layout; extend this in every service
    static/            # Shared CSS, JS, placeholder SVG (mounted at /static)
```

## Running locally (outside Docker)

```bash
cd koserver
pip install -r requirements.txt

# Required env vars (substitute for /data/options.json which only exists in HA)
export API_KEY="your-secret-key"
export HA_URL="http://your-ha-instance:8123"
export DATA_DIR="./data"          # local data dir for SQLite + portraits

uvicorn app.main:app --reload --port 8099
```

The app is then at `http://localhost:8099`.

## Building the Docker image locally

```bash
cd koserver
docker build \
  --build-arg BUILD_FROM=python:3.12-slim \
  -t koserver-dev .

docker run --rm -p 8099:8099 \
  -e API_KEY="test-key" \
  -e HA_URL="http://host.docker.internal:8123" \
  -v $(pwd)/data:/data \
  koserver-dev
```

## Testing the upload API

```bash
# Upload a KoCharacters ZIP (API key auth, no HA token needed)
curl -X POST http://localhost:8099/services/kobooks/api/upload \
  -H "X-Api-Key: your-secret-key" \
  -F "file=@/path/to/BookTitle_1234.zip"
```

Web UI routes require `Authorization: Bearer <ha-long-lived-token>` header.

## Architecture: how a new service is added

1. Create `koserver/app/services/<name>/` with `router.py`, `models.py`, `storage.py`, and `templates/`
2. In `router.py`, set up a `ChoiceLoader` pointing to the service's own `templates/` first and then `app/templates/` (the shared base) — see `kobooks/router.py` for the pattern
3. Mount the router in `app/main.py`: `app.include_router(name_router.router, prefix="/services/<name>")`
4. Add a sidebar link in `app/templates/base.html`

## Authentication model

- **Web UI routes** (`GET /services/*`): `require_ha_auth` dependency — validates `Authorization: Bearer <token>` against `{ha_url}/api/`. Results are cached in-process for 60 s.
- **Upload API** (`POST .../api/upload`): `require_api_key` dependency — checks `X-Api-Key` header against `settings.api_key`.
- `/health` is unauthenticated.

## Configuration resolution order

`config.py` resolves settings in this priority: `/data/options.json` (HA runtime) → environment variables (`API_KEY`, `HA_URL`, `DATA_DIR`) → hardcoded defaults.

## Data persistence

- SQLite database: `/data/db/koserver.db` (WAL mode, synchronous writes via stdlib `sqlite3`)
- Portrait images: `/data/portraits/<book_id>/<filename>.png`
- Both paths are under the `/data` volume mapped in `config.yaml` (`map: [data:rw]`)

## HA add-on repository rules

- `repository.yaml` at repo root must have at minimum a `name` field (quoted string). HA supervisor reads from the **default branch** (`main`).
- Each add-on subdirectory must contain `config.yaml` with `name`, `version`, `slug`, `description`, `startup`, `boot`, `arch`.
- `slug` in `config.yaml` must match the subdirectory name.
- Ingress is configured via `ingress: true` + `ingress_port` in `config.yaml`.

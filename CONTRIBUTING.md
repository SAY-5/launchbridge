# Contributing

## Setup

```
make install          # uv sync with the dev extras (Python 3.12)
cp .env.example .env  # optional, for running the API outside compose
```

Docker is required for the test suite (Testcontainers starts `postgres:16-alpine`) and for
`make up`, `make smoke` and `make demo`. Set `TEST_DATABASE_URL` to use an existing
PostgreSQL instead; CI does this with a service container.

## Checks

```
make lint          # ruff check + ruff format --check
make test          # pytest (unit + database + in-process smoke)
make tf-fmt        # terraform fmt -check
make tf-validate   # terraform init -backend=false && terraform validate
make web-ci        # browser console: npm ci, typecheck and bundle, self-check
make ci            # all of the above plus the image build
```

`.github/workflows/ci.yml` defines the same steps. Node 22 and npm are needed for
`make web-ci`; everything else needs `uv`, Docker and Terraform.

The browser console under `web/` has its own notes in [web/README.md](web/README.md). A
change to the port ships with an assertion in `web/src/sim/selfcheck.ts`, the way a change to
the service ships with a test in `tests/`.

## Conventions

- Single-line conventional commits: `feat:`, `fix:`, `test:`, `docs:`, `build:`, `chore:`.
- Every behaviour change ships with a test in `tests/`. Database-backed tests get a clean
  schema per test via `TRUNCATE`; use the `client`, `worker` and `receiver` fixtures rather
  than starting servers.
- The worker takes an injectable clock (`now`) for scheduling. Outbound signatures always use
  wall-clock time; do not change that or receivers will reject retries as stale.
- Schema changes go through Alembic: edit `launchbridge/models.py`, add a revision under
  `launchbridge/alembic/versions/`, and keep `downgrade()` working.
- Keep `destinations.yaml` free of real secrets; reference environment variables.

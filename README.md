# Artikate Field Asset Check-Out Service

A small internal REST API for tracking equipment checked out to employees,
built for the Artikate backend take-home assignment (Part A).

## Stack

- Django  + Django REST Framework
- PostgreSQL
- Redis  (Celery broker/result backend)
- Celery + Celery Beat (`django-celery-beat` database scheduler)
- Token auth + JWT (`djangorestframework-simplejwt`), rate limiting, and
  an OpenAPI schema/Swagger UI (`drf-spectacular`)
- Docker / docker-compose
- pytest / pytest-django

## Verified end-to-end

Everything below was actually run, not just written — against a real
PostgreSQL 16 + Redis 7 + Celery worker, not sqlite/mocks:

- `python manage.py migrate` — clean, no errors, against real Postgres.
- `python manage.py seed_demo_data` — creates the 8 assets / 4 employees /
  5 check-out scenarios; re-running it a second time correctly no-ops
  (idempotent).
- **Full API exercised live** via `runserver` + `curl`: `GET /health/`,
  `GET /assets/?status=AVAILABLE`, `POST /checkouts/` (201), a repeat
  check-out of the same asset (**409**, as required), `GET /assets/{id}/`
  showing `current_holder` populated correctly, `POST
  /checkouts/{id}/return/` (asset flips back to `AVAILABLE`), `GET
  /employees/{code}/summary/` (counts and mean-hold-duration match the
  underlying data), and `GET /reports/overdue/` (correctly sorted
  most-overdue-first).
- **The full pytest suite (26 tests) passes against real Postgres,
  including the concurrency test** — `TestCheckOutConcurrency` fires two
  simultaneous check-out requests for the same asset via two threads and
  two separate DB connections, and confirms exactly one gets `201` and the
  other `409`, with exactly one open `CheckOut` row left in the database.
  This actually exercises `SELECT ... FOR UPDATE` row-level locking, which
  SQLite cannot do (the test is `skipif`-gated to skip cleanly on SQLite
  rather than give a false pass there).
- **The Celery worker was run for real against Redis** (not called as a
  plain Python function): `flag_overdue_checkouts.delay()` was dispatched
  twice through the actual broker. First run: `2 overdue, 2 notices
  created, 0 already existed`. Second run, same day: `2 overdue, 0 notices
  created, 2 already existed` — confirmed only 2 `OverdueNotice` rows exist
  in the database after both runs, proving idempotency end-to-end through
  the real queue, not just via a direct function call in a unit test.
- **Rate limiting was fired past its limit live**: 22 rapid `POST
  /checkouts/` requests against the same authenticated user returned `404`
  for the first 20 (reaching business logic — unknown asset/employee) and
  **`429 Too Many Requests`** with a `Retry-After` header for the 21st and
  22nd, matching the configured `checkout-create` scope
  (`20/minute`). Covered by `test_platform_features.py::TestThrottling`.
- **JWT auth was exercised live**: obtained an access/refresh pair from
  `POST /api/v1/auth/token/` and successfully called `GET /assets/` using
  `Authorization: Bearer <access>` — a second, stateless alternative to
  token auth, working side-by-side with it.
- **The OpenAPI schema and docs UI were fetched live**: `GET
  /api/v1/schema/` returns a valid `openapi: 3.0.3` document listing every
  endpoint (verified `/checkouts/` and `/reports/overdue/` are present),
  and `GET /api/v1/docs/` (Swagger UI) returns `200`.
- **CI/CD workflows validated for real**: `actionlint` run against both
  `.github/workflows/*.yml` (zero findings, one real bug caught and fixed
  — a job referencing another job's output without declaring it as a
  dependency), `hadolint` run against the `Dockerfile` (zero findings
  after fixing an unpinned-package-version warning), and the `test` job's
  exact command sequence (env vars, `migrate`, `pytest`) executed locally
  against real Postgres/Redis with the same 26/26 pass result the
  workflow would produce. `ruff`/`black`/`isort` were also run against the
  whole codebase and every finding fixed before wiring them into CI, so
  the pipeline is green from its first real run rather than immediately
  red.
- **Zero warnings, anywhere, verified by actually running everything and
  looking**: `python manage.py spectacular` went from 1 warning + 12
  errors down to a completely silent run (fixed by adding
  `@extend_schema`/`@extend_schema_field` to the three plain `APIView`s
  and the `current_holder` method field, plus resolving an enum-naming
  collision). The Celery worker's startup log went from two warnings
  (a `CPendingDeprecationWarning` about broker-retry config, and a
  `SecurityWarning` about running as root) to completely clean — verified
  by actually starting the worker as a non-root user after adding one to
  the Dockerfile. `python manage.py check --deploy` goes from 6 security
  warnings to `0 issues` when given a real production-shaped environment
  (`DEBUG=0`, `DJANGO_ENFORCE_HTTPS=1`, a strong generated secret key) —
  verified directly, not assumed. The full test suite was re-run with
  `python -W always` (forces every Python warning to print) and stayed
  completely silent.

## Quick start (Docker — recommended)

```bash
git clone <this-repo-url>
cd artikate-checkout
cp .env.example .env          # defaults work as-is for local docker-compose

docker compose up --build -d
docker compose exec web python manage.py migrate
docker compose exec web python manage.py createsuperuser   # optional, for /admin/
docker compose exec web python manage.py seed_demo_data
```

The API is now up at `http://localhost:8000/api/v1/`. `docker compose up`
brings up four services as required: `web` (Django/gunicorn), `db`
(Postgres), `redis`, and `worker` (Celery), plus a `beat` service for the
hourly schedule.

### Authenticating requests

Every endpoint except `/api/v1/health/` requires auth. Two schemes are
supported side-by-side:

**Option 1 — Token auth** (simplest to demo, no expiry to manage):
```bash
docker compose exec web python manage.py createsuperuser
docker compose exec web python manage.py shell -c \
  "from django.contrib.auth import get_user_model; from rest_framework.authtoken.models import Token; \
   u = get_user_model().objects.get(username='<your-superuser>'); print(Token.objects.get_or_create(user=u)[0].key)"

curl -H "Authorization: Token <the-key>" http://localhost:8000/api/v1/assets/
```

**Option 2 — JWT** (`djangorestframework-simplejwt`), for clients that would
rather not manage a long-lived static token:
```bash
curl -X POST -H "Content-Type: application/json" \
  -d '{"username": "<user>", "password": "<pass>"}' \
  http://localhost:8000/api/v1/auth/token/
# -> {"access": "...", "refresh": "..."}

curl -H "Authorization: Bearer <access>" http://localhost:8000/api/v1/assets/

# refresh when the access token expires (1 hour):
curl -X POST -H "Content-Type: application/json" \
  -d '{"refresh": "<refresh>"}' \
  http://localhost:8000/api/v1/auth/token/refresh/
```

Session auth also works if you're browsing via `/admin/` login in the same
browser.

### API documentation

Interactive Swagger UI is served at `/api/v1/docs/`, backed by a live
OpenAPI 3.0 schema at `/api/v1/schema/` (via `drf-spectacular`) — generated
directly from the serializers/viewsets, so it can't drift from the actual
code the way a hand-written spec can.

### Rate limiting

All endpoints are throttled (`rest_framework.throttling`): `300/minute` per
authenticated user and `30/minute` for anonymous requests by default.
`POST /checkouts/` (the one endpoint that protects a real business
invariant — the check-out limits) has its own tighter scoped limit of
`20/minute` per user, returning `429 Too Many Requests` with a
`Retry-After` header once exceeded.

## CI/CD

Two GitHub Actions workflows implement the pipeline described in
`ANSWERS.md` Part D3 as running code, not just prose:

**`.github/workflows/ci.yml`** — runs on every PR into `main` and every
push to `main`:
- `lint` — `ruff check .`, `black --check .`, `isort --check .`
- `test` — real `postgres:16` and `redis:7` **service containers** (not
  sqlite), then `manage.py check`, `makemigrations --check --dry-run`
  (fails the build if a model change wasn't migrated), `migrate`, and the
  full pytest suite — including the concurrency test, which only means
  anything against a real Postgres service.
- `docker-build` — builds the image (no push) purely to catch Dockerfile
  drift before it reaches main.

**`.github/workflows/cd.yml`** — triggered by the CI workflow *completing
successfully* on `main` (via `workflow_run`, not an independent trigger),
so a push that fails CI can structurally never reach this pipeline:
- `build-and-push` — builds the image and pushes it to GHCR tagged with
  both the git SHA and `:latest`, so a rollback is always a concrete,
  addressable image, not a hope that an old layer is still cached
  somewhere.
- `migrate` — a discrete, separate step that runs migrations *before* any
  instance is updated, gated behind a GitHub **Environment** named
  `production` (configure required reviewers on it under repo Settings →
  Environments for a manual approval gate here).
- `deploy` — rolls the new image out, then polls `/api/v1/health/` and
  fails the deploy if it never comes back healthy.

**Both workflows were validated with `actionlint` (zero findings) and the
`test` job's exact command sequence — env vars, migrate, pytest — was run
locally against real Postgres/Redis and passed (26/26)**, not just written
and assumed correct.

**What isn't (and can't be) verified without a real target**: the
`migrate`/`deploy` steps in `cd.yml` SSH into a host defined by
`PRODUCTION_SSH_HOST`/`_SSH_USER`/`_SSH_KEY` repo secrets — until those
secrets exist, both steps print a warning and exit cleanly rather than
fail, which is intentional (a CD pipeline with no deploy target configured
yet shouldn't be a permanently red build), but it also means the actual
SSH/deploy commands have not been executed against a real server. Swap
that block for an `aws ecs run-task`/`update-service` pair if deploying to
ECS instead of a single EC2 host.

Also present:
- **`.github/CODEOWNERS`** — requires a specific reviewer on any PR
  touching `core/migrations/`, implementing the D3 answer's "a bad
  migration can't be undone by redeploying old code, so it gets a required
  reviewer" as an actual enforced GitHub rule (once branch protection has
  "Require review from Code Owners" turned on).
- **`.github/dependabot.yml`** — weekly update PRs for pip, Docker base
  images, and the GitHub Actions themselves.

## Quick start (without Docker, sqlite, for fast local iteration)

This is **not** how the concurrency guarantee should be evaluated (see
"Known gaps" below), but it's the fastest way to poke at the API or run most
of the test suite without standing up Postgres/Redis.

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt   # includes requirements.txt + lint tools

USE_SQLITE=1 python manage.py migrate
USE_SQLITE=1 python manage.py seed_demo_data
USE_SQLITE=1 python manage.py runserver
```

## Running tests

```bash
# against sqlite (fast, but skips the true concurrency test — see below)
USE_SQLITE=1 python -m pytest core/tests -v

# against the real stack (recommended, exercises SELECT ... FOR UPDATE)
docker compose exec web python -m pytest core/tests -v
```

## Endpoints

All under `/api/v1/`:

| Method & path | Notes |
|---|---|
| `POST /assets/` | Create an asset |
| `GET /assets/` | List, paginated 20/page. Filters: `?status=`, `?category=`, `?search=` (matches `name` or `asset_tag`) |
| `GET /assets/{id}/` | Detail, includes `current_holder` (`null` or `{employee_code, full_name}`) |
| `POST /checkouts/` | Body: `{asset_tag, employee_code, due_at}` |
| `POST /checkouts/{id}/return/` | Body: `{condition_note, needs_maintenance}` |
| `GET /employees/{employee_code}/summary/` | 4 aggregated numbers, one query |
| `GET /reports/overdue/` | Open, overdue check-outs, most overdue first |
| `GET /health/` | Unauthenticated; reports DB connectivity |
| `POST /auth/token/` | Obtain a JWT access/refresh pair (alternative to token auth) |
| `POST /auth/token/refresh/` | Refresh an expired JWT access token |
| `GET /schema/` | OpenAPI 3.0 schema (JSON/YAML) |
| `GET /docs/` | Interactive Swagger UI |

## Assumptions

These are the judgement calls made where the spec left room, per the
assignment's own instruction to document rather than block on them:

1. **Auth scheme**: DRF TokenAuthentication as the primary scheme (+
   SessionAuthentication for `/admin/` convenience) — chosen because the
   assignment is internal-tool-shaped and it's the least ceremony to demo
   in a 6–8 minute recording. JWT (`djangorestframework-simplejwt`) is
   additionally wired up and working (`/api/v1/auth/token/` +
   `/refresh/`) as a second, stateless option for any client that would
   rather not manage a static long-lived token — both schemes are checked
   on every request, so either credential type works interchangeably.
2. **`due_at` validation is checked against wall-clock time at the moment of
   the request** (`due_at > now` and `due_at <= now + 30 days`), not against
   `checked_out_at` (which is set by `auto_now_add` to the same instant in
   practice, so these are equivalent for real requests).
3. **"Most overdue first"** is interpreted as ordering by `due_at` ascending
   — the earliest due date is the most overdue. Ties broken by natural row
   order (no secondary sort was specified).
4. **Employee open-checkout limit (rule 3)** is enforced by locking the
   `Employee` row with `select_for_update()` inside the same transaction as
   the asset lock. The assignment's explicit concurrency test is about the
   *asset* (rule 7), but the same race exists for "3 open check-outs" if two
   requests for the same employee land at once, so I closed it the same way
   rather than leaving a narrower, technically-passing implementation.
5. **`current_holder`** is derived by looking up the asset's own open
   check-out rather than adding a denormalized `current_employee` FK to
   `Asset` — one extra indexed query (guarded by checking `status` first) is
   preferable to two sources of truth for who holds an asset.
6. **Seed data idempotency** is implemented by tagging each demo check-out's
   `condition_note` with a `SEED:` marker used as the re-run lookup key,
   since check-out timestamps are relative to "now" and will legitimately
   differ across runs on different days — a naive `(asset, employee, due_at)`
   key would falsely treat re-runs as new data every day. Assets and
   employees are keyed on their natural unique fields (`asset_tag`,
   `employee_code`).
7. **`GET /reports/overdue/`** treats "overdue" as strictly
   `due_at < now` (not `<=`), matching the business rule's wording
   ("past their due_at").
8. **Health check** returns `503` (not `200`) if the DB ping fails, since a
   monitoring/orchestration tool consuming this endpoint needs a non-2xx to
   act on; the assignment only specifies the 200/healthy case explicitly.

## Commit history

This repo was built incrementally (see `git log`): project skeleton →
models/migrations → serializers/views/urls/Celery task → seed command →
tests → Docker → docs. Each commit is independently runnable at the point
migrations exist.

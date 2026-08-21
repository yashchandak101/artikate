# Artikate Field Asset Check-Out Service

A small internal REST API for tracking equipment checked out to employees,
built for the Artikate backend take-home assignment (Part A).

## Stack

- Django 5 + Django REST Framework
- PostgreSQL 16
- Redis 7 (Celery broker/result backend)
- Celery + Celery Beat (`django-celery-beat` database scheduler)
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
- **The full pytest suite (20 tests) passes against real Postgres,
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

Every endpoint except `/api/v1/health/` requires auth. This project uses
DRF's built-in **TokenAuthentication** (simplest to demo end-to-end without
extra infra). Session auth also works if you're browsing via `/admin/` login
in the same browser.

```bash
# create a user + token
docker compose exec web python manage.py createsuperuser
docker compose exec web python manage.py shell -c \
  "from django.contrib.auth import get_user_model; from rest_framework.authtoken.models import Token; \
   u = get_user_model().objects.get(username='<your-superuser>'); print(Token.objects.get_or_create(user=u)[0].key)"

curl -H "Authorization: Token <the-key>" http://localhost:8000/api/v1/assets/
```

## Quick start (without Docker, sqlite, for fast local iteration)

This is **not** how the concurrency guarantee should be evaluated (see
"Known gaps" below), but it's the fastest way to poke at the API or run most
of the test suite without standing up Postgres/Redis.

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

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

## Assumptions

These are the judgement calls made where the spec left room, per the
assignment's own instruction to document rather than block on them:

1. **Auth scheme**: DRF TokenAuthentication (+ SessionAuthentication for
   `/admin/` convenience). Chosen over JWT because the assignment is
   internal-tool-shaped (no third-party clients, no refresh-token need) and
   token auth is the least ceremony to demo in a 6–8 minute recording.
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

## Known gaps

Documented honestly rather than hidden, per the assignment's instructions:

1. **No JWT / refresh-token flow** — token auth only, per Assumption 1.
2. **No rate limiting / throttling** on the API — out of scope for the
   assignment but worth flagging for a real internal tool.
3. **No OpenAPI schema** (drf-spectacular etc.) generated — the endpoint
   table above is the closest thing to a spec.
4. **Screen recording**: link goes here once recorded —
   `<ADD LOOM/RECORDING LINK HERE>`.

(The one gap that used to be listed here — being unable to verify the
concurrency test locally — has since been closed: Postgres and Redis were
installed and run for real, and the full suite including
`TestCheckOutConcurrency` and a live Celery-worker dispatch of
`flag_overdue_checkouts` were both verified end-to-end. See "Verified
end-to-end" above.)

## Commit history

This repo was built incrementally (see `git log`): project skeleton →
models/migrations → serializers/views/urls/Celery task → seed command →
tests → Docker → docs. Each commit is independently runnable at the point
migrations exist.

# ANSWERS.md

## Part B — Diagnose three broken snippets

### Snippet 1 — overdue report view

**1. What is wrong (every distinct defect):**

- **N+1 queries.** The loop accesses `c.asset.name`, `c.asset.asset_tag`, and
  `c.employee.full_name` for every row with no `select_related`. Each access
  is a separate query, so the endpoint issues roughly `2 × (number of open
  check-outs)` queries instead of one. This is exactly the failure mode
  Part A explicitly guards against ("must not issue a query per row").
- **Filtering happens in Python, not the database.** `CheckOut.objects.filter(returned_at__isnull=True)`
  pulls *every open check-out* into memory — not just overdue ones — before
  the `if c.due_at < timezone.now()` check discards most of them. As the
  table grows this scales with "all open check-outs," not "overdue
  check-outs," which is a much larger and unnecessarily fetched set.
- **No authentication/permission check at all.** This is a bare Django view
  (`JsonResponse`, no `@api_view`, no permission classes), so it bypasses
  the requirement that every endpoint except `/health/` requires
  authentication.
- **No pagination.** An unbounded, unpaginated JSON array is returned,
  inconsistent with the rest of the API and dangerous at scale.
- **`timezone.now()` is called twice per row** (once in the filter
  condition, once in the `days_overdue` calculation) instead of once
  up-front. On a slow/loaded request these two calls can drift apart by
  seconds, and it's simply wasteful.
- **Sorting is done in Python** (`rows.sort(...)`) after fetching, instead
  of letting the database return rows in order — again fine at small scale,
  a real cost once "small scale" stops being true.

**2. Why does it look correct in local testing?**

With the handful of rows a developer seeds locally, N+1 means "12 queries
instead of 1" — invisible without query-count tooling, and still
millisecond-fast either way. The missing auth check is invisible because
developers testing manually are almost always already authenticated (an
active admin/browser session), so nobody notices the endpoint would also
answer with no credentials at all. And nothing here throws an exception in
local testing — it's a set of *scaling and security* defects, not a
crashing bug, so casual manual testing never surfaces it.

**3. How would you fix it?**

The real implementation in this repo (`core/views.py::OverdueReportView`)
already reflects the fix: DRF `APIView` with the default
`IsAuthenticated` permission, filtering and ordering done by the ORM
(`due_at__lt=now`, `.order_by("due_at")`), `select_related("asset",
"employee")` to eliminate N+1, and `timezone.now()` captured once. In
short:

```python
class OverdueReportView(APIView):
    def get(self, request):
        now = timezone.now()
        queryset = (
            CheckOut.objects.filter(returned_at__isnull=True, due_at__lt=now)
            .select_related("asset", "employee")
            .order_by("due_at")
        )
        rows = [{...} for c in queryset]
        return Response({"count": len(rows), "results": rows})
```

(Pagination would be added via DRF's paginator if this list could get large
enough to need paging in the UI, separate from the "no N+1" fix.)

**4. What test/tooling would have caught this before it shipped?**

- A test with, say, 10+ overdue check-outs wrapped in
  `django.test.utils.CaptureQueriesContext`, asserting the query count stays
  constant (not linear in row count) — this repo's
  `test_overdue.py::test_report_does_not_issue_a_query_per_row` is exactly
  this test.
- A test that hits the endpoint with **no** credentials and asserts
  `401`/`403` — trivial to write, and would have caught the missing auth
  immediately.
- `django-debug-toolbar` or the `nplusone` package in development, which
  flags lazy-loaded related-object access inside a loop.

---

### Snippet 2 — check-out endpoint

**1. What is wrong:**

- **No transaction, no row locking — the core concurrency bug (rule 7).**
  `asset.status != "AVAILABLE"` is read, then several lines later
  `asset.save()` writes it, with no lock in between. Two concurrent requests
  can both read `AVAILABLE` before either writes `CHECKED_OUT`, so **both**
  proceed to create a `CheckOut` row for the same asset — the exact
  violation the assignment calls out.
- **The employee's open-check-out limit has the same race** (rule 3):
  `open_count >= 3` is checked with no lock, so two concurrent requests for
  the same employee's 4th check-out can both read `open_count == 3` (i.e.
  count is currently at the limit) and both pass.
- **The `is_active` check (rule 2) is missing entirely** — not a race, a
  flat omission. This snippet will happily check out equipment to an
  inactive employee.
- **Unknown `asset_tag`/`employee_code` produce a 500, not a 404.**
  `Asset.objects.get(...)` and `Employee.objects.get(...)` raise
  `DoesNotExist` when nothing matches; that exception is never caught, and
  DRF's default exception handler does **not** map bare `DoesNotExist` to a
  404 (only `Http404`/`get_object_or_404` are mapped) — it becomes an
  unhandled 500.
- **`due_at` is never validated (rule 4 is entirely missing).** It's taken
  straight from `request.data["due_at"]` into `.create()` with no check
  that it's in the future or within 30 days — and if it's malformed, the
  field-level error at save time likely surfaces as a 500, not a 400.
- **The two writes (`CheckOut.objects.create(...)` and `asset.save()`) are
  not atomic (rule 5).** If anything raises between them, you get a
  `CheckOut` row that exists alongside an `AVAILABLE` asset — the exact
  invariant rule 5 forbids.
- **`request.data["asset_tag"]` etc. use dict indexing, not `.get()` with
  validation** — a missing key raises `KeyError`, again a 500 instead of a
  400.

**2. Why does it look correct in local testing?**

Nobody manually clicking through Postman fires two requests in the same
microsecond, so the races (rules 3 and 7) are invisible without deliberate
concurrent load (threads/locust). Manual testers also tend to use valid,
known-good `asset_tag`/`employee_code` values and comfortably-valid
`due_at`s ("next week"), so the missing 404 handling, the missing
`is_active` check, and the missing `due_at` range validation are simply
never exercised — they all require someone to deliberately try a bad input,
which "does the happy path work" testing skips by construction.

**3. How would you fix it?**

This repo's `core/views.py::CheckOutViewSet.create` is the corrected
version. The key structural changes:

```python
asset = get_object_or_404(Asset, asset_tag=asset_tag)          # 404s first
employee = get_object_or_404(Employee, employee_code=employee_code)

if not employee.is_active:                                      # rule 2
    raise ValidationError({"detail": "employee is not active"})
if due_at <= now or due_at > now + timedelta(days=30):           # rule 4
    raise ValidationError({"detail": "..."})

with transaction.atomic():
    asset_locked = Asset.objects.select_for_update().get(pk=asset.pk)
    if asset_locked.status != Asset.Status.AVAILABLE:            # rule 1/7
        return Response({...}, status=409)

    employee_locked = Employee.objects.select_for_update().get(pk=employee.pk)
    open_count = CheckOut.objects.filter(
        employee=employee_locked, returned_at__isnull=True
    ).count()
    if open_count >= 3:                                          # rule 3/7
        return Response({...}, status=409)

    checkout = CheckOut.objects.create(asset=asset_locked, employee=employee_locked, due_at=due_at)
    asset_locked.status = Asset.Status.CHECKED_OUT
    asset_locked.save(update_fields=["status", "updated_at"])     # rule 5, same transaction
```

`select_for_update()` inside `transaction.atomic()` means a second
concurrent request blocks on acquiring the row lock until the first
transaction commits (or rolls back); when it then re-reads `status`, it
correctly sees `CHECKED_OUT` and returns 409 — solved at the database
level, not with an application flag or a sleep, as required.

**4. What test/tooling would have caught this before it shipped?**

- A genuine concurrency test spawning two threads/requests at once and
  asserting exactly one `201` and one `409` (this repo's
  `TestCheckOutConcurrency`, gated to run against Postgres where
  `SELECT ... FOR UPDATE` is real).
- Simple unit tests for unknown asset/employee (expect 404, would
  immediately show a 500 traceback instead), inactive employee (expect
  400, would immediately show a 201 instead), and out-of-range `due_at`
  (expect 400).
- A project convention/lint rule that any view mutating more than one
  row/model must have an explicit `transaction.atomic()` block, checked in
  code review.

---

### Snippet 3 — nightly notice task

**1. What is wrong:**

- **No idempotency guard — the central bug.** The task unconditionally
  calls `OverdueNotice.objects.create(...)` for every overdue check-out on
  every invocation. Re-running it (whether from Celery Beat firing twice,
  or a retry after partial failure) creates a duplicate notice for every
  row already processed — directly violating the "must be safe to run
  repeatedly" requirement. Nothing here relies on the `(checkout,
  notice_date)` unique constraint or checks for an existing notice first.
- **No `select_related("employee")`** — `c.employee` is accessed per row
  inside the loop with no eager load, so this is the same N+1 pattern as
  Snippet 1, and the prompt itself flags this matters "when the overdue set
  grows to tens of thousands of rows."
- **Passing full model instances into `.delay()`.** `deliver_email.delay(c.employee, c)`
  hands Celery two Django model instances. With the JSON serializer
  specified for this project, that either fails outright (`EncodeError`) or
  depends on non-default serialization — and even where it "works," it's
  fragile: by the time the child task actually runs, the in-memory snapshot
  may be stale. Only primitive IDs should cross a task boundary; the child
  task should re-fetch fresh data itself.
- **No partial-failure handling / no batching.** The whole overdue set is
  iterated inside one task invocation with no `try/except` per row and no
  chunking. A single bad row aborts the entire run after already creating
  some notices and dispatching some emails — worsening the duplication
  problem on retry, since the next run reprocesses everything, including
  rows already handled.
- **`overdue.count()` at the end re-runs the query** a second time
  (the queryset was already consumed once by the `for` loop), which is
  wasteful and, under concurrent writes, can disagree with the number
  actually processed in the loop.

**2. Why does it look correct in local testing?**

Run once, manually, in a dev shell against a handful of demo rows, the task
"looks" idempotent purely by accident — because nobody ran it twice in the
same test session. The retry-duplication defect only appears when the task
is genuinely retried after a partial failure (a crash mid-loop, a Celery
worker restart, Beat firing while a previous run is still in flight) —
none of which casual local testing simulates. The N+1 pattern is invisible
at 5–10 demo rows the same way it was in Snippet 1. And the serialization
issue can be masked entirely if a dev's local Celery config uses eager
mode or pickle rather than the JSON+Redis setup specified for production.

**3. How would you fix it?**

This repo's `core/tasks.py::flag_overdue_checkouts` is the corrected
version: it relies on the database's own uniqueness guarantee rather than
an application-level flag, wraps each row's creation in its own small
transaction so one failure doesn't sink the batch, and only pulls IDs
up-front:

```python
overdue_checkout_ids = list(
    CheckOut.objects.filter(returned_at__isnull=True, due_at__lt=now)
    .values_list("id", flat=True)
)
for checkout_id in overdue_checkout_ids:
    try:
        with transaction.atomic():
            OverdueNotice.objects.create(checkout_id=checkout_id, notice_date=today)
        created += 1
    except IntegrityError:
        skipped += 1   # already notified today — expected, not an error
```

For the "tens of thousands of rows" scale case and the email fan-out, I'd
additionally: pass only `checkout.id`/`employee_id` into
`deliver_email.delay(...)` (never a model instance), and chunk the work
(`.iterator(chunk_size=...)` or split into Celery sub-tasks via
`group`/`chord`) rather than looping synchronously over the entire set in
one task invocation.

**4. What test or tooling would have caught this?**

- Exactly the test in `core/tests/test_tasks.py`: call the task twice,
  assert the second run creates zero new notices.
- A test simulating partial failure (e.g. monkeypatching
  `deliver_email.delay` to raise on the 2nd call) and asserting a rerun
  doesn't duplicate notices for rows already committed.
- A project rule/lint that Celery task arguments must be JSON-primitive
  (ids/strings), not model instances — enforceable by a small custom check
  or firmly by review.

---

## Part C — Optimise a slow PostgreSQL query

### 1. Rewritten query

```sql
SELECT c.id, c.asset_id, c.employee_id, c.checked_out_at, c.due_at,
       c.returned_at, c.condition_note
FROM checkouts c
JOIN employees e
  ON e.id = c.employee_id
 AND e.is_active = true
WHERE c.checked_out_at >= '2026-01-01'
  AND c.checked_out_at <  '2026-07-01'
  AND c.returned_at IS NULL
ORDER BY c.due_at ASC
LIMIT 50;  -- paginate; adjust OFFSET/keyset per screen
```

Changes and why:

- **`DATE(c.checked_out_at) BETWEEN ... AND ...` → a plain range compare
  on the raw timestamp column, with an exclusive upper bound
  (`< '2026-07-01'` instead of `<= '2026-06-30'`).** This is the single
  most important change. `DATE(...)` wraps the column in a function, which
  makes the predicate **non-sargable** — Postgres can't use a plain b-tree
  index on `checked_out_at` to satisfy it, because it would have to
  evaluate the function per row rather than range-scan the index. The
  range form is index-friendly. The exclusive upper bound also removes an
  implicit dependency on the server's timezone setting when truncating a
  `timestamptz` to a date — the original `BETWEEN ... '2026-06-30'` is
  ambiguous about what time-of-day on the 30th it means; `< '2026-07-01'`
  isn't.
- **Uncorrelated `IN (SELECT ...)` → explicit `JOIN ... AND e.is_active = true`.**
  Functionally equivalent (Postgres 15 will often flatten the subquery
  into a semi-join anyway), but the explicit join is clearer, and pushing
  `is_active` into the join condition avoids relying on planner subquery
  flattening. Given `employees` is only ~12k rows, this part was never the
  bottleneck — the change here is about clarity and predictability more
  than raw speed.
- **`SELECT *` → an explicit column list**, dropping `condition_note`
  (free-text, potentially large/TOASTed) unless the report actually
  displays it. Reduces I/O for a query that can return many rows.
- **Added `LIMIT`** (with pagination) since this feeds a screen, not a
  batch export — even with perfect indexing, streaming thousands of full
  rows to a UI is its own scalability problem, separate from the query
  plan.

### 2. Indexes

```sql
CREATE INDEX CONCURRENTLY idx_checkouts_open_checked_out_at
ON checkouts (checked_out_at)
INCLUDE (due_at, employee_id)
WHERE returned_at IS NULL;
```

Why a **partial** index: `returned_at IS NULL` is highly selective at
scale — most historical check-outs are eventually returned, so "open"
check-outs are a small, roughly stable-sized subset of the table even as
the table itself grows to millions of rows. A partial index only contains
that subset, so it stays small and cheap to maintain regardless of total
table size — this is exactly why it's the right call here rather than a
full index over all 4.2M rows (most of which the query will never match).

Why `checked_out_at` as the leading (only indexed) column: it's the range
predicate the query filters on, and a partial index restricted to open
rows lets Postgres do a targeted range scan directly against the date
window instead of a sequential scan.

Why `INCLUDE (due_at, employee_id)` rather than making them part of the key:
they aren't filtered or joined on with equality here in a way that benefits
from being sort-key columns of the index itself — `INCLUDE` adds them as
payload so the index can potentially satisfy the query without a heap
fetch (index-only scan, if the visibility map is current), without
complicating the index's actual key structure.

Why **not** a composite index leading on `employee_id`: `employee_id` here
is only filtered indirectly (via the join to an already-tiny `employees`
table), not with an equality condition on `checkouts` itself — an index
led by a non-equality-filtered column would waste the sort order the
partial-index-on-`checked_out_at` approach actually needs.

Why I'm **not** adding `ORDER BY due_at` support via an index: `due_at` is
not filtered, only sorted, and it doesn't share a natural prefix
relationship with the range condition on `checked_out_at` — you can't have
one plain b-tree index simultaneously provide an efficient range scan on
one column and pre-sorted output on a different, uncorrelated column. Given
the filter (date window + open + active employee) should already narrow
the row count substantially, I'd accept an in-memory sort (`Sort Method:
quicksort`) over the filtered result rather than distort the index design
to chase eliminating the sort node — see the `EXPLAIN` discussion below for
the line that confirms whether that assumption holds.

I'd explicitly **not** add an index on `employees.is_active` — with ~12k
rows, a sequential scan of that whole table costs a handful of page reads
and is not worth a second index to maintain; I'd say so directly rather
than add it reflexively.

### 3. Expected `EXPLAIN (ANALYZE, BUFFERS)`, before vs. after

**Before:** top node is a `Seq Scan on checkouts`, with a `Filter:` line
containing the `DATE(checked_out_at)` computation (because it's
non-sargable, the planner has no index condition to push down — it can
only apply this as a row-by-row filter after fetching), a large `Rows
Removed by Filter` count (most of the table's 4.2M rows get fetched and
then discarded), a large `Buffers: shared hit=... read=...` reflecting a
near-full-table scan, and a separate `Sort` node above it for `ORDER BY
due_at` — likely `Sort Method: external merge` (spilling to disk) if the
pre-sort row count is large enough to exceed `work_mem`. Total `Execution
Time` at the bottom around the reported ~8000ms.

**After:** top node becomes an `Index Scan` (or `Bitmap Heap Scan` +
`Bitmap Index Scan`) using `idx_checkouts_open_checked_out_at`, with an
`Index Cond:` line showing the range bounds pushed into the scan itself
(not a post-fetch filter), `Rows Removed by Filter` collapsing to near
zero, and dramatically fewer `Buffers` (the partial index only covers open
rows). The `Sort` node for `due_at` should now show `Sort Method:
quicksort` (in-memory) since the filtered row count is small. **The single
line that tells you the fix worked** is `Rows Removed by Filter` dropping
from "most of the table" to ~0, together with `Execution Time` dropping
from ~8000ms toward tens of milliseconds.

### 4. What breaks first as the table keeps growing (8k rows/day)

Because the new index is partial on `returned_at IS NULL`, its size tracks
the count of *open* check-outs, not the total table size — so as long as
the fraction of check-outs that stay open doesn't itself grow, this
specific query is fairly insulated from raw table growth. What actually
breaks first in practice is more likely to be **autovacuum falling behind
on a high-churn table**: every "return" is an `UPDATE` on a previously
open row (setting `returned_at`), which under MVCC creates a dead tuple.
At 8k new rows/day plus a comparable rate of returns, if autovacuum's
default cost-based throttling can't keep pace (especially if a long-running
transaction is holding back cleanup), table and index bloat accumulates,
and `ANALYZE` statistics go stale — which can flip the planner back toward
sequential scans even with a good index in place. Before that happens, I'd
tune `autovacuum_vacuum_scale_factor`/cost settings specifically for
`checkouts` (rather than relying on defaults tuned for the whole cluster),
and I'd watch `pg_stat_user_tables.n_dead_tup` and
`last_autovacuum`/`last_analyze` as an early-warning signal rather than
waiting for the reporting screen to start timing out again. If reporting
windows ever need to reach much further back than "recent," partitioning
`checkouts` by `checked_out_at` (e.g., monthly ranges) would also bound
per-partition index size and let old data be dropped/archived instead of
deleted row by row.

### 5. What I'd want to measure before committing to this answer

I'd want the real `returned_at IS NULL` selectivity — `SELECT count(*)
FROM checkouts WHERE returned_at IS NULL` versus the total row count — and
`pg_stats` (particularly `n_distinct`/`correlation`) for `checked_out_at`
on this actual table. The whole partial-index strategy is a bet that "open
check-outs" stays a small fraction of the table; if that assumption is
wrong (e.g. a broken return-processing pipeline leaves a huge and growing
number of check-outs permanently open), the partial index gives far less
benefit and a different shape (e.g., leading on `employee_id`, or a
BRIN index given the naturally time-correlated, append-mostly nature of
`checked_out_at`) might be worth it instead. I can't be certain the index
above is the right one without a real `EXPLAIN (ANALYZE, BUFFERS)` on the
production data — reasoning from the schema alone is a hypothesis, not a
verified plan.

---

## Part D — Production reasoning

### D1. Zero-downtime migration

I'd use the standard expand → backfill → dual-write → contract sequence,
across four separate deploys, because every intermediate schema state must
remain valid for whatever code is still running against it during a
rolling deploy across four instances.

**Deploy 1 (expand, schema only):** Add `location_id` as a **nullable**
column: `ALTER TABLE checkouts ADD COLUMN location_id bigint NULL;` — a
metadata-only change in Postgres as long as it's nullable with no
non-constant default, so it doesn't rewrite the 4.2M-row table. Add the
foreign key as `NOT VALID` (`ADD CONSTRAINT ... FOREIGN KEY (location_id)
REFERENCES locations(id) NOT VALID`), which only checks new/changed rows
going forward and doesn't scan existing data. Then run `ALTER TABLE ...
VALIDATE CONSTRAINT` as its own statement — this takes a `SHARE UPDATE
EXCLUSIVE` lock (blocks other DDL, not reads/writes) rather than the
stronger lock a combined add+validate would need. No app code changes yet;
old code running against this schema is completely unaffected since
nothing references the new column.

**Deploy 2 (backfill):** A separate, batched data-migration job — chunked
by primary key ranges, committing each batch, with a short pause between
batches — populates `location_id` for existing rows. Never a single
unbatched `UPDATE` across 4.2M rows, which would hold locks and bloat the
table for a long time. Old code still runs unaffected; new code hasn't
shipped yet.

**Deploy 3 (dual-write):** Ship app code that starts populating
`location_id` on every new/updated check-out, while the column is still
nullable at the DB level. Because deploys roll across 4 instances one at a
time (not atomically), there's a window where some in-flight requests hit
old instances (skip the field) and some hit new ones (fill it) — expected,
and harmless since the column is still optional; any stragglers get caught
by a final backfill sweep before the last step.

**Deploy 4 (contract, enforce NOT NULL):** Only after confirming (a) 100%
of instances are running the dual-write code and (b) a final sweep shows
zero remaining NULLs, add the constraint via the same
`CHECK (location_id IS NOT NULL) NOT VALID` → `VALIDATE CONSTRAINT`
two-step (cheap add, then a lock-light validation pass) rather than a
direct `ALTER COLUMN ... SET NOT NULL`, which in the naive form requires a
full-table scan under a stronger lock.

**Four deploys total.** In-flight requests during any of these deploys are
safe because every intermediate schema state is a strict superset of what
older code needs — nothing is renamed, dropped, or made mandatory until
the very last step, and that last step only ships once no running code
path can violate it.

**What would lock the table if done wrong:** adding the column as `NOT
NULL` with a default in one step (or on a Postgres version/config where
that isn't a fast metadata-only path) forces a full table rewrite under an
`ACCESS EXCLUSIVE` lock — blocking all reads and writes on `checkouts` for
the whole rewrite. Likewise, applying `SET NOT NULL` directly (skipping the
`CHECK ... NOT VALID` + `VALIDATE CONSTRAINT` trick) makes Postgres scan
the entire table to prove the constraint while holding a lock that blocks
concurrent writes for the duration.

### D2. Latency triage

Since no deploy happened in nine days, I'd assume the code is innocent
until the data says otherwise, and check in this order:

1. **`pg_stat_activity`** for currently long-running or blocked queries and
   lock waits — rules in/out an active contention problem (a stuck
   transaction, a lock pile-up) versus a plan/statistics issue.
2. **Live `EXPLAIN (ANALYZE, BUFFERS)`** on the actual overdue query against
   production (or an equivalently-sized replica) — rules in/out a plan
   regression: did the planner switch from an index scan to a sequential
   scan because statistics drifted or because row-count crossed a
   cost-estimate threshold? Given "no code changed," this is one of the
   most likely explanations, since data volume and its statistics shift
   silently even when code doesn't.
3. **`pg_stat_user_tables`** for `checkouts` — `n_dead_tup`,
   `last_autovacuum`, `last_analyze` — rules in/out autovacuum falling
   behind (bloat inflating the physical size the same logical query has to
   scan) as the root cause.
4. **Any long-running/idle-in-transaction session** that could be blocking
   autovacuum or holding locks — rules in/out an external actor (a batch
   job, an analyst's ad-hoc query) separate from this endpoint's own code.
5. **Whether 25s is uniform across all calls or only some** — and whether
   the overdue *row count itself* has simply grown (more equipment going
   unreturned) — rules in/out a genuine, unremarkable change in data shape
   as opposed to an infrastructure regression.
6. **Infra/config changes outside the app deploy pipeline** — a DBA-side
   config change (`work_mem`, autovacuum settings, connection pooler), an
   instance/disk change, or a noisy neighbor on a shared instance — rules
   in/out causes that wouldn't show up in the app's own deploy history at
   all.
7. **App-side connection pool saturation** — rules in/out the bottleneck
   being before the query even reaches Postgres.

**Two most likely causes, given no code deploy:**

- **A query plan regression from stale statistics or autovacuum falling
  behind**, flipping an index scan to a sequential scan as the underlying
  data crossed some threshold. Confirmed by comparing today's `EXPLAIN
  (ANALYZE, BUFFERS)` against a known-good historical plan (from
  `pg_stat_statements` or logged slow queries) and checking
  `last_autovacuum`/`n_dead_tup`.
- **Organic growth of the overdue set itself** — more overdue check-outs
  accumulating (e.g., returns processing slowing down, seasonal demand),
  meaning even a well-planned query now touches and sorts meaningfully more
  rows than before. Confirmed by directly counting current overdue rows
  and comparing to a historical baseline from dashboards or prior logs.

### D3. CI/CD and safety

**On pull request:** lint/format checks (ruff/flake8, black --check,
isort --check); the full pytest suite run against real Postgres and Redis
service containers in GitHub Actions (`services:`), not sqlite, so
concurrency/locking behavior is actually exercised; `python manage.py
makemigrations --check --dry-run` to catch missing migrations;
`python manage.py check --deploy`; and a Docker build step to confirm the
image still builds cleanly, since a broken Dockerfile is just as much a
shipping blocker as a failing test.

**On merge to main:** re-run the same suite (guards against a race between
two PRs merged close together), then build and tag the Docker image (git
SHA + environment tag), push it to a registry, and auto-deploy to a
staging environment for smoke tests (hit `/health/`, run a scripted
check-out → return → summary flow).

**What gates a production deploy:** green CI on main, a passing staging
smoke test, and — specifically because this project cares about migration
safety — any PR touching `migrations/` requires an explicit reviewer
(e.g. a CODEOWNERS rule), since a bad migration is the one class of change
that can't be undone just by redeploying old code. I'd also prefer a
canary/rolling rollout over a single big-bang cutover, with automated
error-rate/latency checks that can trigger an automatic rollback of the
*application* if the canary looks unhealthy.

**Migrations relative to code going live:** migrations are their own
explicit pipeline step, run against production *before* the new image is
rolled out to any instance (e.g., a one-off migration job that must
succeed before the deployment step proceeds) — and, per the D1 discipline,
every migration shipped through this pipeline is required to be backward
compatible with the *currently-running* app version, since a rolling
deploy across multiple instances means old code will keep serving traffic
against the new schema for a while.

**Rollback story when the schema has already migrated:** because
migrations are required to be additive/backward-compatible with the prior
app version, rolling the application back to the previous image is always
safe by itself — old code simply ignores columns/tables it doesn't use
yet. What I would never do automatically is run the schema's "down"
migration as part of an app rollback — reversing a schema change (e.g.
dropping a column other in-flight code might still reference) can cause
its own outage. Schema rollbacks are treated as a separate, manual,
reviewed action taken only if truly necessary — decoupled from "redeploy
the previous image," which is the fast, safe path that handles the vast
majority of bad deploys.

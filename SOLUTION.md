# Solution Design & Trade-offs

## Overview

The system is a single Python process with two logical components — an
HTTP API and a background worker — sharing one PostgreSQL database. The
components are designed so they can be split into separate services
without changing the contract between them.

---

## Architecture

```
┌─────────────────────────────────────────────────────┐
│                   Single Process                     │
│                                                      │
│   HTTP (FastAPI)          Background Thread          │
│   ─────────────           ─────────────────          │
│   POST /orders   ──┐      Stock Worker               │
│   GET  /orders   ──┤ SQL  polls outbox every 2s      │
│   GET  /stock    ──┼────► deducts stock              │
│   GET  /reports  ──┘      marks events done          │
│                                │                     │
└────────────────────────────────┼─────────────────────┘
                                 │
                    ┌────────────▼────────────┐
                    │       PostgreSQL         │
                    │  products               │
                    │  orders                 │
                    │  order_items            │
                    │  outbox  ◄── key table  │
                    └─────────────────────────┘
```

---

## Key design decisions

### 1. Transactional Outbox for order/stock decoupling

**Decision:** When an order is created, the API writes an `outbox` row in
the *same database transaction* as the order row. A separate worker thread
polls the outbox and applies stock deductions independently.

**Why:** This guarantees that an accepted order will always eventually
result in a stock deduction, even if the worker crashes, is paused, or is
restarted. There is no window where an order is confirmed but stock is
silently never updated — the outbox row persists until it is successfully
processed.

**Alternative considered:** Deducting stock directly inside the order
transaction. This is simpler but couples the two concerns tightly. If stock
deduction fails (e.g. a lock timeout under load), the order fails too —
even though the order itself was valid. The outbox approach keeps order
intake fast and reliable regardless of stock processing latency.

**Trade-off:** The outbox introduces a short lag between order confirmation
and stock being decremented. The daily report reflects this — `units_sold`
is derived from completed outbox events, so during an outage it will
temporarily undercount. This is documented behaviour and reconciles
automatically when the worker catches up.

---

### 2. Idempotency via database constraint

**Decision:** The `orders` table has a `UNIQUE (order_ref)` constraint.
The application attempts the INSERT and catches `UniqueViolation` —
returning the existing order with `is_duplicate: true` rather than an
error.

**Why:** The database constraint is the true safety net. Application-level
checks (read-before-write) have a TOCTOU race under concurrent requests —
two requests for the same `order_ref` could both pass the check and both
attempt the insert. The constraint makes this impossible at the storage
level regardless of concurrency.

**Trade-off:** Relying on exception handling for a normal code path
(duplicate submission) is slightly unusual. The alternative — a
`SELECT` to check existence first — would require a serialisable
transaction or advisory lock to be safe, which is more complex and
slower.

---

### 3. Stock concurrency safety with SELECT FOR UPDATE

**Decision:** The worker locks product rows with `SELECT … FOR UPDATE`
before reading and deducting stock. The lock is held until the transaction
commits, serialising concurrent deductions for the same SKU.

**Why:** Without the lock, two workers processing orders for the same SKU
concurrently would both read the same stock value, both compute the new
value, and the second write would win — losing one deduction (lost update
race).

**Additional safeguard:** The `products.stock` column has a
`CHECK (stock >= 0)` constraint. If the application logic ever miscounts,
the database rejects the update outright rather than allowing negative
stock.

---

### 4. Queue safety with SKIP LOCKED

**Decision:** The worker uses `SELECT … FOR UPDATE SKIP LOCKED` to claim
outbox rows.

**Why:** `SKIP LOCKED` allows multiple worker instances to share the same
queue without coordination. Each worker locks only the rows it will
process and skips any row already locked by another worker. This means
horizontal scaling (multiple workers) is safe without any additional
infrastructure — just run more processes pointing at the same database.

---

### 5. Single process vs two services

**Decision:** API and worker run as threads in the same process.

**Why:** The spec explicitly permits this for the take-home, and it keeps
the demo self-contained — one command starts everything. The outbox pattern
means the two components are already loosely coupled: they communicate only
through the `outbox` table, not through shared memory or direct calls.

**Splitting is straightforward:** The worker can become a separate process
or service by extracting `app/worker.py` and `app/db.py` into its own
entry point. No schema changes or protocol changes are needed — it just
reads the same `outbox` table. The `SKIP LOCKED` query already handles
multiple workers safely.

---

### 6. Option B — Daily report over Option A (integration surface)

**Decision:** Implemented the daily report endpoint rather than an
event feed/webhook consumer.

**Why:** The report is self-contained and directly verifiable — a reviewer
can see in one response whether orders, stock deductions, and revenue are
all consistent. It also exercises the JSONB aggregation in PostgreSQL
(unnesting the outbox payload to sum units per SKU), which is a meaningful
demonstration of the data model.

Option A (a feed or webhook) would require a separate consumer process and
introduces more moving parts — an event format, a consumer loop, ordering
guarantees — without adding significant proof of correctness for this
scope.

---

## Data model

```sql
products       -- catalogue; stock is the live inventory level
orders         -- one row per unique order_ref (UNIQUE constraint)
order_items    -- line items for each order
outbox         -- durable event queue; worker reads from here
```

The `outbox.payload` column is JSONB and stores the order items inline.
This avoids a join when the worker processes events and keeps the event
self-contained, which matters if the worker is ever split into a separate
service.

---

## What is intentionally out of scope

- **Authentication / authorisation** — explicitly excluded by the spec.
- **Negative stock prevention at order time** — stock is only checked
  during deduction (in the worker). An order can be accepted even if stock
  is low; the worker marks the outbox event `failed` after 3 attempts and
  logs the shortfall. A real system would want a compensation flow (cancel
  order, notify customer), but that is beyond the minimal scope here.
- **Pagination** on list endpoints — not required for the demonstrated flows.
- **Schema migrations** — a single `schema.sql` is sufficient for a
  fresh-start take-home; Alembic or similar would be appropriate for a
  production codebase.

---

## If this were going to production

- Replace the in-process worker thread with a separate horizontally
  scalable worker service — the `SKIP LOCKED` outbox polling already
  supports this.
- Add Alembic for schema migrations.
- Add authentication (OAuth2 / API keys) to all endpoints.
- Add a dead-letter queue or alerting for outbox rows that reach
  `status = failed`.
- Instrument with structured logging and metrics (Prometheus) on the
  worker poll cycle and stock deduction latency.
- Use a connection pooler (PgBouncer) in front of PostgreSQL for
  production connection counts.

---

## AI tool usage

I used an AI coding assistant during this assignment. Below are the key prompts and how I used the output.

---

**Boilerplate and project setup**

Prompt: *"Set up a FastAPI project with psycopg v3, pydantic-settings, and a connection pool. No ORM."*

Used the output as a starting point for `db.py` and `config.py`, then adjusted the pool settings and context manager behaviour to match how I wanted commits and rollbacks handled.

---

**Unit test scaffolding**

Prompt: *"Write pytest unit tests for this function using mocks — no real database. Mock get_conn() as a context manager."*

The trickiest part was getting the mock cursor to behave correctly as a context manager. The AI generated the initial fixture structure in `conftest.py`. I reviewed it, identified that the `side_effect` approach for simulating `UniqueViolation` needed adjustment for the read-back calls after rollback, and fixed that manually.

---

**Docker and Compose**

Prompt: *"Write a multi-stage Dockerfile for a Python FastAPI app and a docker-compose with a health-checked postgres dependency."*

Used the output directly with minor changes — added the non-root user and adjusted the `DATABASE_URL` to use the compose service name `db` instead of `localhost`.

---

**What I did not use AI for**

The core design decisions — the transactional outbox pattern, idempotency via `UniqueViolation`, `SELECT FOR UPDATE` for stock safety, and `SKIP LOCKED` for the worker queue — were my own. I used the AI to speed up boilerplate, not to design the system.

# Mr. D – Orders & Stock Core Flow

A minimal transactional backend built with **Python + PostgreSQL** that
accepts orders, manages stock, and exposes clean REST interfaces.

---

## Table of Contents

1. [Running with Docker Compose (recommended)](#running-with-docker-compose-recommended)
2. [Running locally (Python + PostgreSQL)](#running-locally-python--postgresql)
3. [Demonstrating the two unhappy paths](#demonstrating-the-two-unhappy-paths)
4. [API reference](#api-reference)
5. [Project layout](#project-layout)

---

## Running with Docker Compose (recommended)

The simplest way to run everything — no Python or PostgreSQL installation needed beyond Docker Desktop.

### Prerequisites

- [Docker Desktop](https://www.docker.com/products/docker-desktop/) (any recent version)

### Start

```bash
docker compose up --build
```

This will:
- Build the application image
- Start a PostgreSQL 16 container with the schema applied automatically
- Start the API on **http://localhost:8000** with the worker running inside

The API is ready when you see:

```
api-1  | INFO:     Application startup complete.
api-1  | INFO:     Uvicorn running on http://0.0.0.0:8000
```

### Run the demo scripts against the running containers

Open a second terminal:

```bash
# Seed products and submit orders (includes duplicates)
docker compose exec api python scripts/seed.py --base-url http://localhost:8000

# Or run from outside the container (Python must be installed locally)
python scripts/seed.py

# Outage simulation and catch-up demo
python scripts/demo_outage.py
```

### Stop

```bash
docker compose down          # stop containers, keep database volume
docker compose down -v       # stop containers AND delete database volume (fresh start)
```

### Rebuild after code changes

```bash
docker compose up --build
```

---

## Running locally (Python + PostgreSQL)

### Prerequisites

| Requirement | Version |
|-------------|---------|
| Python | 3.11+ |
| PostgreSQL | 14+ (via Docker or native) |

### 1. Create a virtual environment and install dependencies

```bash
python -m venv .venv

# macOS / Linux
source .venv/bin/activate

# Windows (PowerShell)
.venv\Scripts\Activate.ps1

pip install -r requirements.txt
```

### 2. Start PostgreSQL

**Docker one-liner (macOS / Linux):**

```bash
docker run -d \
  --name mrd-postgres \
  -e POSTGRES_USER=postgres \
  -e POSTGRES_PASSWORD=password \
  -e POSTGRES_DB=mrd_orders \
  -p 5432:5432 \
  --restart unless-stopped \
  postgres:16-alpine
```

**Windows PowerShell:**

```powershell
docker run -d --name mrd-postgres -e POSTGRES_USER=postgres -e POSTGRES_PASSWORD=password -e POSTGRES_DB=mrd_orders -p 5432:5432 --restart unless-stopped postgres:16-alpine
```

### 3. Configure environment

```bash
cp .env.example .env
```

The defaults match the Docker setup above — no edits needed unless your postgres is on a different host/port.

### 4. Apply the database schema

```bash
# macOS / Linux
psql $DATABASE_URL -f db/schema.sql

# Windows PowerShell
Get-Content db\schema.sql | docker exec -i mrd-postgres psql -U postgres -d mrd_orders
```

### 5. Start the server

```bash
python main.py
```

API available at **http://localhost:8000**.
Swagger UI: **http://localhost:8000/docs**

The stock worker starts automatically as a background thread in the same process.

---

## Demonstrating the two unhappy paths

Both scripts require the server to be running (either via Compose or locally).

### Unhappy path 1 — Duplicate order submissions

```bash
python scripts/seed.py
```

Seeds 5 products and submits 7 orders, intentionally including `web-100045`
and `web-100047` twice each. Prints a table showing which were new and which
were flagged as duplicates — no second order is created, no second stock
deduction queued.

Expected output (trimmed):

```
  order_ref        status        duplicate      total
  ---------------  ------------  ----------  ----------
  ✓  web-100045   confirmed     new              647¢
  ✓  web-100046   confirmed     new             1525¢
  ✓  web-100047   confirmed     new              459¢
  ✓  web-100048   confirmed     new             1324¢
  ⚠  web-100045   confirmed     ⚠ DUPLICATE      647¢
  ⚠  web-100047   confirmed     ⚠ DUPLICATE      459¢
  ✓  web-100049   confirmed     new             1894¢
```

### Unhappy path 2 — Outage simulation and catch-up

```bash
python scripts/demo_outage.py
```

Pauses the stock worker, submits 3 orders (stock unchanged — orders are still
accepted), then resumes the worker and verifies all stock deductions are applied.

Expected outcome:

```
  ✓ PASS – stock unchanged as expected          ← during pause
  ✓ PASS – stock caught up correctly after worker resumed
```

---

## API reference

### Orders

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/orders` | Create an order (idempotent on `order_ref`) |
| `GET`  | `/orders/{order_ref}` | Fetch order details and status |

**Create order request:**

```json
{
  "order_ref": "web-100045",
  "customer_id": "cust-42",
  "items": [
    {"sku": "BAN-001", "qty": 2},
    {"sku": "APL-003", "qty": 1}
  ]
}
```

**First submission** → `201 { "is_duplicate": false, "order": { ... } }`  
**Repeat submission** → `201 { "is_duplicate": true, "order": { ... } }` (same order returned)

### Stock

| Method | Path | Description |
|--------|------|-------------|
| `GET`  | `/stock/{sku}` | Current stock level and price for a SKU |

### Daily report

| Method | Path | Description |
|--------|------|-------------|
| `GET`  | `/reports/daily?date=YYYY-MM-DD` | Orders, revenue, units sold per SKU, current stock |

`date` defaults to today (UTC) if omitted.

### Worker control

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/worker/pause`  | Pause the stock worker (simulates outage) |
| `POST` | `/worker/resume` | Resume the stock worker (triggers catch-up) |
| `GET`  | `/worker/status` | Check whether the worker is currently paused |

### Products

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/products` | Upsert a product (used by seed script) |

---

## Project layout

```
mr-d/
├── app/
│   ├── api.py            # FastAPI app and all endpoints
│   ├── config.py         # Settings loaded from .env
│   ├── db.py             # psycopg v3 connection pool
│   ├── orders.py         # Orders domain – create, fetch, idempotency
│   ├── products.py       # Products & stock – upsert, deduct
│   └── worker.py         # Outbox worker thread + pause/resume
├── db/
│   └── schema.sql        # DDL – applied automatically by Docker Compose
├── scripts/
│   ├── seed.py           # Demo: products + order burst with duplicates
│   └── demo_outage.py    # Demo: pause worker → orders → resume → verify
├── main.py               # Uvicorn entry point
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
├── .env.example
├── README.md
└── SOLUTION.md
```

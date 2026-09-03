"""
FastAPI application.

Endpoints
---------
Orders
    POST   /orders                    Create (or return duplicate) order
    GET    /orders/{order_ref}        Fetch order details + status

Stock
    GET    /stock/{sku}               Current stock level for a SKU

Report  (Option B)
    GET    /reports/daily?date=YYYY-MM-DD
           Returns for the given day:
             - total_orders
             - revenue_cents
             - units_sold_per_sku  [{sku, units_sold}]
             - stock_per_sku       [{sku, current_stock}]

Worker control  (for outage demo)
    POST   /worker/pause
    POST   /worker/resume
    GET    /worker/status
"""

from __future__ import annotations

import threading
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Optional

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, field_validator

from app import worker as stock_worker
from app.db import close_pool, get_conn
from app.orders import CreateOrderResult, OrderRequest, create_order, get_order
from app.products import get_product, upsert_product


# ---------------------------------------------------------------------------
# Lifespan: launch worker thread on startup, clean up on shutdown
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    # startup
    t = threading.Thread(target=stock_worker.run_worker, daemon=True, name="stock-worker")
    t.start()
    yield
    # shutdown
    stock_worker.stop_worker()
    close_pool()


app = FastAPI(title="Mr. D – Orders & Stock", version="1.0.0", lifespan=lifespan)


# ===========================================================================
# Request / Response schemas
# ===========================================================================

class OrderItemIn(BaseModel):
    sku: str
    qty: int

    @field_validator("qty")
    @classmethod
    def qty_positive(cls, v: int) -> int:
        if v <= 0:
            raise ValueError("qty must be > 0")
        return v


class CreateOrderIn(BaseModel):
    order_ref: str
    customer_id: str
    items: list[OrderItemIn]

    @field_validator("items")
    @classmethod
    def items_not_empty(cls, v: list) -> list:
        if not v:
            raise ValueError("items must not be empty")
        return v


class OrderItemOut(BaseModel):
    sku: str
    qty: int
    unit_price_cents: int


class OrderOut(BaseModel):
    id: int
    order_ref: str
    customer_id: str
    status: str
    total_cents: int
    created_at: str
    items: list[OrderItemOut]


class CreateOrderOut(BaseModel):
    order: OrderOut
    is_duplicate: bool


class StockOut(BaseModel):
    sku: str
    name: str
    stock: int
    price_cents: int


# Report schemas
class SkuUnits(BaseModel):
    sku: str
    units_sold: int


class SkuStock(BaseModel):
    sku: str
    current_stock: int


class DailyReportOut(BaseModel):
    date: str
    total_orders: int
    revenue_cents: int
    units_sold_per_sku: list[SkuUnits]
    stock_per_sku: list[SkuStock]


class WorkerStatusOut(BaseModel):
    paused: bool


# Product upsert (used by seed script via HTTP or directly)
class UpsertProductIn(BaseModel):
    sku: str
    name: str
    price_cents: int
    stock: int


# ===========================================================================
# Helpers
# ===========================================================================

def _order_result_to_response(result: CreateOrderResult) -> CreateOrderOut:
    o = result.order
    return CreateOrderOut(
        is_duplicate=result.is_duplicate,
        order=OrderOut(
            id=o.id,
            order_ref=o.order_ref,
            customer_id=o.customer_id,
            status=o.status,
            total_cents=o.total_cents,
            created_at=o.created_at,
            items=[
                OrderItemOut(
                    sku=i.sku,
                    qty=i.qty,
                    unit_price_cents=i.unit_price_cents,
                )
                for i in o.items
            ],
        ),
    )


# ===========================================================================
# Orders endpoints
# ===========================================================================

@app.post("/orders", response_model=CreateOrderOut, status_code=201)
def post_order(body: CreateOrderIn) -> CreateOrderOut:
    """Accept a new order.

    * Returns 201 with is_duplicate=false on first submission.
    * Returns 201 with is_duplicate=true (and the original order) on repeat
      submissions with the same order_ref — no second record is created.
    * Returns 422 if any SKU is unknown.
    """
    try:
        result = create_order(
            OrderRequest(
                order_ref=body.order_ref,
                customer_id=body.customer_id,
                items=[{"sku": i.sku, "qty": i.qty} for i in body.items],
            )
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    return _order_result_to_response(result)


@app.get("/orders/{order_ref}", response_model=OrderOut)
def fetch_order(order_ref: str) -> OrderOut:
    """Fetch order details and current status by order_ref."""
    order = get_order(order_ref)
    if not order:
        raise HTTPException(status_code=404, detail=f"Order '{order_ref}' not found")

    return OrderOut(
        id=order.id,
        order_ref=order.order_ref,
        customer_id=order.customer_id,
        status=order.status,
        total_cents=order.total_cents,
        created_at=order.created_at,
        items=[
            OrderItemOut(sku=i.sku, qty=i.qty, unit_price_cents=i.unit_price_cents)
            for i in order.items
        ],
    )


# ===========================================================================
# Stock endpoint
# ===========================================================================

@app.get("/stock/{sku}", response_model=StockOut)
def fetch_stock(sku: str) -> StockOut:
    """Return current stock level (and price) for a SKU."""
    product = get_product(sku)
    if not product:
        raise HTTPException(status_code=404, detail=f"SKU '{sku}' not found")
    return StockOut(
        sku=product.sku,
        name=product.name,
        stock=product.stock,
        price_cents=product.price_cents,
    )


# ===========================================================================
# Products upsert (used by seed script)
# ===========================================================================

@app.post("/products", status_code=201)
def post_product(body: UpsertProductIn) -> dict:
    p = upsert_product(
        sku=body.sku,
        name=body.name,
        price_cents=body.price_cents,
        stock=body.stock,
    )
    return {"sku": p.sku, "name": p.name, "price_cents": p.price_cents, "stock": p.stock}


# ===========================================================================
# Daily Report  (Option B)
# ===========================================================================

@app.get("/reports/daily", response_model=DailyReportOut)
def daily_report(
    date: Optional[str] = Query(
        default=None,
        description="ISO date YYYY-MM-DD (defaults to today UTC)",
    )
) -> DailyReportOut:
    """Return a daily summary:

    * total_orders   – count of confirmed orders placed on that day
    * revenue_cents  – sum of order totals for that day
    * units_sold_per_sku – units deducted (from done outbox events) per SKU
    * stock_per_sku      – current live stock per SKU
    """
    report_date: date
    if date is None:
        report_date = datetime.now(timezone.utc).date()
    else:
        try:
            report_date = datetime.strptime(date, "%Y-%m-%d").date()
        except ValueError:
            raise HTTPException(status_code=422, detail="date must be YYYY-MM-DD")

    day_start = f"{report_date} 00:00:00+00"
    day_end   = f"{report_date} 23:59:59+00"

    with get_conn() as conn:
        # --- order totals ---
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    COUNT(*)        AS total_orders,
                    COALESCE(SUM(total_cents), 0) AS revenue_cents
                FROM orders
                WHERE status = 'confirmed'
                  AND created_at BETWEEN %s AND %s
                """,
                (day_start, day_end),
            )
            totals = cur.fetchone()

        # --- units sold per SKU (from done outbox events on that day) ---
        # We derive units from completed outbox events so the report only
        # counts stock that was actually deducted (not just ordered).
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    item ->> 'sku'          AS sku,
                    SUM((item ->> 'qty')::int) AS units_sold
                FROM outbox,
                     jsonb_array_elements(payload -> 'items') AS item
                WHERE status = 'done'
                  AND processed_at BETWEEN %s AND %s
                GROUP BY sku
                ORDER BY sku
                """,
                (day_start, day_end),
            )
            units_rows = cur.fetchall()

        # --- current stock per SKU ---
        with conn.cursor() as cur:
            cur.execute(
                "SELECT sku, stock FROM products ORDER BY sku"
            )
            stock_rows = cur.fetchall()

        conn.commit()   # close implicit tx – all reads, no writes

    return DailyReportOut(
        date=str(report_date),
        total_orders=totals["total_orders"],
        revenue_cents=totals["revenue_cents"],
        units_sold_per_sku=[
            SkuUnits(sku=r["sku"], units_sold=r["units_sold"]) for r in units_rows
        ],
        stock_per_sku=[
            SkuStock(sku=r["sku"], current_stock=r["stock"]) for r in stock_rows
        ],
    )

@app.post("/worker/pause", response_model=WorkerStatusOut)
def pause() -> WorkerStatusOut:
    """Pause the stock worker (simulates stock capability being unavailable)."""
    stock_worker.pause_worker()
    return WorkerStatusOut(paused=True)


@app.post("/worker/resume", response_model=WorkerStatusOut)
def resume() -> WorkerStatusOut:
    """Resume the stock worker (catch-up begins immediately)."""
    stock_worker.resume_worker()
    return WorkerStatusOut(paused=False)


@app.get("/worker/status", response_model=WorkerStatusOut)
def worker_status() -> WorkerStatusOut:
    """Check whether the worker is currently paused."""
    return WorkerStatusOut(paused=stock_worker.is_paused())

"""
Orders domain.

Responsibilities
----------------
* Accept a new order and persist it atomically with:
    - order row (status=confirmed)
    - order_items rows
    - outbox row (event_type='order_confirmed', payload contains items)
* Idempotency: if order_ref already exists, return the existing order
  without creating a duplicate — the UNIQUE constraint on order_ref is
  the safety net; the application layer catches the UniqueViolation and
  performs a read-back.
* Fetch order details including line items.

Stock deduction is NOT done here.  The outbox worker (app/worker.py)
reads outbox rows and calls products.deduct_stock(), decoupling order
intake from stock availability.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Optional

import psycopg
from psycopg.errors import UniqueViolation

from app.db import get_conn
from app.products import get_prices


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class OrderItem:
    sku: str
    qty: int
    unit_price_cents: int


@dataclass
class Order:
    id: int
    order_ref: str
    customer_id: str
    status: str
    total_cents: int
    created_at: str
    items: list[OrderItem] = field(default_factory=list)


@dataclass
class OrderRequest:
    order_ref: str
    customer_id: str
    items: list[dict]   # [{sku, qty}, ...]


@dataclass
class CreateOrderResult:
    order: Order
    is_duplicate: bool   # True when the order_ref already existed


# ---------------------------------------------------------------------------
# Repository helpers
# ---------------------------------------------------------------------------

def _fetch_order_with_items(conn: psycopg.Connection, order_id: int) -> Order:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, order_ref, customer_id, status, total_cents,
                   created_at::text
            FROM orders WHERE id = %s
            """,
            (order_id,),
        )
        row = cur.fetchone()

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT sku, qty, unit_price_cents
            FROM order_items WHERE order_id = %s
            ORDER BY id
            """,
            (order_id,),
        )
        item_rows = cur.fetchall()

    return Order(
        id=row["id"],
        order_ref=row["order_ref"],
        customer_id=row["customer_id"],
        status=row["status"],
        total_cents=row["total_cents"],
        created_at=row["created_at"],
        items=[OrderItem(**r) for r in item_rows],
    )


def _fetch_order_by_ref(conn: psycopg.Connection, order_ref: str) -> Optional[Order]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM orders WHERE order_ref = %s",
            (order_ref,),
        )
        row = cur.fetchone()
    if not row:
        return None
    return _fetch_order_with_items(conn, row["id"])


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def create_order(req: OrderRequest) -> CreateOrderResult:
    """Create an order or return the existing one for duplicate order_refs.

    The happy path:
      1. Look up current prices for all SKUs (validates they exist).
      2. INSERT order + order_items + outbox row in a single transaction.
      3. COMMIT.

    Duplicate path:
      If the INSERT raises a UniqueViolation (duplicate order_ref), we
      rollback, fetch the existing order and return it with is_duplicate=True.
    """
    skus = [i["sku"] for i in req.items]
    prices = get_prices(skus)

    missing = [s for s in skus if s not in prices]
    if missing:
        raise ValueError(f"Unknown SKU(s): {missing}")

    total_cents = sum(prices[i["sku"]] * i["qty"] for i in req.items)

    with get_conn() as conn:
        try:
            with conn.cursor() as cur:
                # 1. Insert order
                cur.execute(
                    """
                    INSERT INTO orders (order_ref, customer_id, status, total_cents)
                    VALUES (%(order_ref)s, %(customer_id)s, 'confirmed', %(total_cents)s)
                    RETURNING id, order_ref, customer_id, status, total_cents, created_at::text
                    """,
                    dict(
                        order_ref=req.order_ref,
                        customer_id=req.customer_id,
                        total_cents=total_cents,
                    ),
                )
                order_row = cur.fetchone()
                order_id = order_row["id"]

                # 2. Insert line items
                for item in req.items:
                    cur.execute(
                        """
                        INSERT INTO order_items (order_id, sku, qty, unit_price_cents)
                        VALUES (%s, %s, %s, %s)
                        """,
                        (order_id, item["sku"], item["qty"], prices[item["sku"]]),
                    )

                # 3. Write outbox event (same transaction → atomic)
                outbox_payload = {
                    "order_id": order_id,
                    "order_ref": req.order_ref,
                    "items": [
                        {"sku": i["sku"], "qty": i["qty"]} for i in req.items
                    ],
                }
                cur.execute(
                    """
                    INSERT INTO outbox (event_type, payload)
                    VALUES ('order_confirmed', %s)
                    """,
                    (json.dumps(outbox_payload),),
                )

            conn.commit()

            order = Order(
                id=order_row["id"],
                order_ref=order_row["order_ref"],
                customer_id=order_row["customer_id"],
                status=order_row["status"],
                total_cents=order_row["total_cents"],
                created_at=order_row["created_at"],
                items=[
                    OrderItem(
                        sku=i["sku"],
                        qty=i["qty"],
                        unit_price_cents=prices[i["sku"]],
                    )
                    for i in req.items
                ],
            )
            return CreateOrderResult(order=order, is_duplicate=False)

        except UniqueViolation:
            # Duplicate order_ref — rollback and return existing order
            conn.rollback()
            existing = _fetch_order_by_ref(conn, req.order_ref)
            conn.commit()   # close the implicit tx on the read
            return CreateOrderResult(order=existing, is_duplicate=True)


def get_order(order_ref: str) -> Optional[Order]:
    """Fetch a single order by order_ref, including line items."""
    with get_conn() as conn:
        result = _fetch_order_by_ref(conn, order_ref)
        conn.commit()   # close the implicit tx so pool gets a clean connection
        return result

"""
Products & Stock domain.

Responsibilities
----------------
* Upsert / seed products.
* Read a single product (with current stock) by SKU.
* Deduct stock for a set of order items — done inside the outbox
  worker, NOT during order intake, so the two concerns are decoupled.
* Expose current stock for a SKU (used by the stock API endpoint).

All writes use SELECT … FOR UPDATE to prevent concurrent workers from
double-deducting the same stock row.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import psycopg

from app.db import get_conn


# ---------------------------------------------------------------------------
# Data classes (plain Python – no ORM)
# ---------------------------------------------------------------------------

@dataclass
class Product:
    sku: str
    name: str
    price_cents: int
    stock: int


@dataclass
class StockShortfall:
    """Raised (as exception payload) when stock cannot cover a deduction."""
    sku: str
    requested: int
    available: int


class InsufficientStockError(Exception):
    def __init__(self, shortfalls: list[StockShortfall]):
        self.shortfalls = shortfalls
        super().__init__(f"Insufficient stock: {shortfalls}")


# ---------------------------------------------------------------------------
# Repository
# ---------------------------------------------------------------------------

def upsert_product(sku: str, name: str, price_cents: int, stock: int) -> Product:
    """Insert or update a product.  Stock is only set on INSERT; subsequent
    calls update name/price but leave stock untouched so existing inventory
    isn't accidentally overwritten by a re-seed.
    """
    sql = """
        INSERT INTO products (sku, name, price_cents, stock)
        VALUES (%(sku)s, %(name)s, %(price_cents)s, %(stock)s)
        ON CONFLICT (sku) DO UPDATE
            SET name        = EXCLUDED.name,
                price_cents = EXCLUDED.price_cents
        RETURNING sku, name, price_cents, stock
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, dict(sku=sku, name=name, price_cents=price_cents, stock=stock))
            row = cur.fetchone()
        conn.commit()
    return Product(**row)


def get_product(sku: str) -> Optional[Product]:
    """Fetch a product by SKU.  Returns None if not found."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT sku, name, price_cents, stock FROM products WHERE sku = %s",
                (sku,),
            )
            row = cur.fetchone()
        conn.commit()
    return Product(**row) if row else None


def get_prices(skus: list[str]) -> dict[str, int]:
    """Return {sku: price_cents} for the given SKUs in one query."""
    if not skus:
        return {}
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT sku, price_cents FROM products WHERE sku = ANY(%s)",
                (skus,),
            )
            rows = cur.fetchall()
        conn.commit()
    return {r["sku"]: r["price_cents"] for r in rows}


def deduct_stock(conn: psycopg.Connection, items: list[dict]) -> None:
    """Deduct stock for a list of {sku, qty} dicts.

    Must be called with an *open, uncommitted* connection so the deduction
    and the outbox status update happen atomically in the caller's transaction.

    Raises InsufficientStockError if any SKU cannot cover the requested qty.
    Uses SELECT … FOR UPDATE to serialise concurrent deductions.
    """
    skus = [i["sku"] for i in items]
    qty_map = {i["sku"]: i["qty"] for i in items}

    with conn.cursor() as cur:
        cur.execute(
            "SELECT sku, stock FROM products WHERE sku = ANY(%s) FOR UPDATE",
            (skus,),
        )
        rows = cur.fetchall()

    stock_map = {r["sku"]: r["stock"] for r in rows}

    shortfalls = [
        StockShortfall(sku=sku, requested=qty, available=stock_map.get(sku, 0))
        for sku, qty in qty_map.items()
        if stock_map.get(sku, 0) < qty
    ]
    if shortfalls:
        raise InsufficientStockError(shortfalls)

    with conn.cursor() as cur:
        for sku, qty in qty_map.items():
            cur.execute(
                "UPDATE products SET stock = stock - %s WHERE sku = %s",
                (qty, sku),
            )

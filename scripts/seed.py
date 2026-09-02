"""
Seed script – Task 1 demo
=========================
1. Upserts a catalogue of products.
2. Submits a burst of orders, intentionally including duplicate order_refs.
3. Prints a clear summary showing which submissions were new vs duplicate.

Usage
-----
    python scripts/seed.py [--base-url http://localhost:8000]

The API must be running before you execute this script.
"""

from __future__ import annotations

import argparse
import json
import sys
import time

import httpx

# ---------------------------------------------------------------------------
# Catalogue
# ---------------------------------------------------------------------------
PRODUCTS = [
    {"sku": "BAN-001", "name": "Bananas 1kg",       "price_cents": 199,  "stock": 100},
    {"sku": "APL-003", "name": "Apples 500g",        "price_cents": 249,  "stock": 80},
    {"sku": "MLK-002", "name": "Full Cream Milk 1L", "price_cents": 329,  "stock": 60},
    {"sku": "BRD-007", "name": "White Bread 700g",   "price_cents": 189,  "stock": 50},
    {"sku": "EGG-012", "name": "Free Range Eggs x6", "price_cents": 459,  "stock": 40},
]

# ---------------------------------------------------------------------------
# Order burst  –  web-100045 and web-100047 appear twice (duplicates)
# ---------------------------------------------------------------------------
ORDERS = [
    # First, genuine unique orders
    {
        "order_ref":   "web-100045",
        "customer_id": "cust-42",
        "items": [{"sku": "BAN-001", "qty": 2}, {"sku": "APL-003", "qty": 1}],
    },
    {
        "order_ref":   "web-100046",
        "customer_id": "cust-17",
        "items": [{"sku": "MLK-002", "qty": 3}, {"sku": "BRD-007", "qty": 2}],
    },
    {
        "order_ref":   "web-100047",
        "customer_id": "cust-99",
        "items": [{"sku": "EGG-012", "qty": 1}],
    },
    {
        "order_ref":   "web-100048",
        "customer_id": "cust-55",
        "items": [{"sku": "BAN-001", "qty": 5}, {"sku": "MLK-002", "qty": 1}],
    },
    # ── Duplicate submissions ──────────────────────────────────────────────
    # Same order_ref; different payload doesn't matter – first one wins.
    {
        "order_ref":   "web-100045",          # DUPLICATE of first order
        "customer_id": "cust-42",
        "items": [{"sku": "BAN-001", "qty": 2}, {"sku": "APL-003", "qty": 1}],
    },
    {
        "order_ref":   "web-100047",          # DUPLICATE of third order
        "customer_id": "cust-99",
        "items": [{"sku": "EGG-012", "qty": 1}],
    },
    # One more unique order after the duplicates
    {
        "order_ref":   "web-100049",
        "customer_id": "cust-11",
        "items": [{"sku": "APL-003", "qty": 4}, {"sku": "EGG-012", "qty": 2}],
    },
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _print_section(title: str) -> None:
    print(f"\n{'=' * 60}")
    print(f"  {title}")
    print('=' * 60)


def seed_products(client: httpx.Client) -> None:
    _print_section("1. Seeding products")
    for p in PRODUCTS:
        resp = client.post("/products", json=p)
        resp.raise_for_status()
        data = resp.json()
        print(f"  ✓  {data['sku']:10s}  {data['name']:25s}  stock={data['stock']:>4d}  price={data['price_cents']}¢")


def submit_orders(client: httpx.Client) -> None:
    _print_section("2. Submitting order burst (includes intentional duplicates)")
    print(f"  {'order_ref':15s}  {'status':12s}  {'duplicate':10s}  {'total':>10s}")
    print(f"  {'-'*15}  {'-'*12}  {'-'*10}  {'-'*10}")

    for order in ORDERS:
        resp = client.post("/orders", json=order)
        if resp.status_code not in (200, 201):
            print(f"  ✗  {order['order_ref']:15s}  ERROR {resp.status_code}: {resp.text}")
            continue

        data = resp.json()
        o    = data["order"]
        dup  = "⚠ DUPLICATE" if data["is_duplicate"] else "new"
        print(
            f"  {'⚠' if data['is_duplicate'] else '✓'}  "
            f"{o['order_ref']:15s}  "
            f"{o['status']:12s}  "
            f"{dup:10s}  "
            f"{o['total_cents']:>8d}¢"
        )
        time.sleep(0.05)   # tiny pause so timestamps are distinct


def show_stock(client: httpx.Client) -> None:
    _print_section("3. Current stock levels")
    for p in PRODUCTS:
        resp = client.get(f"/stock/{p['sku']}")
        if resp.status_code == 200:
            d = resp.json()
            print(f"  {d['sku']:10s}  {d['name']:25s}  stock={d['stock']:>4d}")
        else:
            print(f"  {p['sku']}  fetch error {resp.status_code}")


def show_report(client: httpx.Client) -> None:
    _print_section("4. Daily report (today)")
    resp = client.get("/reports/daily")
    resp.raise_for_status()
    print(json.dumps(resp.json(), indent=2))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Seed products and orders")
    parser.add_argument("--base-url", default="http://localhost:8000")
    args = parser.parse_args()

    print(f"\nConnecting to {args.base_url} …")

    with httpx.Client(base_url=args.base_url, timeout=10.0) as client:
        # Quick health-check
        try:
            client.get("/docs")
        except httpx.ConnectError:
            print(f"\n✗ Cannot reach {args.base_url}. Is the server running?\n")
            sys.exit(1)

        seed_products(client)
        submit_orders(client)

        # Give the worker a moment to process the outbox before reading stock/report
        print("\n  ⏳ Waiting 3 s for worker to process outbox events …")
        time.sleep(3)

        show_stock(client)
        show_report(client)

    print("\nDone.\n")


if __name__ == "__main__":
    main()

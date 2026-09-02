"""
Outage & catch-up demo – Task 2 unhappy path #2
================================================
Demonstrates that:
  • Orders are still accepted while the stock worker is paused.
  • Stock levels do NOT change during the outage.
  • When the worker resumes it drains the backlog and stock catches up.

Steps
-----
  1. Record stock levels before the outage.
  2. Pause the worker  (POST /worker/pause).
  3. Submit three new orders while the worker is paused.
  4. Show that stock is unchanged  →  outbox rows are pending.
  5. Resume the worker (POST /worker/resume).
  6. Wait a few seconds for catch-up.
  7. Show the final stock levels and confirm deductions match.

Usage
-----
    python scripts/demo_outage.py [--base-url http://localhost:8000]

The server must be running and products must already be seeded
(run scripts/seed.py first, or the script will seed them automatically).
"""

from __future__ import annotations

import argparse
import sys
import time

import httpx

# ---------------------------------------------------------------------------
# Orders submitted during the simulated outage
# ---------------------------------------------------------------------------
OUTAGE_ORDERS = [
    {
        "order_ref":   "outage-001",
        "customer_id": "cust-77",
        "items": [{"sku": "BAN-001", "qty": 3}],
    },
    {
        "order_ref":   "outage-002",
        "customer_id": "cust-88",
        "items": [{"sku": "APL-003", "qty": 2}, {"sku": "MLK-002", "qty": 1}],
    },
    {
        "order_ref":   "outage-003",
        "customer_id": "cust-99",
        "items": [{"sku": "EGG-012", "qty": 2}],
    },
]

# SKUs we care about in this demo
TRACKED_SKUS = ["BAN-001", "APL-003", "MLK-002", "EGG-012"]

PRODUCTS = [
    {"sku": "BAN-001", "name": "Bananas 1kg",       "price_cents": 199, "stock": 100},
    {"sku": "APL-003", "name": "Apples 500g",        "price_cents": 249, "stock": 80},
    {"sku": "MLK-002", "name": "Full Cream Milk 1L", "price_cents": 329, "stock": 60},
    {"sku": "BRD-007", "name": "White Bread 700g",   "price_cents": 189, "stock": 50},
    {"sku": "EGG-012", "name": "Free Range Eggs x6", "price_cents": 459, "stock": 40},
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def divider(title: str) -> None:
    print(f"\n{'─' * 60}")
    print(f"  {title}")
    print('─' * 60)


def get_stock_snapshot(client: httpx.Client, skus: list[str]) -> dict[str, int]:
    """Return {sku: stock} for the given SKUs."""
    snapshot: dict[str, int] = {}
    for sku in skus:
        r = client.get(f"/stock/{sku}")
        if r.status_code == 200:
            snapshot[sku] = r.json()["stock"]
        else:
            snapshot[sku] = -1   # unknown
    return snapshot


def print_stock(label: str, snapshot: dict[str, int]) -> None:
    print(f"\n  {label}")
    for sku, qty in sorted(snapshot.items()):
        print(f"    {sku:10s}  stock = {qty:>4d}")


def ensure_products(client: httpx.Client) -> None:
    """Upsert products so the demo works even without running seed.py first."""
    for p in PRODUCTS:
        r = client.post("/products", json=p)
        if r.status_code not in (200, 201):
            print(f"  ✗ Could not upsert {p['sku']}: {r.status_code} {r.text}")
            sys.exit(1)


def submit_order(client: httpx.Client, order: dict) -> dict:
    r = client.post("/orders", json=order)
    r.raise_for_status()
    return r.json()


def worker_status(client: httpx.Client) -> bool:
    """Return True if worker is paused."""
    return client.get("/worker/status").json()["paused"]


# ---------------------------------------------------------------------------
# Main demo
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Outage & catch-up demo")
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument(
        "--resume-delay",
        type=int,
        default=5,
        help="Seconds to keep the worker paused (default: 5)",
    )
    args = parser.parse_args()

    print(f"\n{'=' * 60}")
    print("  Mr. D – Outage & Catch-up Demo")
    print(f"  API: {args.base_url}")
    print('=' * 60)

    with httpx.Client(base_url=args.base_url, timeout=10.0) as client:
        # Health-check
        try:
            client.get("/docs")
        except httpx.ConnectError:
            print(f"\n✗ Cannot reach {args.base_url}. Is the server running?\n")
            sys.exit(1)

        # ── 0. Ensure products exist ─────────────────────────────────────
        divider("Step 0 – Ensuring products are seeded")
        ensure_products(client)
        print("  Products OK")

        # ── 1. Baseline stock snapshot ───────────────────────────────────
        divider("Step 1 – Baseline stock (before outage)")
        before = get_stock_snapshot(client, TRACKED_SKUS)
        print_stock("Stock before outage:", before)

        # ── 2. Pause worker ──────────────────────────────────────────────
        divider("Step 2 – Pausing the stock worker (simulating outage)")
        r = client.post("/worker/pause")
        r.raise_for_status()
        print(f"  Worker paused: {worker_status(client)}")

        # ── 3. Submit orders during outage ───────────────────────────────
        divider("Step 3 – Submitting orders WHILE worker is paused")
        print("  (Orders are accepted; outbox events queue up but stock is NOT yet deducted)\n")
        submitted_refs = []
        for order in OUTAGE_ORDERS:
            result = submit_order(client, order)
            o = result["order"]
            submitted_refs.append(o["order_ref"])
            print(
                f"  ✓ {o['order_ref']:15s}  status={o['status']:12s}  "
                f"total={o['total_cents']:>8d}¢  duplicate={result['is_duplicate']}"
            )
            time.sleep(0.1)

        # ── 4. Stock check during outage ─────────────────────────────────
        divider("Step 4 – Stock during outage (should be UNCHANGED)")
        during = get_stock_snapshot(client, TRACKED_SKUS)
        print_stock("Stock during outage:", during)

        unchanged = all(during[s] == before[s] for s in TRACKED_SKUS)
        verdict = "✓ PASS – stock unchanged as expected" if unchanged else "✗ FAIL – stock changed unexpectedly!"
        print(f"\n  {verdict}")

        # ── 5. Resume worker ─────────────────────────────────────────────
        divider(f"Step 5 – Keeping worker paused for {args.resume_delay}s then resuming")
        print(f"  Sleeping {args.resume_delay}s to make the pause visible …")
        time.sleep(args.resume_delay)

        r = client.post("/worker/resume")
        r.raise_for_status()
        print(f"  Worker paused: {worker_status(client)}")
        print("  Worker resumed – catch-up in progress …")

        # ── 6. Wait for catch-up ─────────────────────────────────────────
        divider("Step 6 – Waiting for worker to drain the backlog")
        # Poll until all submitted orders' outbox events are 'done' (max 15s)
        deadline = time.time() + 15
        caught_up = False
        while time.time() < deadline:
            time.sleep(1)
            after = get_stock_snapshot(client, TRACKED_SKUS)
            if any(after[s] < before[s] for s in TRACKED_SKUS):
                caught_up = True
                break
            print("  … still catching up")

        # ── 7. Final stock comparison ────────────────────────────────────
        divider("Step 7 – Final stock (after catch-up)")
        after = get_stock_snapshot(client, TRACKED_SKUS)
        print_stock("Stock after catch-up:", after)

        # Calculate expected deductions from OUTAGE_ORDERS
        expected_deductions: dict[str, int] = {}
        for order in OUTAGE_ORDERS:
            for item in order["items"]:
                expected_deductions[item["sku"]] = (
                    expected_deductions.get(item["sku"], 0) + item["qty"]
                )

        print("\n  Deduction check:")
        print(f"  {'SKU':10s}  {'before':>8s}  {'after':>8s}  {'deducted':>10s}  {'expected':>10s}  {'ok?':>6s}")
        print(f"  {'-'*10}  {'-'*8}  {'-'*8}  {'-'*10}  {'-'*10}  {'-'*6}")
        all_ok = True
        for sku in sorted(TRACKED_SKUS):
            b = before[sku]
            a = after[sku]
            deducted = b - a
            expected = expected_deductions.get(sku, 0)
            ok = deducted == expected
            if not ok:
                all_ok = False
            print(
                f"  {sku:10s}  {b:>8d}  {a:>8d}  {deducted:>10d}  {expected:>10d}  {'✓' if ok else '✗':>6s}"
            )

        print()
        if caught_up and all_ok:
            print("  ✓ PASS – stock caught up correctly after worker resumed")
        else:
            print("  ✗ FAIL – catch-up check failed (worker may still be processing)")

    print("\nDemo complete.\n")


if __name__ == "__main__":
    main()

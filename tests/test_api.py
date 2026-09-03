"""
Unit tests – API endpoints (app/api.py)

Uses FastAPI's TestClient (synchronous). The worker thread and DB pool
are patched out so no real infrastructure is needed.

Covered
-------
POST /orders
  - 201 + is_duplicate=false on new order
  - 201 + is_duplicate=true on duplicate order_ref
  - 422 on unknown SKU
  - 422 on missing required fields
  - 422 on qty <= 0

GET /orders/{order_ref}
  - 200 with order data when found
  - 404 when not found

GET /stock/{sku}
  - 200 with stock data when found
  - 404 when not found

GET /reports/daily
  - 200 with correct shape
  - 422 on invalid date format

POST /worker/pause + /worker/resume
  - correct paused flag in response

GET /worker/status
  - reflects current worker state
"""

from __future__ import annotations

from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.orders import CreateOrderResult, Order, OrderItem
from app.products import Product


# ---------------------------------------------------------------------------
# App fixture – patch out the worker thread and DB pool for all tests
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def client():
    """
    Create a TestClient with the worker thread and pool lifecycle patched
    so tests don't need a real DB or background thread.
    """
    with patch("app.api.stock_worker.run_worker"), \
         patch("app.api.stock_worker.stop_worker"), \
         patch("app.api.close_pool"):
        from app.api import app
        with TestClient(app, raise_server_exceptions=True) as c:
            yield c


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_order(order_ref="web-001", is_dup=False):
    order = Order(
        id=1,
        order_ref=order_ref,
        customer_id="cust-1",
        status="confirmed",
        total_cents=647,
        created_at="2026-09-02 10:00:00+00",
        items=[
            OrderItem(sku="BAN-001", qty=2, unit_price_cents=199),
            OrderItem(sku="APL-003", qty=1, unit_price_cents=249),
        ],
    )
    return CreateOrderResult(order=order, is_duplicate=is_dup)


_ORDER_BODY = {
    "order_ref": "web-001",
    "customer_id": "cust-1",
    "items": [
        {"sku": "BAN-001", "qty": 2},
        {"sku": "APL-003", "qty": 1},
    ],
}


# ---------------------------------------------------------------------------
# POST /orders
# ---------------------------------------------------------------------------

class TestPostOrders:
    def test_new_order_returns_201(self, client):
        with patch("app.api.create_order", return_value=_make_order()):
            resp = client.post("/orders", json=_ORDER_BODY)
        assert resp.status_code == 201

    def test_new_order_is_duplicate_false(self, client):
        with patch("app.api.create_order", return_value=_make_order(is_dup=False)):
            resp = client.post("/orders", json=_ORDER_BODY)
        assert resp.json()["is_duplicate"] is False

    def test_duplicate_order_is_duplicate_true(self, client):
        with patch("app.api.create_order",
                   return_value=_make_order(is_dup=True)):
            resp = client.post("/orders", json=_ORDER_BODY)
        assert resp.status_code == 201
        assert resp.json()["is_duplicate"] is True

    def test_duplicate_returns_original_order_ref(self, client):
        with patch("app.api.create_order",
                   return_value=_make_order(order_ref="web-001", is_dup=True)):
            resp = client.post("/orders", json=_ORDER_BODY)
        assert resp.json()["order"]["order_ref"] == "web-001"

    def test_unknown_sku_returns_422(self, client):
        with patch("app.api.create_order",
                   side_effect=ValueError("Unknown SKU(s): ['GHOST-000']")):
            resp = client.post("/orders", json=_ORDER_BODY)
        assert resp.status_code == 422

    def test_missing_order_ref_returns_422(self, client):
        body = {k: v for k, v in _ORDER_BODY.items() if k != "order_ref"}
        resp = client.post("/orders", json=body)
        assert resp.status_code == 422

    def test_empty_items_returns_422(self, client):
        resp = client.post("/orders", json={**_ORDER_BODY, "items": []})
        assert resp.status_code == 422

    def test_zero_qty_returns_422(self, client):
        resp = client.post("/orders", json={
            **_ORDER_BODY,
            "items": [{"sku": "BAN-001", "qty": 0}],
        })
        assert resp.status_code == 422

    def test_negative_qty_returns_422(self, client):
        resp = client.post("/orders", json={
            **_ORDER_BODY,
            "items": [{"sku": "BAN-001", "qty": -1}],
        })
        assert resp.status_code == 422

    def test_response_contains_order_total(self, client):
        with patch("app.api.create_order", return_value=_make_order()):
            resp = client.post("/orders", json=_ORDER_BODY)
        assert resp.json()["order"]["total_cents"] == 647

    def test_response_contains_line_items(self, client):
        with patch("app.api.create_order", return_value=_make_order()):
            resp = client.post("/orders", json=_ORDER_BODY)
        items = resp.json()["order"]["items"]
        assert len(items) == 2
        skus = {i["sku"] for i in items}
        assert skus == {"BAN-001", "APL-003"}


# ---------------------------------------------------------------------------
# GET /orders/{order_ref}
# ---------------------------------------------------------------------------

class TestGetOrder:
    def _order(self):
        return Order(
            id=1, order_ref="web-001", customer_id="cust-1",
            status="confirmed", total_cents=647,
            created_at="2026-09-02 10:00:00+00",
            items=[OrderItem(sku="BAN-001", qty=2, unit_price_cents=199)],
        )

    def test_returns_200_when_found(self, client):
        with patch("app.api.get_order", return_value=self._order()):
            resp = client.get("/orders/web-001")
        assert resp.status_code == 200

    def test_response_has_correct_order_ref(self, client):
        with patch("app.api.get_order", return_value=self._order()):
            resp = client.get("/orders/web-001")
        assert resp.json()["order_ref"] == "web-001"

    def test_returns_404_when_not_found(self, client):
        with patch("app.api.get_order", return_value=None):
            resp = client.get("/orders/does-not-exist")
        assert resp.status_code == 404

    def test_404_message_contains_order_ref(self, client):
        with patch("app.api.get_order", return_value=None):
            resp = client.get("/orders/missing-ref")
        assert "missing-ref" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# GET /stock/{sku}
# ---------------------------------------------------------------------------

class TestGetStock:
    def _product(self):
        return Product(sku="BAN-001", name="Bananas 1kg",
                       price_cents=199, stock=45)

    def test_returns_200_when_found(self, client):
        with patch("app.api.get_product", return_value=self._product()):
            resp = client.get("/stock/BAN-001")
        assert resp.status_code == 200

    def test_response_has_correct_stock(self, client):
        with patch("app.api.get_product", return_value=self._product()):
            resp = client.get("/stock/BAN-001")
        assert resp.json()["stock"] == 45

    def test_returns_404_when_not_found(self, client):
        with patch("app.api.get_product", return_value=None):
            resp = client.get("/stock/NOPE-000")
        assert resp.status_code == 404

    def test_404_message_contains_sku(self, client):
        with patch("app.api.get_product", return_value=None):
            resp = client.get("/stock/NOPE-000")
        assert "NOPE-000" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# GET /reports/daily
# ---------------------------------------------------------------------------

@contextmanager
def _ctx(conn):
    yield conn


class TestDailyReport:
    def _mock_report_conn(self):
        conn = MagicMock()
        cur = MagicMock()
        cur.__enter__ = MagicMock(return_value=cur)
        cur.__exit__ = MagicMock(return_value=False)
        # Sequence: totals row, units rows, stock rows
        cur.fetchone.return_value = {"total_orders": 3, "revenue_cents": 1500}
        cur.fetchall.side_effect = [
            [{"sku": "BAN-001", "units_sold": 5}],   # units sold
            [{"sku": "BAN-001", "stock": 95}],         # stock levels
        ]
        conn.cursor.return_value = cur
        return conn

    def test_returns_200(self, client):
        conn = self._mock_report_conn()
        with patch("app.api.get_conn", lambda: _ctx(conn)):
            resp = client.get("/reports/daily?date=2026-09-02")
        assert resp.status_code == 200

    def test_response_has_required_fields(self, client):
        conn = self._mock_report_conn()
        with patch("app.api.get_conn", lambda: _ctx(conn)):
            resp = client.get("/reports/daily?date=2026-09-02")
        data = resp.json()
        assert "total_orders" in data
        assert "revenue_cents" in data
        assert "units_sold_per_sku" in data
        assert "stock_per_sku" in data

    def test_defaults_to_today_when_no_date(self, client):
        conn = self._mock_report_conn()
        with patch("app.api.get_conn", lambda: _ctx(conn)):
            resp = client.get("/reports/daily")
        assert resp.status_code == 200

    def test_invalid_date_format_returns_422(self, client):
        resp = client.get("/reports/daily?date=not-a-date")
        assert resp.status_code == 422

    def test_total_orders_value(self, client):
        conn = self._mock_report_conn()
        with patch("app.api.get_conn", lambda: _ctx(conn)):
            resp = client.get("/reports/daily?date=2026-09-02")
        assert resp.json()["total_orders"] == 3

    def test_revenue_cents_value(self, client):
        conn = self._mock_report_conn()
        with patch("app.api.get_conn", lambda: _ctx(conn)):
            resp = client.get("/reports/daily?date=2026-09-02")
        assert resp.json()["revenue_cents"] == 1500


# ---------------------------------------------------------------------------
# POST /worker/pause and /worker/resume
# ---------------------------------------------------------------------------

class TestWorkerControl:
    def test_pause_returns_paused_true(self, client):
        with patch("app.api.stock_worker.pause_worker"), \
             patch("app.api.stock_worker.is_paused", return_value=True):
            resp = client.post("/worker/pause")
        assert resp.status_code == 200
        assert resp.json()["paused"] is True

    def test_resume_returns_paused_false(self, client):
        with patch("app.api.stock_worker.resume_worker"), \
             patch("app.api.stock_worker.is_paused", return_value=False):
            resp = client.post("/worker/resume")
        assert resp.status_code == 200
        assert resp.json()["paused"] is False

    def test_status_reflects_paused_state(self, client):
        with patch("app.api.stock_worker.is_paused", return_value=True):
            resp = client.get("/worker/status")
        assert resp.json()["paused"] is True

    def test_status_reflects_running_state(self, client):
        with patch("app.api.stock_worker.is_paused", return_value=False):
            resp = client.get("/worker/status")
        assert resp.json()["paused"] is False

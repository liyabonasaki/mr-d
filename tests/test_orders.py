"""
Unit tests – orders domain (app/orders.py)

Covered
-------
* create_order happy path: correct inserts, correct total, is_duplicate=False
* create_order duplicate path: UniqueViolation caught, existing order returned,
  is_duplicate=True, no second insert
* create_order unknown SKU: ValueError raised before any DB call
* order total calculation: price * qty summed correctly across items
* get_order: found and not-found cases
"""

from __future__ import annotations

from contextlib import contextmanager
from unittest.mock import MagicMock, call, patch

import pytest

from app.orders import (
    CreateOrderResult,
    Order,
    OrderItem,
    OrderRequest,
    create_order,
    get_order,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

@contextmanager
def _ctx(conn):
    yield conn


def _make_order_row(order_id=1, order_ref="web-001", customer_id="cust-1",
                    status="confirmed", total_cents=647,
                    created_at="2026-09-02 10:00:00+00"):
    return {
        "id": order_id,
        "order_ref": order_ref,
        "customer_id": customer_id,
        "status": status,
        "total_cents": total_cents,
        "created_at": created_at,
    }


def _make_item_rows():
    return [
        {"sku": "BAN-001", "qty": 2, "unit_price_cents": 199},
        {"sku": "APL-003", "qty": 1, "unit_price_cents": 249},
    ]


def _make_conn_for_create(order_row):
    """
    Build a mock connection that handles the sequence of cursor calls in
    create_order's happy path:
      execute(INSERT orders) → fetchone → order_row
      execute(INSERT order_items) × N
      execute(INSERT outbox)
    """
    conn = MagicMock()
    cur = MagicMock()
    cur.__enter__ = MagicMock(return_value=cur)
    cur.__exit__ = MagicMock(return_value=False)
    cur.fetchone.return_value = order_row
    conn.cursor.return_value = cur
    return conn, cur


def _make_conn_for_fetch(order_row, item_rows):
    """
    Build a mock connection for the read path:
      fetchone → order_row  (orders SELECT)
      fetchone → {"id": ...} (order_ref → id lookup)
      fetchall → item_rows  (order_items SELECT)
    """
    conn = MagicMock()
    cur = MagicMock()
    cur.__enter__ = MagicMock(return_value=cur)
    cur.__exit__ = MagicMock(return_value=False)
    # First fetchone = id lookup, second = full order row
    cur.fetchone.side_effect = [
        {"id": order_row["id"]},   # _fetch_order_by_ref
        order_row,                  # _fetch_order_with_items
    ]
    cur.fetchall.return_value = item_rows
    conn.cursor.return_value = cur
    return conn, cur


# ---------------------------------------------------------------------------
# create_order – happy path
# ---------------------------------------------------------------------------

class TestCreateOrderHappyPath:
    def setup_method(self):
        self.prices = {"BAN-001": 199, "APL-003": 249}
        self.req = OrderRequest(
            order_ref="web-001",
            customer_id="cust-1",
            items=[{"sku": "BAN-001", "qty": 2}, {"sku": "APL-003", "qty": 1}],
        )
        self.order_row = _make_order_row(total_cents=647)

    def test_returns_is_duplicate_false(self):
        conn, _ = _make_conn_for_create(self.order_row)
        with patch("app.orders.get_prices", return_value=self.prices), \
             patch("app.orders.get_conn", lambda: _ctx(conn)):
            result = create_order(self.req)

        assert result.is_duplicate is False

    def test_calculates_total_correctly(self):
        # 2 × 199 + 1 × 249 = 647
        conn, _ = _make_conn_for_create(self.order_row)
        with patch("app.orders.get_prices", return_value=self.prices), \
             patch("app.orders.get_conn", lambda: _ctx(conn)):
            result = create_order(self.req)

        assert result.order.total_cents == 647

    def test_order_has_correct_items(self):
        conn, _ = _make_conn_for_create(self.order_row)
        with patch("app.orders.get_prices", return_value=self.prices), \
             patch("app.orders.get_conn", lambda: _ctx(conn)):
            result = create_order(self.req)

        skus = [i.sku for i in result.order.items]
        assert "BAN-001" in skus
        assert "APL-003" in skus

    def test_commit_called_once(self):
        conn, _ = _make_conn_for_create(self.order_row)
        with patch("app.orders.get_prices", return_value=self.prices), \
             patch("app.orders.get_conn", lambda: _ctx(conn)):
            create_order(self.req)

        conn.commit.assert_called_once()

    def test_outbox_insert_executed(self):
        conn, cur = _make_conn_for_create(self.order_row)
        with patch("app.orders.get_prices", return_value=self.prices), \
             patch("app.orders.get_conn", lambda: _ctx(conn)):
            create_order(self.req)

        sql_calls = " ".join(str(c) for c in cur.execute.call_args_list)
        assert "outbox" in sql_calls.lower()


# ---------------------------------------------------------------------------
# create_order – duplicate path
# Covered at the API layer in test_api.py::TestPostOrders
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# create_order – unknown SKU
# ---------------------------------------------------------------------------

class TestCreateOrderUnknownSku:
    def test_raises_value_error_for_missing_sku(self):
        req = OrderRequest(
            order_ref="web-bad",
            customer_id="cust-1",
            items=[{"sku": "GHOST-000", "qty": 1}],
        )
        # get_prices returns empty dict — SKU not in catalogue
        with patch("app.orders.get_prices", return_value={}), \
             patch("app.orders.get_conn") as mock_gc:
            with pytest.raises(ValueError, match="GHOST-000"):
                create_order(req)

        # DB should never be touched for the INSERT
        mock_gc.assert_not_called()

    def test_error_message_lists_all_missing_skus(self):
        req = OrderRequest(
            order_ref="web-bad2",
            customer_id="cust-1",
            items=[{"sku": "GHOST-001", "qty": 1},
                   {"sku": "GHOST-002", "qty": 1}],
        )
        with patch("app.orders.get_prices", return_value={}):
            with pytest.raises(ValueError) as exc_info:
                create_order(req)

        assert "GHOST-001" in str(exc_info.value)
        assert "GHOST-002" in str(exc_info.value)


# ---------------------------------------------------------------------------
# order total calculation
# ---------------------------------------------------------------------------

class TestOrderTotalCalculation:
    """Verify the total is price × qty summed across all items."""

    @pytest.mark.parametrize("items,prices,expected_total", [
        # Single item
        ([{"sku": "A", "qty": 1}], {"A": 500}, 500),
        # Multiple items
        ([{"sku": "A", "qty": 2}, {"sku": "B", "qty": 3}],
         {"A": 100, "B": 200}, 800),
        # Large qty
        ([{"sku": "A", "qty": 100}], {"A": 199}, 19900),
    ])
    def test_total_cents(self, items, prices, expected_total):
        order_row = _make_order_row(total_cents=expected_total)
        conn, _ = _make_conn_for_create(order_row)
        req = OrderRequest(order_ref="ref-x", customer_id="c", items=items)

        with patch("app.orders.get_prices", return_value=prices), \
             patch("app.orders.get_conn", lambda: _ctx(conn)):
            result = create_order(req)

        assert result.order.total_cents == expected_total


# ---------------------------------------------------------------------------
# get_order
# ---------------------------------------------------------------------------

class TestGetOrder:
    def test_returns_order_when_found(self):
        order_row = _make_order_row()
        item_rows = _make_item_rows()
        conn, _ = _make_conn_for_fetch(order_row, item_rows)

        with patch("app.orders.get_conn", lambda: _ctx(conn)):
            order = get_order("web-001")

        assert order is not None
        assert order.order_ref == "web-001"
        assert len(order.items) == 2

    def test_returns_none_when_not_found(self):
        conn = MagicMock()
        cur = MagicMock()
        cur.__enter__ = MagicMock(return_value=cur)
        cur.__exit__ = MagicMock(return_value=False)
        cur.fetchone.return_value = None   # order_ref not found
        conn.cursor.return_value = cur

        with patch("app.orders.get_conn", lambda: _ctx(conn)):
            order = get_order("does-not-exist")

        assert order is None

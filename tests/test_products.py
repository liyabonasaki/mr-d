"""
Unit tests – products & stock domain (app/products.py)

Covered
-------
* get_prices: returns correct dict, empty list short-circuit
* get_product: found and not-found cases
* upsert_product: returns Product from RETURNING row
* deduct_stock: happy path deductions, insufficient stock raises error,
  partial shortfall (one SKU ok, one not)
"""

from __future__ import annotations

from unittest.mock import MagicMock, call, patch

import pytest

from app.products import (
    InsufficientStockError,
    Product,
    StockShortfall,
    deduct_stock,
    get_prices,
    get_product,
    upsert_product,
)
from tests.conftest import make_conn, make_cursor


# ---------------------------------------------------------------------------
# get_prices
# ---------------------------------------------------------------------------

class TestGetPrices:
    def test_returns_sku_price_map(self):
        rows = [{"sku": "BAN-001", "price_cents": 199},
                {"sku": "APL-003", "price_cents": 249}]
        cur = make_cursor(fetchall=rows)
        conn = make_conn(cursor=cur)

        with patch("app.products.get_conn", lambda: _ctx(conn)):
            result = get_prices(["BAN-001", "APL-003"])

        assert result == {"BAN-001": 199, "APL-003": 249}

    def test_empty_list_returns_empty_dict_without_db_call(self):
        with patch("app.products.get_conn") as mock_gc:
            result = get_prices([])

        assert result == {}
        mock_gc.assert_not_called()

    def test_missing_sku_absent_from_result(self):
        # DB only returns rows for SKUs that exist
        rows = [{"sku": "BAN-001", "price_cents": 199}]
        cur = make_cursor(fetchall=rows)
        conn = make_conn(cursor=cur)

        with patch("app.products.get_conn", lambda: _ctx(conn)):
            result = get_prices(["BAN-001", "MISSING-999"])

        assert "MISSING-999" not in result
        assert result["BAN-001"] == 199


# ---------------------------------------------------------------------------
# get_product
# ---------------------------------------------------------------------------

class TestGetProduct:
    def test_returns_product_when_found(self):
        row = {"sku": "BAN-001", "name": "Bananas 1kg",
               "price_cents": 199, "stock": 50}
        cur = make_cursor(fetchone=row)
        conn = make_conn(cursor=cur)

        with patch("app.products.get_conn", lambda: _ctx(conn)):
            product = get_product("BAN-001")

        assert product == Product(sku="BAN-001", name="Bananas 1kg",
                                  price_cents=199, stock=50)

    def test_returns_none_when_not_found(self):
        cur = make_cursor(fetchone=None)
        conn = make_conn(cursor=cur)

        with patch("app.products.get_conn", lambda: _ctx(conn)):
            product = get_product("NOPE-000")

        assert product is None


# ---------------------------------------------------------------------------
# upsert_product
# ---------------------------------------------------------------------------

class TestUpsertProduct:
    def test_returns_product_from_returning_row(self):
        row = {"sku": "BAN-001", "name": "Bananas 1kg",
               "price_cents": 199, "stock": 100}
        cur = make_cursor(fetchone=row)
        conn = make_conn(cursor=cur)

        with patch("app.products.get_conn", lambda: _ctx(conn)):
            product = upsert_product("BAN-001", "Bananas 1kg", 199, 100)

        assert product.sku == "BAN-001"
        assert product.stock == 100
        conn.commit.assert_called_once()

    def test_commit_called_after_upsert(self):
        row = {"sku": "X", "name": "Y", "price_cents": 10, "stock": 5}
        cur = make_cursor(fetchone=row)
        conn = make_conn(cursor=cur)

        with patch("app.products.get_conn", lambda: _ctx(conn)):
            upsert_product("X", "Y", 10, 5)

        conn.commit.assert_called_once()


# ---------------------------------------------------------------------------
# deduct_stock
# ---------------------------------------------------------------------------

class TestDeductStock:
    def test_happy_path_issues_update_for_each_item(self):
        stock_rows = [{"sku": "BAN-001", "stock": 50},
                      {"sku": "APL-003", "stock": 30}]
        items = [{"sku": "BAN-001", "qty": 5}, {"sku": "APL-003", "qty": 3}]

        conn = MagicMock()
        cur = MagicMock()
        cur.__enter__ = MagicMock(return_value=cur)
        cur.__exit__ = MagicMock(return_value=False)
        cur.fetchall.return_value = stock_rows
        conn.cursor.return_value = cur

        deduct_stock(conn, items)

        # Two UPDATE calls expected — one per SKU
        # Filter by checking the first positional argument of each execute call
        update_calls = [
            c for c in cur.execute.call_args_list
            if c.args and str(c.args[0]).strip().startswith("UPDATE")
        ]
        assert len(update_calls) == 2

    def test_raises_insufficient_stock_error(self):
        stock_rows = [{"sku": "BAN-001", "stock": 2}]
        items = [{"sku": "BAN-001", "qty": 10}]   # requesting more than available

        conn = MagicMock()
        cur = MagicMock()
        cur.__enter__ = MagicMock(return_value=cur)
        cur.__exit__ = MagicMock(return_value=False)
        cur.fetchall.return_value = stock_rows
        conn.cursor.return_value = cur

        with pytest.raises(InsufficientStockError) as exc_info:
            deduct_stock(conn, items)

        assert len(exc_info.value.shortfalls) == 1
        shortfall = exc_info.value.shortfalls[0]
        assert shortfall.sku == "BAN-001"
        assert shortfall.requested == 10
        assert shortfall.available == 2

    def test_partial_shortfall_raises_for_failing_sku_only(self):
        # BAN-001 has enough, APL-003 does not
        stock_rows = [{"sku": "BAN-001", "stock": 50},
                      {"sku": "APL-003", "stock": 1}]
        items = [{"sku": "BAN-001", "qty": 5},
                 {"sku": "APL-003", "qty": 10}]

        conn = MagicMock()
        cur = MagicMock()
        cur.__enter__ = MagicMock(return_value=cur)
        cur.__exit__ = MagicMock(return_value=False)
        cur.fetchall.return_value = stock_rows
        conn.cursor.return_value = cur

        with pytest.raises(InsufficientStockError) as exc_info:
            deduct_stock(conn, items)

        failing_skus = [s.sku for s in exc_info.value.shortfalls]
        assert "APL-003" in failing_skus
        assert "BAN-001" not in failing_skus

    def test_no_update_issued_on_shortfall(self):
        """If any SKU is short, no UPDATE should be executed at all."""
        stock_rows = [{"sku": "BAN-001", "stock": 1}]
        items = [{"sku": "BAN-001", "qty": 99}]

        conn = MagicMock()
        cur = MagicMock()
        cur.__enter__ = MagicMock(return_value=cur)
        cur.__exit__ = MagicMock(return_value=False)
        cur.fetchall.return_value = stock_rows
        conn.cursor.return_value = cur

        with pytest.raises(InsufficientStockError):
            deduct_stock(conn, items)

        update_calls = [
            c for c in cur.execute.call_args_list
            if c.args and str(c.args[0]).strip().startswith("UPDATE")
        ]
        assert len(update_calls) == 0

    def test_unknown_sku_treated_as_zero_stock(self):
        """A SKU not in the DB has available=0 and should shortfall."""
        stock_rows = []   # DB returns nothing
        items = [{"sku": "GHOST-000", "qty": 1}]

        conn = MagicMock()
        cur = MagicMock()
        cur.__enter__ = MagicMock(return_value=cur)
        cur.__exit__ = MagicMock(return_value=False)
        cur.fetchall.return_value = stock_rows
        conn.cursor.return_value = cur

        with pytest.raises(InsufficientStockError) as exc_info:
            deduct_stock(conn, items)

        assert exc_info.value.shortfalls[0].available == 0


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

from contextlib import contextmanager

@contextmanager
def _ctx(conn):
    yield conn

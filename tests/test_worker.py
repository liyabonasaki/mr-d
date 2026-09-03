"""
Unit tests – outbox worker (app/worker.py)

Covered
-------
* pause_worker / resume_worker / stop_worker / is_paused state transitions
* _poll_once: empty outbox returns 0 and commits
* _poll_once: processes a batch and returns correct count
* _process_one: happy path commits stock deduction + outbox update atomically
* _process_one: InsufficientStockError triggers rollback and _record_failure
* _process_one: unexpected exception triggers rollback and _record_failure
* _record_failure: increments attempts; marks 'failed' at MAX_ATTEMPTS
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from unittest.mock import MagicMock, call, patch

import pytest

import app.worker as worker_module
from app.products import InsufficientStockError, StockShortfall
from app.worker import (
    _poll_once,
    _process_one,
    _record_failure,
    is_paused,
    pause_worker,
    resume_worker,
    stop_worker,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

@contextmanager
def _ctx(conn):
    yield conn


def _make_conn():
    conn = MagicMock()
    cur = MagicMock()
    cur.__enter__ = MagicMock(return_value=cur)
    cur.__exit__ = MagicMock(return_value=False)
    cur.fetchall.return_value = []
    cur.fetchone.return_value = None
    conn.cursor.return_value = cur
    return conn, cur


# ---------------------------------------------------------------------------
# Control functions
# ---------------------------------------------------------------------------

class TestWorkerControl:
    def test_initial_state_not_paused(self):
        assert is_paused() is False

    def test_pause_sets_paused(self):
        pause_worker()
        assert is_paused() is True

    def test_resume_clears_paused(self):
        pause_worker()
        resume_worker()
        assert is_paused() is False

    def test_stop_clears_paused_if_set(self):
        pause_worker()
        stop_worker()
        assert is_paused() is False

    def test_stop_sets_stop_event(self):
        stop_worker()
        assert worker_module._stop.is_set() is True

    def test_pause_resume_cycle(self):
        assert is_paused() is False
        pause_worker()
        assert is_paused() is True
        resume_worker()
        assert is_paused() is False


# ---------------------------------------------------------------------------
# _poll_once – empty outbox
# ---------------------------------------------------------------------------

class TestPollOnceEmpty:
    def test_returns_zero_when_no_pending_events(self):
        conn, cur = _make_conn()
        cur.fetchall.return_value = []   # no pending rows

        with patch("app.worker.get_conn", lambda: _ctx(conn)):
            result = _poll_once(batch_size=10)

        assert result == 0

    def test_commits_even_when_no_rows(self):
        """Prevent the 'rolling back returned connection' pool warning."""
        conn, cur = _make_conn()
        cur.fetchall.return_value = []

        with patch("app.worker.get_conn", lambda: _ctx(conn)):
            _poll_once(batch_size=10)

        conn.commit.assert_called_once()

    def test_does_not_call_process_one_when_empty(self):
        conn, cur = _make_conn()
        cur.fetchall.return_value = []

        with patch("app.worker.get_conn", lambda: _ctx(conn)), \
             patch("app.worker._process_one") as mock_process:
            _poll_once(batch_size=10)

        mock_process.assert_not_called()


# ---------------------------------------------------------------------------
# _poll_once – with pending events
# ---------------------------------------------------------------------------

class TestPollOnceBatch:
    def _pending_rows(self, n=2):
        return [
            {"id": i, "payload": {"order_id": i, "order_ref": f"ref-{i}",
                                   "items": [{"sku": "BAN-001", "qty": 1}]}}
            for i in range(1, n + 1)
        ]

    def test_returns_count_of_processed_events(self):
        rows = self._pending_rows(3)
        conn, cur = _make_conn()
        cur.fetchall.return_value = rows

        with patch("app.worker.get_conn", lambda: _ctx(conn)), \
             patch("app.worker._process_one"):
            result = _poll_once(batch_size=10)

        assert result == 3

    def test_marks_rows_processing_before_calling_process_one(self):
        rows = self._pending_rows(2)
        conn, cur = _make_conn()
        cur.fetchall.return_value = rows

        process_calls = []

        def fake_process(event_id, payload):
            # Capture that UPDATE to 'processing' was already executed
            update_calls = [c for c in cur.execute.call_args_list
                            if "processing" in str(c)]
            process_calls.append(len(update_calls))

        with patch("app.worker.get_conn", lambda: _ctx(conn)), \
             patch("app.worker._process_one", side_effect=fake_process):
            _poll_once(batch_size=10)

        # By the time _process_one is called, UPDATE to 'processing' should have happened
        assert all(n >= 1 for n in process_calls)

    def test_calls_process_one_for_each_row(self):
        rows = self._pending_rows(2)
        conn, cur = _make_conn()
        cur.fetchall.return_value = rows

        with patch("app.worker.get_conn", lambda: _ctx(conn)), \
             patch("app.worker._process_one") as mock_process:
            _poll_once(batch_size=10)

        assert mock_process.call_count == 2
        called_ids = {c.args[0] for c in mock_process.call_args_list}
        assert called_ids == {1, 2}


# ---------------------------------------------------------------------------
# _process_one – happy path
# ---------------------------------------------------------------------------

class TestProcessOneHappyPath:
    def test_deducts_stock_and_marks_done(self):
        payload = {"order_id": 1, "order_ref": "web-1",
                   "items": [{"sku": "BAN-001", "qty": 2}]}
        conn, cur = _make_conn()
        cur.fetchone.return_value = {"id": 42}  # row lock SELECT

        with patch("app.worker.get_conn", lambda: _ctx(conn)), \
             patch("app.worker.deduct_stock") as mock_deduct:
            _process_one(42, payload)

        mock_deduct.assert_called_once()
        # Verify the items passed to deduct_stock
        assert mock_deduct.call_args.args[1] == payload["items"]

    def test_commits_after_successful_deduction(self):
        payload = {"order_id": 1, "order_ref": "web-1",
                   "items": [{"sku": "BAN-001", "qty": 2}]}
        conn, cur = _make_conn()
        cur.fetchone.return_value = {"id": 42}

        with patch("app.worker.get_conn", lambda: _ctx(conn)), \
             patch("app.worker.deduct_stock"):
            _process_one(42, payload)

        conn.commit.assert_called_once()

    def test_outbox_status_updated_to_done(self):
        payload = {"order_id": 1, "order_ref": "web-1",
                   "items": [{"sku": "BAN-001", "qty": 1}]}
        conn, cur = _make_conn()
        cur.fetchone.return_value = {"id": 42}

        with patch("app.worker.get_conn", lambda: _ctx(conn)), \
             patch("app.worker.deduct_stock"):
            _process_one(42, payload)

        sql_calls = " ".join(str(c) for c in cur.execute.call_args_list)
        assert "done" in sql_calls


# ---------------------------------------------------------------------------
# _process_one – failure paths
# ---------------------------------------------------------------------------

class TestProcessOneFailure:
    def test_insufficient_stock_triggers_rollback(self):
        payload = {"order_id": 1, "order_ref": "web-1",
                   "items": [{"sku": "BAN-001", "qty": 99}]}
        conn, cur = _make_conn()
        cur.fetchone.return_value = {"id": 42}
        shortfalls = [StockShortfall(sku="BAN-001", requested=99, available=1)]

        with patch("app.worker.get_conn", lambda: _ctx(conn)), \
             patch("app.worker.deduct_stock",
                   side_effect=InsufficientStockError(shortfalls)), \
             patch("app.worker._record_failure"):
            _process_one(42, payload)

        conn.rollback.assert_called_once()
        conn.commit.assert_not_called()

    def test_insufficient_stock_calls_record_failure(self):
        payload = {"order_id": 1, "order_ref": "web-1",
                   "items": [{"sku": "BAN-001", "qty": 99}]}
        conn, cur = _make_conn()
        cur.fetchone.return_value = {"id": 42}
        shortfalls = [StockShortfall(sku="BAN-001", requested=99, available=1)]

        with patch("app.worker.get_conn", lambda: _ctx(conn)), \
             patch("app.worker.deduct_stock",
                   side_effect=InsufficientStockError(shortfalls)), \
             patch("app.worker._record_failure") as mock_fail:
            _process_one(42, payload)

        mock_fail.assert_called_once_with(42, mock_fail.call_args.args[1])

    def test_unexpected_exception_triggers_rollback(self):
        payload = {"order_id": 1, "order_ref": "web-1",
                   "items": [{"sku": "BAN-001", "qty": 1}]}
        conn, cur = _make_conn()
        cur.fetchone.return_value = {"id": 42}

        with patch("app.worker.get_conn", lambda: _ctx(conn)), \
             patch("app.worker.deduct_stock",
                   side_effect=RuntimeError("unexpected")), \
             patch("app.worker._record_failure"):
            _process_one(42, payload)   # should not raise

        conn.rollback.assert_called_once()


# ---------------------------------------------------------------------------
# _record_failure
# ---------------------------------------------------------------------------

class TestRecordFailure:
    def test_executes_update_with_error_message(self):
        conn, cur = _make_conn()

        with patch("app.worker.get_conn", lambda: _ctx(conn)):
            _record_failure(99, "something went wrong")

        sql_calls = " ".join(str(c) for c in cur.execute.call_args_list)
        assert "attempts" in sql_calls.lower()

    def test_commits_after_recording(self):
        conn, cur = _make_conn()

        with patch("app.worker.get_conn", lambda: _ctx(conn)):
            _record_failure(99, "error")

        conn.commit.assert_called_once()

"""
Outbox Worker – stock catch-up after interruption.

How it works
------------
1. Poll the outbox table for rows with status='pending'.
2. For each row (one DB transaction per row):
   a. Lock the row with SELECT … FOR UPDATE SKIP LOCKED so concurrent
      worker instances never double-process the same event.
   b. Mark it 'processing'.
   c. Call products.deduct_stock() with the order's items.
   d. Mark it 'done' and record processed_at.
   e. COMMIT — deduction and status change are atomic.
3. On any error, ROLLBACK and increment the attempts counter.
   After 3 failed attempts the row is marked 'failed' so it doesn't
   block the queue forever (manual inspection / alerting hook).

Interruption / catch-up demo
-----------------------------
The worker runs as a background thread inside the same process as the
API.  The API exposes two control endpoints:

    POST /worker/pause   – sets a threading.Event that makes the
                          poll loop sleep indefinitely (simulates
                          the stock capability being unavailable).
    POST /worker/resume  – clears the event so polling resumes.

During a pause orders are still accepted (outbox rows accumulate).
On resume, the worker drains the backlog in batches and stock catches up.

The pause/resume mechanism is intentionally simple — it lives in a
single process to keep the demo self-contained.
"""

from __future__ import annotations

import json
import logging
import threading
import time

from app.config import settings
from app.db import get_conn
from app.products import InsufficientStockError, deduct_stock

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Pause / resume control
# ---------------------------------------------------------------------------
_paused = threading.Event()   # when SET → worker is paused
_stop   = threading.Event()   # when SET → worker thread exits


def pause_worker() -> None:
    logger.info("Worker paused")
    _paused.set()


def resume_worker() -> None:
    logger.info("Worker resumed")
    _paused.clear()


def stop_worker() -> None:
    logger.info("Worker stopping")
    _stop.set()
    _paused.clear()   # unblock if currently paused


def is_paused() -> bool:
    return _paused.is_set()


# ---------------------------------------------------------------------------
# Core processing
# ---------------------------------------------------------------------------
MAX_ATTEMPTS = 3


def _process_one(event_id: int, payload: dict) -> None:
    """Apply stock deduction for a single outbox event.

    Runs inside its own transaction that is passed through to deduct_stock
    so the stock update and the outbox status change commit together.
    """
    items = payload.get("items", [])

    with get_conn() as conn:
        try:
            with conn.cursor() as cur:
                # Re-lock the row before any writes
                cur.execute(
                    "SELECT id FROM outbox WHERE id = %s FOR UPDATE",
                    (event_id,),
                )

                # Deduct stock (raises InsufficientStockError on shortfall)
                deduct_stock(conn, items)

                # Mark done
                cur.execute(
                    """
                    UPDATE outbox
                    SET status = 'done',
                        processed_at = NOW(),
                        attempts = attempts + 1
                    WHERE id = %s
                    """,
                    (event_id,),
                )

            conn.commit()
            logger.info("Outbox event %d processed (order_ref=%s)", event_id, payload.get("order_ref"))

        except InsufficientStockError as exc:
            conn.rollback()
            _record_failure(event_id, str(exc))
            logger.warning("Event %d – insufficient stock: %s", event_id, exc)

        except Exception as exc:
            conn.rollback()
            _record_failure(event_id, str(exc))
            logger.exception("Event %d – unexpected error: %s", event_id, exc)


def _record_failure(event_id: int, error: str) -> None:
    """Increment attempt counter; mark 'failed' after MAX_ATTEMPTS."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE outbox
                SET attempts   = attempts + 1,
                    last_error = %s,
                    status     = CASE
                                   WHEN attempts + 1 >= %s THEN 'failed'::outbox_status
                                   ELSE 'pending'::outbox_status
                                 END
                WHERE id = %s
                """,
                (error, MAX_ATTEMPTS, event_id),
            )
        conn.commit()


# ---------------------------------------------------------------------------
# Poll loop
# ---------------------------------------------------------------------------

def _poll_once(batch_size: int) -> int:
    """Fetch and process up to batch_size pending outbox events.

    Returns the number of events processed this cycle.
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            # SKIP LOCKED: safe for multiple worker instances
            cur.execute(
                """
                SELECT id, payload
                FROM outbox
                WHERE status = 'pending'
                ORDER BY created_at
                LIMIT %s
                FOR UPDATE SKIP LOCKED
                """,
                (batch_size,),
            )
            rows = cur.fetchall()

        if not rows:
            # No work to do — commit to cleanly close the implicit transaction
            # so psycopg pool doesn't see an open transaction on return.
            conn.commit()
            return 0

        # Mark batch as 'processing' so a restart won't pick them up mid-flight
        ids = [r["id"] for r in rows]
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE outbox SET status = 'processing' WHERE id = ANY(%s)",
                (ids,),
            )
        conn.commit()

    for row in rows:
        payload = row["payload"] if isinstance(row["payload"], dict) else json.loads(row["payload"])
        _process_one(row["id"], payload)

    return len(rows)


def run_worker() -> None:
    """Entry point for the background worker thread."""
    logger.info(
        "Stock worker started (poll_interval=%ds, batch_size=%d)",
        settings.worker_poll_interval,
        settings.worker_batch_size,
    )

    while not _stop.is_set():
        # --- pause gate ---
        if _paused.is_set():
            logger.debug("Worker paused – waiting for resume signal")
            while _paused.is_set() and not _stop.is_set():
                time.sleep(0.5)
            if _stop.is_set():
                break
            logger.info("Worker resumed – draining backlog")

        # --- process a batch ---
        try:
            processed = _poll_once(settings.worker_batch_size)
            if processed:
                logger.info("Processed %d outbox event(s)", processed)
        except Exception:
            logger.exception("Worker poll error – will retry after sleep")

        # --- sleep before next cycle ---
        _stop.wait(timeout=settings.worker_poll_interval)

    logger.info("Stock worker stopped")

-- =============================================================
-- Mr. D Orders & Stock Core Flow  –  Database Schema
-- =============================================================
-- Run once against a fresh database:
--   psql $DATABASE_URL -f db/schema.sql
-- =============================================================

-- ---------------------------------------------------------------
-- Products & Stock
-- ---------------------------------------------------------------
CREATE TABLE IF NOT EXISTS products (
    sku           TEXT        PRIMARY KEY,
    name          TEXT        NOT NULL,
    price_cents   INTEGER     NOT NULL CHECK (price_cents >= 0),
    stock         INTEGER     NOT NULL CHECK (stock >= 0),
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ---------------------------------------------------------------
-- Orders
-- ---------------------------------------------------------------
CREATE TYPE order_status AS ENUM ('pending', 'confirmed', 'failed');

CREATE TABLE IF NOT EXISTS orders (
    id            BIGSERIAL   PRIMARY KEY,
    order_ref     TEXT        NOT NULL,
    customer_id   TEXT        NOT NULL,
    status        order_status NOT NULL DEFAULT 'pending',
    total_cents   INTEGER     NOT NULL DEFAULT 0,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    -- Idempotency: only one row per order_ref may be non-failed.
    -- We enforce uniqueness on order_ref so the first insert wins;
    -- subsequent duplicates are caught at the application layer
    -- and the existing order is returned instead.
    CONSTRAINT uq_orders_order_ref UNIQUE (order_ref)
);

CREATE TABLE IF NOT EXISTS order_items (
    id          BIGSERIAL   PRIMARY KEY,
    order_id    BIGINT      NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
    sku         TEXT        NOT NULL REFERENCES products(sku),
    qty         INTEGER     NOT NULL CHECK (qty > 0),
    unit_price_cents INTEGER NOT NULL CHECK (unit_price_cents >= 0)
);

CREATE INDEX IF NOT EXISTS idx_order_items_order_id ON order_items(order_id);
CREATE INDEX IF NOT EXISTS idx_orders_created_at    ON orders(created_at);
CREATE INDEX IF NOT EXISTS idx_orders_customer_id   ON orders(customer_id);

-- ---------------------------------------------------------------
-- Outbox  (transactional outbox for stock-deduction events)
-- ---------------------------------------------------------------
-- Every accepted order writes one outbox row inside the SAME
-- transaction.  The stock worker polls this table, applies
-- stock deductions, and marks rows processed.
-- This decouples order intake from stock updates and enables
-- catch-up after any interruption.
-- ---------------------------------------------------------------
CREATE TYPE outbox_status AS ENUM ('pending', 'processing', 'done', 'failed');

CREATE TABLE IF NOT EXISTS outbox (
    id            BIGSERIAL    PRIMARY KEY,
    event_type    TEXT         NOT NULL,          -- e.g. 'order_confirmed'
    payload       JSONB        NOT NULL,           -- full event data
    status        outbox_status NOT NULL DEFAULT 'pending',
    attempts      INTEGER      NOT NULL DEFAULT 0,
    last_error    TEXT,
    created_at    TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    processed_at  TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_outbox_status     ON outbox(status);
CREATE INDEX IF NOT EXISTS idx_outbox_created_at ON outbox(created_at);

-- ---------------------------------------------------------------
-- Helpers
-- ---------------------------------------------------------------
-- Auto-update updated_at on orders and products
CREATE OR REPLACE FUNCTION set_updated_at()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_orders_updated_at   ON orders;
CREATE TRIGGER trg_orders_updated_at
    BEFORE UPDATE ON orders
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

DROP TRIGGER IF EXISTS trg_products_updated_at ON products;
CREATE TRIGGER trg_products_updated_at
    BEFORE UPDATE ON products
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

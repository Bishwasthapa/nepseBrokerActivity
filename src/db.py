"""Database connection helpers."""

from __future__ import annotations

import os
from contextlib import contextmanager
from datetime import date

import psycopg2
from sqlalchemy import create_engine

DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql://quant:quantpass@db:5432/nepse_analytics",
)

_SCHEMA_MIGRATED = False


def ensure_schema(conn=None) -> None:
    """Apply idempotent database migrations ensuring all required tables, columns, and indexes exist."""
    global _SCHEMA_MIGRATED
    if _SCHEMA_MIGRATED:
        return

    should_close = False
    if conn is None:
        try:
            conn = psycopg2.connect(DATABASE_URL)
            should_close = True
        except Exception:
            return

    try:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS securities_meta (
                    symbol              VARCHAR(20)    PRIMARY KEY,
                    company_name        VARCHAR(120),
                    sector              VARCHAR(50),
                    instrument_type     VARCHAR(30),
                    market_cap          NUMERIC(16, 2),
                    fifty_two_week_high NUMERIC(10, 2),
                    fifty_two_week_low  NUMERIC(10, 2),
                    updated_at          TIMESTAMP      DEFAULT CURRENT_TIMESTAMP
                );
                ALTER TABLE daily_market_summary ADD COLUMN IF NOT EXISTS sector VARCHAR(50);
                ALTER TABLE daily_market_summary ADD COLUMN IF NOT EXISTS market_cap NUMERIC(16, 2);
                ALTER TABLE daily_market_summary ADD COLUMN IF NOT EXISTS fifty_two_week_high NUMERIC(10, 2);
                ALTER TABLE daily_market_summary ADD COLUMN IF NOT EXISTS fifty_two_week_low NUMERIC(10, 2);
                ALTER TABLE daily_market_summary ADD COLUMN IF NOT EXISTS vwap NUMERIC(10, 2);
                ALTER TABLE daily_broker_rollup ADD COLUMN IF NOT EXISTS matched_qty BIGINT NOT NULL DEFAULT 0;
                CREATE INDEX IF NOT EXISTS idx_summary_sector_date ON daily_market_summary (sector, trade_date);

                UPDATE daily_market_summary s
                SET sector = COALESCE(s.sector, m.sector),
                    market_cap = COALESCE(s.market_cap, m.market_cap),
                    fifty_two_week_high = COALESCE(s.fifty_two_week_high, m.fifty_two_week_high),
                    fifty_two_week_low = COALESCE(s.fifty_two_week_low, m.fifty_two_week_low)
                FROM securities_meta m
                WHERE s.symbol = m.symbol
                  AND (s.sector IS NULL OR s.market_cap IS NULL OR s.fifty_two_week_high IS NULL);
            """)
            conn.commit()
            _SCHEMA_MIGRATED = True
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
    finally:
        if should_close:
            try:
                conn.close()
            except Exception:
                pass


def get_engine():
    return create_engine(DATABASE_URL, pool_pre_ping=True)


def get_conn():
    conn = psycopg2.connect(DATABASE_URL)
    if not _SCHEMA_MIGRATED:
        ensure_schema(conn)
    return conn


@contextmanager
def connection():
    conn = get_conn()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def latest_trade_date(conn) -> date | None:
    """Most recent trade_date present in the market summary."""
    with conn.cursor() as cur:
        cur.execute("SELECT MAX(trade_date) FROM daily_market_summary")
        row = cur.fetchone()
        return row[0] if row and row[0] else None

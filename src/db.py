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


def get_engine():
    return create_engine(DATABASE_URL, pool_pre_ping=True)


def get_conn():
    return psycopg2.connect(DATABASE_URL)


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

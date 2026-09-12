"""Persistent user research watchlist and dated journal notes."""

from __future__ import annotations

from datetime import date


def add_symbol(
    conn,
    symbol: str,
    thesis: str | None = None,
    tags: str | None = None,
    note: str | None = None,
) -> None:
    """Add or reactivate a ticker and optionally record its first journal note."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO watchlist (symbol, status, thesis, tags)
            VALUES (%s, 'WATCHING', %s, %s)
            ON CONFLICT (symbol) DO UPDATE SET
                status = 'WATCHING',
                thesis = COALESCE(EXCLUDED.thesis, watchlist.thesis),
                tags = COALESCE(EXCLUDED.tags, watchlist.tags),
                updated_at = CURRENT_TIMESTAMP
            """,
            (symbol, thesis, tags),
        )
        if note:
            cur.execute(
                "INSERT INTO watchlist_notes (symbol, note) VALUES (%s, %s)",
                (symbol, note),
            )
    conn.commit()


def add_note(conn, symbol: str, note: str, note_date: date | None = None) -> bool:
    """Append a dated observation. Return False if the ticker is not watched."""
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM watchlist WHERE symbol = %s", (symbol,))
        if cur.fetchone() is None:
            return False
        if note_date is None:
            cur.execute(
                "INSERT INTO watchlist_notes (symbol, note) VALUES (%s, %s)",
                (symbol, note),
            )
        else:
            cur.execute(
                "INSERT INTO watchlist_notes (symbol, note_date, note) VALUES (%s, %s, %s)",
                (symbol, note_date, note),
            )
        cur.execute(
            "UPDATE watchlist SET updated_at = CURRENT_TIMESTAMP WHERE symbol = %s",
            (symbol,),
        )
    conn.commit()
    return True


def archive_symbol(conn, symbol: str) -> bool:
    """Archive a ticker without deleting its research history."""
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE watchlist
            SET status = 'ARCHIVED', updated_at = CURRENT_TIMESTAMP
            WHERE symbol = %s AND status <> 'ARCHIVED'
            """,
            (symbol,),
        )
        changed = cur.rowcount > 0
    conn.commit()
    return changed


def list_symbols(conn, include_archived: bool = False) -> list[dict]:
    """Return watchlist rows enriched with current market context and last note."""
    where = "" if include_archived else "WHERE w.status <> 'ARCHIVED'"
    sql = f"""
        SELECT w.symbol, w.status, w.thesis, w.tags, w.added_at::date AS added_date,
               w.updated_at::date AS updated_date,
               m.trade_date AS market_date, m.close_price, m.price_change_pct,
               m.turnover_rank,
               n.note_date, n.note
        FROM watchlist w
        LEFT JOIN LATERAL (
            SELECT trade_date, close_price, price_change_pct, turnover_rank
            FROM daily_market_summary
            WHERE symbol = w.symbol
            ORDER BY trade_date DESC
            LIMIT 1
        ) m ON TRUE
        LEFT JOIN LATERAL (
            SELECT note_date, note
            FROM watchlist_notes
            WHERE symbol = w.symbol
            ORDER BY note_date DESC, id DESC
            LIMIT 1
        ) n ON TRUE
        {where}
        ORDER BY w.updated_at DESC, w.symbol
    """
    with conn.cursor() as cur:
        cur.execute(sql)
        columns = [d[0] for d in cur.description]
        return [dict(zip(columns, row)) for row in cur.fetchall()]


def history(conn, symbol: str, limit: int = 100) -> tuple[dict | None, list[dict]]:
    """Return a watch item's metadata and its dated notes, newest first."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT symbol, status, thesis, tags, added_at::date AS added_date,
                   updated_at::date AS updated_date
            FROM watchlist WHERE symbol = %s
            """,
            (symbol,),
        )
        row = cur.fetchone()
        if row is None:
            return None, []
        metadata = dict(zip([d[0] for d in cur.description], row))
        cur.execute(
            """
            SELECT note_date, note, created_at
            FROM watchlist_notes
            WHERE symbol = %s
            ORDER BY note_date DESC, id DESC
            LIMIT %s
            """,
            (symbol, limit),
        )
        columns = [d[0] for d in cur.description]
        notes = [dict(zip(columns, row)) for row in cur.fetchall()]
    return metadata, notes

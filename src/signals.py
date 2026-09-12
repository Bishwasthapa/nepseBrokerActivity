"""Persist screener signals to PostgreSQL and support history + streak queries.

Streak semantics: a *current streak* for a given ``(symbol, broker_id, track,
signal)`` counts how many consecutive trading sessions — walking backward from
the most recent session in the history table — that exact signal was persisted
for that pair. Because only actionable signals are persisted (``WATCH`` and
unclassified rows are skipped), any session without the signal breaks the run.
"""

from __future__ import annotations

from datetime import date

import psycopg2.extras

# Signal names that are meaningful enough to persist. ``WATCH`` is an
# unclassified fallback and would pollute streak detection, so it is dropped.
ACTIONABLE = {
    "SILENT_ACCUMULATION",
    "ACTIVE_MARKUP",
    "DISTRIBUTION_TRAP",
    "STEALTH_ACCUMULATION",
}


def build_signal_rows(
    track_a: list[dict], track_b: list[dict], trade_date: date
) -> list[tuple]:
    """Map Track A/B results into history-table tuples (actionable only)."""
    rows: list[tuple] = []
    for r in track_a:
        sig = r.get("signal")
        if sig not in ACTIONABLE:
            continue
        rows.append(
            (
                trade_date,
                r["symbol"],
                int(r.get("turnover_rank") or 0),
                int(r["broker_id"]),
                r.get("net_t1"),
                r.get("net_t5"),
                r.get("net_t22"),
                r.get("net_t66"),
                r.get("margin_pct"),
                r.get("t1_change_pct"),
                sig,
                "TRACK_A",
            )
        )
    for r in track_b:
        sig = r.get("signal")
        if sig not in ACTIONABLE:
            continue
        # Track B is market-wide / unranked; its window change lives in
        # ``t22_price_change_pct``, stored in the generic ``t1_change_pct``.
        rows.append(
            (
                trade_date,
                r["symbol"],
                0,  # turnover_rank NOT NULL; unranked for Track B
                int(r["broker_id"]),
                None,  # net_1d
                None,  # net_5d
                r.get("net_t22"),
                None,  # net_66d
                r.get("margin_pct"),
                r.get("t22_price_change_pct"),
                sig,
                "TRACK_B",
            )
        )
    return rows


def persist_signals(
    conn, track_a: list[dict], track_b: list[dict], trade_date: date
) -> int:
    """Upsert one session's actionable signals into the history table."""
    rows = build_signal_rows(track_a, track_b, trade_date)
    if not rows:
        return 0
    sql = """
        INSERT INTO screener_signals_history
            (trade_date, symbol, turnover_rank, broker_id, net_1d, net_5d,
             net_22d, net_66d, margin_pct, t1_change_pct, signal, track)
        VALUES %s
        ON CONFLICT (trade_date, symbol, broker_id, track) DO UPDATE SET
            turnover_rank  = EXCLUDED.turnover_rank,
            net_1d         = EXCLUDED.net_1d,
            net_5d         = EXCLUDED.net_5d,
            net_22d        = EXCLUDED.net_22d,
            net_66d        = EXCLUDED.net_66d,
            margin_pct     = EXCLUDED.margin_pct,
            t1_change_pct  = EXCLUDED.t1_change_pct,
            signal         = EXCLUDED.signal,
            created_at     = CURRENT_TIMESTAMP
    """
    with conn.cursor() as cur:
        psycopg2.extras.execute_values(cur, sql, rows, page_size=1000)
    return len(rows)


def load_signal_history(
    conn,
    symbol: str | None = None,
    broker_id: int | None = None,
    signal: str | None = None,
    track: str | None = None,
    limit: int = 100,
) -> list[dict]:
    """Query signal history with optional filters, newest-first."""
    where: list[str] = []
    params: list = []
    if symbol:
        where.append("symbol = %s")
        params.append(symbol)
    if broker_id is not None:
        where.append("broker_id = %s")
        params.append(int(broker_id))
    if signal:
        where.append("signal = %s")
        params.append(signal)
    if track:
        where.append("track = %s")
        params.append(track.upper())

    sql = (
        "SELECT trade_date, symbol, broker_id, track, signal, turnover_rank, "
        "net_1d, net_5d, net_22d, net_66d, margin_pct, t1_change_pct "
        "FROM screener_signals_history"
    )
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY trade_date DESC, turnover_rank ASC LIMIT %s"
    params.append(int(limit))

    with conn.cursor() as cur:
        cur.execute(sql, params)
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


def current_streaks(conn, min_streak: int = 1) -> list[dict]:
    """Current consecutive-session streak per (symbol, broker_id, signal).

    The streak timeline is the real set of trading sessions from
    ``daily_market_summary``: a weekend/holiday gap does not break a streak, but
    any actual session that lacks the signal does.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT DISTINCT trade_date FROM daily_market_summary ORDER BY trade_date"
        )
        all_dates = [r[0] for r in cur.fetchall()]
        cur.execute(
            "SELECT trade_date, symbol, broker_id, track, signal "
            "FROM screener_signals_history ORDER BY trade_date"
        )
        rows = cur.fetchall()
    if not all_dates or not rows:
        return []

    date_index = {d: i for i, d in enumerate(all_dates)}
    by_signal: dict[tuple, set[date]] = {}
    for d, s, b, tr, sig in rows:
        by_signal.setdefault((s, b, tr, sig), set()).add(d)

    out: list[dict] = []
    for (sym, bid, track, sig), dates in by_signal.items():
        last = max(dates)
        if last not in date_index:
            continue
        i = date_index[last]
        n = 0
        while i >= 0 and all_dates[i] in dates:
            n += 1
            i -= 1
        if n >= min_streak:
            out.append(
                {
                    "symbol": sym,
                    "broker_id": bid,
                    "track": track,
                    "signal": sig,
                    "streak": n,
                    "last_date": last,
                }
            )
    out.sort(key=lambda r: (-r["streak"], r["last_date"], r["symbol"]))
    return out
    return rows
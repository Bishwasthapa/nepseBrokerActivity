"""Dual-track NEPSE screener: Track A momentum/traps, Track B stealth radar."""

from __future__ import annotations

from datetime import date

import polars as pl

from src.db import get_conn
from src.scraper import get_top_brokers_today
from src.ai_insight import generate_broker_alert
from src.notifier import send_alert

WINDOWS = {"T_1": 1, "T_5": 5, "T_22": 22, "T_66": 66}

# Symbols to exclude from screening:
# - Promoter stocks that END in 'P', except HIDCLP and HEIP are kept.
# - Debentures, which carry a digit in their ticker (e.g. H8020, PRVU1).
# Add any specific symbols to EXCLUDED_SYMBOLS for manual exclusions.
EXCLUDED_SYMBOLS: list[str] = ["RSY"]
PROMOTER_ALLOWLIST = {"HIDCLP", "HEIP"}


def _is_excluded(symbol: str) -> bool:
    """Return True when a symbol should be filtered out of screening."""
    if symbol in EXCLUDED_SYMBOLS:
        return True
    if symbol in PROMOTER_ALLOWLIST:
        return False
    # Promoter stocks: ticker ends with 'P' (e.g. LECP, NABILP).
    if symbol.endswith("P"):
        return True
    # Debentures: tickers embed a digit (e.g. H8020).
    if any(c.isdigit() for c in symbol):
        return True
    return False


def fetch_trade_dates(conn) -> list[date]:
    with conn.cursor() as cur:
        cur.execute("SELECT DISTINCT trade_date FROM daily_market_summary ORDER BY trade_date")
        return [r[0] for r in cur.fetchall()]


def window_dates(all_dates: list[date]) -> dict[str, list[date]]:
    """Map T_N -> last N trading sessions (not calendar days)."""
    if not all_dates:
        return {k: [] for k in WINDOWS}
    ordered = sorted(all_dates)
    out = {}
    for name, n in WINDOWS.items():
        out[name] = ordered[-n:] if len(ordered) >= n else ordered[:]
    return out


def _load_frame(conn, sql: str, params=None) -> pl.DataFrame:
    with conn.cursor() as cur:
        cur.execute(sql, params)
        cols = [d[0] for d in cur.description]
        rows = cur.fetchall()
    if not rows:
        return pl.DataFrame(schema={c: pl.Utf8 for c in cols})
    return pl.DataFrame(rows, schema=cols, orient="row")


def load_rollup(conn, dates: list[date]) -> pl.DataFrame:
    return _load_frame(
        conn,
        """
        SELECT trade_date, symbol, broker_id,
               buy_qty, buy_amount, sell_qty, sell_amount, self_trade_qty,
               matched_qty
        FROM daily_broker_rollup
        WHERE trade_date = ANY(%s)
        """,
        (dates,),
    )


def classify_cap_tier(market_cap: float | None) -> str:
    """Classify market cap into LARGE, MID, or SMALL tier.

    market_cap is in million NPR (e.g. 145000 = 145B NPR, 3304 = 3.3B NPR).
    - LARGE: >= 20,000M (>= 20 Billion NPR)
    - MID: 5,000M to 20,000M (5B - 20B NPR)
    - SMALL: < 5,000M (< 5 Billion NPR)
    """
    if market_cap is None:
        return "UNKNOWN"
    try:
        val = float(market_cap)
    except (ValueError, TypeError):
        return "UNKNOWN"
    if val >= 20000.0:
        return "LARGE"
    if val >= 5000.0:
        return "MID"
    return "SMALL"


def load_summary(conn, dates: list[date]) -> pl.DataFrame:
    try:
        df = _load_frame(
            conn,
            """
            SELECT s.trade_date, s.symbol, s.close_price, s.price_change_pct,
                   s.total_qty, s.total_turnover, s.turnover_rank,
                   COALESCE(s.sector, m.sector) AS sector,
                   COALESCE(s.market_cap, m.market_cap) AS market_cap,
                   COALESCE(s.fifty_two_week_high, m.fifty_two_week_high) AS fifty_two_week_high,
                   COALESCE(s.fifty_two_week_low, m.fifty_two_week_low) AS fifty_two_week_low,
                   s.vwap
            FROM daily_market_summary s
            LEFT JOIN securities_meta m ON s.symbol = m.symbol
            WHERE s.trade_date = ANY(%s)
            """,
            (dates,),
        )
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        try:
            df = _load_frame(
                conn,
                """
                SELECT trade_date, symbol, close_price, price_change_pct,
                       total_qty, total_turnover, turnover_rank,
                       sector, market_cap, fifty_two_week_high, fifty_two_week_low, vwap
                FROM daily_market_summary
                WHERE trade_date = ANY(%s)
                """,
                (dates,),
            )
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass
            df = _load_frame(
                conn,
                """
                SELECT trade_date, symbol, close_price, price_change_pct,
                       total_qty, total_turnover, turnover_rank
                FROM daily_market_summary
                WHERE trade_date = ANY(%s)
                """,
                (dates,),
            )
    expected = {
        "sector": pl.Utf8,
        "market_cap": pl.Float64,
        "fifty_two_week_high": pl.Float64,
        "fifty_two_week_low": pl.Float64,
        "vwap": pl.Float64,
    }
    for col, dtype in expected.items():
        if col not in df.columns:
            df = df.with_columns(pl.lit(None, dtype=dtype).alias(col))
    return df


def _ensure_matched(rollup: pl.DataFrame) -> pl.DataFrame:
    """Guarantee a ``matched_qty`` column exists (zero-filled if absent)."""
    if "matched_qty" not in rollup.columns:
        rollup = rollup.with_columns(pl.lit(0, dtype=pl.Int64).alias("matched_qty"))
    return rollup


def aggregate_window(rollup: pl.DataFrame, dates: list[date]) -> pl.DataFrame:
    """Net metrics + VWAP per (symbol, broker) over a session window."""
    if rollup.is_empty() or not dates:
        return pl.DataFrame()
    rollup = _ensure_matched(rollup)
    w = rollup.filter(pl.col("trade_date").is_in(dates))
    return w.group_by(["symbol", "broker_id"]).agg(
        buy_qty=pl.col("buy_qty").sum(),
        buy_amount=pl.col("buy_amount").sum(),
        sell_qty=pl.col("sell_qty").sum(),
        sell_amount=pl.col("sell_amount").sum(),
        self_trade_qty=pl.col("self_trade_qty").sum(),
        matched_qty=pl.col("matched_qty").sum(),
        net_qty=(pl.col("buy_qty") - pl.col("sell_qty")).sum(),
        buy_vwap=pl.when(pl.col("buy_qty").sum() == 0)
        .then(None)
        .otherwise(pl.col("buy_amount").sum() / pl.col("buy_qty").sum().cast(pl.Float64)),
        sell_vwap=pl.when(pl.col("sell_qty").sum() == 0)
        .then(None)
        .otherwise(pl.col("sell_amount").sum() / pl.col("sell_qty").sum().cast(pl.Float64)),
    )


def compute_broker_match_pct(
    rollup: pl.DataFrame, dates: list[date], per_symbol: bool = False
) -> pl.DataFrame:
    """Broker-level internal-matching metric over a window.

    For each broker B (or each symbol,broker pair when ``per_symbol=True``):
      buy_qty   = sum(buy_qty  where buyer = B)
      sell_qty  = sum(sell_qty where seller = B)
      matched   = sum(matched_qty where buyer = seller = B)
      gross     = buy_qty + sell_qty
      match_pct = 2 * matched / gross * 100  (0.0 when gross == 0)
    """
    if rollup.is_empty() or not dates:
        return pl.DataFrame()
    rollup = _ensure_matched(rollup)
    w = rollup.filter(pl.col("trade_date").is_in(dates))
    keys = ["symbol", "broker_id"] if per_symbol else ["broker_id"]
    return (
        w.group_by(keys)
        .agg(
            buy_qty=pl.col("buy_qty").sum(),
            sell_qty=pl.col("sell_qty").sum(),
            matched_qty=pl.col("matched_qty").sum(),
        )
        .with_columns(
            gross_volume=pl.col("buy_qty") + pl.col("sell_qty"),
        )
        .with_columns(
            match_pct=pl.when(pl.col("gross_volume") > 0)
            .then(
                100.0
                * (2 * pl.col("matched_qty"))
                / pl.col("gross_volume")
            )
            .otherwise(0.0)
        )
    )


def compute_session_match_pct(
    rollup: pl.DataFrame, summary: pl.DataFrame, dates: list[date]
) -> pl.DataFrame:
    """Session-level cross-trade metric per (trade_date, symbol).

      crossed_qty   = sum(matched_qty) over brokers for that session/symbol
      total_qty     = daily_market_summary.total_qty
      session_match = crossed_qty / total_qty * 100  (0.0 when total_qty == 0)
    """
    if rollup.is_empty() or summary.is_empty() or not dates:
        return pl.DataFrame()
    rollup = _ensure_matched(rollup)
    w = rollup.filter(pl.col("trade_date").is_in(dates))
    crossed = w.group_by(["trade_date", "symbol"]).agg(
        crossed_qty=pl.col("matched_qty").sum()
    )
    sm = (
        summary.filter(pl.col("trade_date").is_in(dates))
        .select(["trade_date", "symbol", "total_qty"])
        .with_columns(pl.col("total_qty").cast(pl.Int64))
    )
    per_session = crossed.join(sm, on=["trade_date", "symbol"], how="left").with_columns(
        total_qty=pl.col("total_qty").fill_null(0),
        session_match_pct=pl.when(
            pl.col("total_qty").fill_null(0) > 0
        )
        .then(100.0 * pl.col("crossed_qty") / pl.col("total_qty").fill_null(0))
        .otherwise(0.0),
    )
    # Collapse to one row per symbol: keep the session with the highest match %.
    return (
        per_session
        .sort("session_match_pct", descending=True)
        .unique(subset=["symbol"], keep="first")
        .drop("trade_date")
    )


def stock_window_totals(summary: pl.DataFrame, dates: list[date]) -> pl.DataFrame:
    if summary.is_empty() or not dates:
        return pl.DataFrame()
    w = summary.filter(pl.col("trade_date").is_in(dates))
    return w.group_by("symbol").agg(
        total_qty=pl.col("total_qty").sum(),
        total_turnover=pl.col("total_turnover").sum(),
        avg_daily_turnover=pl.col("total_turnover").mean(),
        close_first=pl.col("close_price").sort_by("trade_date").first(),
        close_last=pl.col("close_price").sort_by("trade_date").last(),
        n_sessions=pl.col("trade_date").n_unique(),
    ).with_columns(
        price_change_pct=pl.when(
            pl.col("close_first").is_null() | (pl.col("close_first") == 0)
        )
        .then(0.0)
        .otherwise(
            (pl.col("close_last") - pl.col("close_first")) / pl.col("close_first") * 100.0
        )
    )


def _dominant_brokers(rollup: pl.DataFrame, dates: list[date]) -> dict[str, tuple[int | None, int | None]]:
    """Map symbol -> (top net buyer broker_id, top net seller broker_id) over a window."""
    if rollup is None or rollup.is_empty() or not dates:
        return {}
    agg = aggregate_window(rollup, dates)
    if agg.is_empty():
        return {}
    out: dict[str, tuple[int | None, int | None]] = {}
    for sym in agg["symbol"].unique().to_list():
        sub = agg.filter(pl.col("symbol") == sym)
        buyer = sub.sort("net_qty", descending=True).head(1)
        seller = sub.sort("net_qty", descending=False).head(1)
        out[sym] = (
            int(buyer["broker_id"][0]) if not buyer.is_empty() else None,
            int(seller["broker_id"][0]) if not seller.is_empty() else None,
        )
    return out


def _compute_all_turnover_momentum(
    summary_df: pl.DataFrame,
    short_window: int = 5,
    base_window: int = 22,
    rollup: pl.DataFrame | None = None,
) -> tuple[pl.DataFrame, dict[str, tuple[int | None, int | None]]]:
    """Compute multi-window turnover and rank drift statistics across all symbols."""
    if summary_df.is_empty():
        return pl.DataFrame(), {}
    dates = sorted(summary_df["trade_date"].unique().to_list())
    if not dates:
        return pl.DataFrame(), {}

    short_dates = dates[-short_window:]
    base_dates = dates[-base_window:]

    def _window_agg(ds: list[date]) -> pl.DataFrame:
        sub = summary_df.filter(pl.col("trade_date").is_in(ds))
        return sub.group_by("symbol").agg(
            avg_turnover=pl.col("total_turnover").mean(),
            avg_rank=pl.col("turnover_rank").mean(),
            close_first=pl.col("close_price").sort_by("trade_date").first(),
            close_last=pl.col("close_price").sort_by("trade_date").last(),
        )

    s = _window_agg(short_dates).rename(
        {"avg_turnover": "avg_turnover_short", "avg_rank": "avg_rank_short"}
    )
    b = _window_agg(base_dates).rename(
        {"avg_turnover": "avg_turnover_base", "avg_rank": "avg_rank_base"}
    )
    joined = s.join(b, on="symbol", how="inner").with_columns(
        rank_drift=pl.col("avg_rank_base") - pl.col("avg_rank_short"),
        turnover_ratio=pl.when(pl.col("avg_turnover_base") > 0)
        .then(pl.col("avg_turnover_short") / pl.col("avg_turnover_base"))
        .otherwise(None),
        price_change_pct_window=pl.when(
            pl.col("close_first").is_null() | (pl.col("close_first") == 0)
        )
        .then(0.0)
        .otherwise(
            (pl.col("close_last") - pl.col("close_first")) / pl.col("close_first") * 100.0
        ),
    ).with_columns(close=pl.col("close_last"))

    dominators = _dominant_brokers(rollup, short_dates) if rollup is not None else {}
    return joined, dominators


def classify_momentum_status(
    avg_rank_short: float,
    avg_rank_base: float,
    rank_drift: float,
    turnover_ratio: float | None,
) -> str:
    """Classify a symbol's liquidity/momentum state into a standardized badge."""
    if turnover_ratio is None:
        return "INSUFFICIENT_DATA"
    if avg_rank_short <= 50 and rank_drift >= 15 and turnover_ratio >= 1.75:
        return "MOMENTUM_GAINER"
    if avg_rank_base <= 40 and rank_drift <= -15 and turnover_ratio <= 0.50:
        return "MOMENTUM_LOSER"
    if turnover_ratio >= 1.25 and rank_drift >= 5:
        return "STEALTH_BUILDING"
    if turnover_ratio <= 0.75 and rank_drift <= -5:
        return "LIQUIDITY_FADING"
    if avg_rank_short <= 30 and abs(rank_drift) < 10:
        return "HIGH_VOLUME_STABLE"
    return "NEUTRAL"


def symbol_turnover_momentum(
    summary_df: pl.DataFrame,
    symbol: str,
    short_window: int = 5,
    base_window: int = 22,
    rollup: pl.DataFrame | None = None,
) -> dict | None:
    """Compute comprehensive momentum diagnostic for a single stock."""
    if summary_df.is_empty():
        return None
    joined, dominators = _compute_all_turnover_momentum(
        summary_df, short_window, base_window, rollup
    )
    if joined.is_empty():
        return None
    target = joined.filter(pl.col("symbol") == symbol.upper().strip())
    if target.is_empty():
        return None
    r = target.row(0, named=True)
    acc, dist = dominators.get(r["symbol"], (None, None))

    total_symbols = len(joined)
    ratio_val = r["turnover_ratio"]
    drift_val = r["rank_drift"]

    ratio_pctile = None
    drift_pctile = None
    if ratio_val is not None:
        less_equal_ratio = joined.filter(pl.col("turnover_ratio") <= ratio_val).height
        ratio_pctile = round((less_equal_ratio / total_symbols) * 100.0, 1)
    if drift_val is not None:
        less_equal_drift = joined.filter(pl.col("rank_drift") <= drift_val).height
        drift_pctile = round((less_equal_drift / total_symbols) * 100.0, 1)

    status = classify_momentum_status(
        r["avg_rank_short"],
        r["avg_rank_base"],
        r["rank_drift"],
        r["turnover_ratio"],
    )

    return {
        "symbol": r["symbol"],
        "status": status,
        "avg_turnover_short": (
            round(r["avg_turnover_short"], 2)
            if r["avg_turnover_short"] is not None
            else None
        ),
        "avg_turnover_base": (
            round(r["avg_turnover_base"], 2)
            if r["avg_turnover_base"] is not None
            else None
        ),
        "avg_rank_base": round(r["avg_rank_base"], 2),
        "avg_rank_short": round(r["avg_rank_short"], 2),
        "rank_drift": round(r["rank_drift"], 2),
        "turnover_ratio": (
            round(r["turnover_ratio"], 2)
            if r["turnover_ratio"] is not None
            else None
        ),
        "close": r["close"],
        "price_change_pct_window": (
            round(r["price_change_pct_window"], 2)
            if r["price_change_pct_window"] is not None
            else None
        ),
        "top_accumulator": acc,
        "top_distributor": dist,
        "percentile_turnover_ratio": ratio_pctile,
        "percentile_rank_drift": drift_pctile,
        "total_market_symbols": total_symbols,
    }


def find_similar_momentum(
    summary_df: pl.DataFrame,
    symbol: str,
    short_window: int = 5,
    base_window: int = 22,
    rollup: pl.DataFrame | None = None,
    top_n: int = 5,
) -> list[dict]:
    """Find stocks with a similar momentum profile using weighted Euclidean distance."""
    if summary_df.is_empty():
        return []
    joined, dominators = _compute_all_turnover_momentum(
        summary_df, short_window, base_window, rollup
    )
    if joined.height <= 1:
        return []
    sym_clean = symbol.upper().strip()
    target = joined.filter(pl.col("symbol") == sym_clean)
    if target.is_empty():
        return []

    t = target.row(0, named=True)
    if t["turnover_ratio"] is None:
        return []

    valid = joined.filter(pl.col("turnover_ratio").is_not_null())
    if valid.height <= 1:
        return []

    def _std(col_name: str) -> float:
        val = valid[col_name].std()
        return float(val) if val and val > 1e-6 else 1.0

    std_ratio = _std("turnover_ratio")
    std_drift = _std("rank_drift")
    std_price = _std("price_change_pct_window")
    std_rank = _std("avg_rank_short")

    t_ratio = float(t["turnover_ratio"])
    t_drift = float(t["rank_drift"])
    t_price = float(t["price_change_pct_window"] or 0.0)
    t_rank = float(t["avg_rank_short"])

    w_ratio, w_drift, w_price, w_rank = 0.35, 0.35, 0.15, 0.15

    candidates = []
    for r in valid.iter_rows(named=True):
        if r["symbol"] == sym_clean:
            continue
        d_ratio = (float(r["turnover_ratio"]) - t_ratio) / std_ratio
        d_drift = (float(r["rank_drift"]) - t_drift) / std_drift
        d_price = (float(r["price_change_pct_window"] or 0.0) - t_price) / std_price
        d_rank = (float(r["avg_rank_short"]) - t_rank) / std_rank

        dist = (
            w_ratio * (d_ratio**2)
            + w_drift * (d_drift**2)
            + w_price * (d_price**2)
            + w_rank * (d_rank**2)
        ) ** 0.5

        similarity_pct = round(100.0 / (1.0 + dist), 1)
        acc, dist_broker = dominators.get(r["symbol"], (None, None))
        status = classify_momentum_status(
            r["avg_rank_short"],
            r["avg_rank_base"],
            r["rank_drift"],
            r["turnover_ratio"],
        )

        candidates.append(
            {
                "symbol": r["symbol"],
                "similarity_pct": similarity_pct,
                "status": status,
                "avg_rank_base": round(r["avg_rank_base"], 2),
                "avg_rank_short": round(r["avg_rank_short"], 2),
                "rank_drift": round(r["rank_drift"], 2),
                "turnover_ratio": round(r["turnover_ratio"], 2),
                "close": r["close"],
                "price_change_pct_window": (
                    round(r["price_change_pct_window"], 2)
                    if r["price_change_pct_window"] is not None
                    else None
                ),
                "top_accumulator": acc,
                "top_distributor": dist_broker,
                "_dist": dist,
            }
        )

    candidates.sort(key=lambda x: x["_dist"])
    for c in candidates:
        del c["_dist"]
    return candidates[:top_n]


def screen_turnover_momentum(
    summary_df: pl.DataFrame,
    short_window: int = 5,
    base_window: int = 22,
    rollup: pl.DataFrame | None = None,
) -> tuple[list[dict], list[dict]]:
    """Compare short-window vs baseline turnover to flag structural liquidity change.

    For every symbol present in both windows compute:
      - avg_turnover_short / avg_turnover_base (per-session means)
      - avg_rank_short / avg_rank_base (mean turnover_rank)
      - rank_drift = avg_rank_base - avg_rank_short (positive = climbing the board)
      - turnover_ratio = avg_turnover_short / avg_turnover_base
      - window Δ% (close_first -> close_last across the short window)

    Returns (gainers, losers).
    """
    joined, dominators = _compute_all_turnover_momentum(
        summary_df, short_window, base_window, rollup
    )
    if joined.is_empty():
        return [], []

    def _to_rows(frame: pl.DataFrame, drift_asc: bool = True) -> list[dict]:
        rows = []
        for r in frame.iter_rows(named=True):
            acc, dist = dominators.get(r["symbol"], (None, None))
            rows.append(
                {
                    "symbol": r["symbol"],
                    "avg_rank_base": round(r["avg_rank_base"], 2),
                    "avg_rank_short": round(r["avg_rank_short"], 2),
                    "rank_drift": round(r["rank_drift"], 2),
                    "turnover_ratio": (
                        round(r["turnover_ratio"], 2)
                        if r["turnover_ratio"] is not None
                        else None
                    ),
                    "close": r["close"],
                    "price_change_pct_window": r["price_change_pct_window"],
                    "top_accumulator": acc,
                    "top_distributor": dist,
                }
            )
        return rows

    gainers = joined.filter(
        (pl.col("avg_rank_short") <= 50)
        & (pl.col("rank_drift") >= 15)
        & (pl.col("turnover_ratio") >= 1.75)
    ).sort("rank_drift", descending=True)

    losers = joined.filter(
        (pl.col("avg_rank_base") <= 40)
        & (pl.col("rank_drift") <= -15)
        & (pl.col("turnover_ratio") <= 0.50)
    ).sort("rank_drift", descending=False)

    return _to_rows(gainers, True), _to_rows(losers, False)


def classify_track_a(
    broker_id: int,
    nets: dict[str, int],
    margin_pct: float | None,
    t1_change: float,
    was_dominant_buyer: bool,
    is_top_seller_t1: bool,
) -> str | None:
    n1 = nets.get("T_1", 0)
    n22 = nets.get("T_22", 0)
    n66 = nets.get("T_66", 0)
    margin = margin_pct if margin_pct is not None else 0.0

    if is_top_seller_t1 and was_dominant_buyer and n1 < 0:
        return "DISTRIBUTION_TRAP"
    if n66 > 0 and n22 > 0 and n1 > 0 and -3.0 <= margin <= 3.0 and t1_change <= 2.0:
        return "SILENT_ACCUMULATION"
    if n22 > 0 and n1 > 0 and t1_change > 2.5 and margin > 5.0:
        return "ACTIVE_MARKUP"
    return None


def _top_n_by_net(agg: pl.DataFrame, symbol: str, n: int, descending: bool = True) -> pl.DataFrame:
    sub = agg.filter(pl.col("symbol") == symbol)
    if sub.is_empty():
        return sub
    return sub.sort("net_qty", descending=descending).head(n)


def screen_track_a(
    rollup: pl.DataFrame,
    summary: pl.DataFrame,
    windows: dict[str, list[date]],
    top_turnover: int = 20,
    top_holder_window: int = 22,
    sector: str | None = None,
    cap_tier: str | None = None,
) -> list[dict]:
    t1 = windows["T_1"]
    if not t1:
        return []
    t1_date = t1[-1]
    t1_sum = summary.filter(pl.col("trade_date") == t1_date)
    if t1_sum.is_empty():
        return []
    top_universe = t1_sum.filter(pl.col("turnover_rank") <= top_turnover)
    symbols = [s for s in top_universe["symbol"].to_list() if not _is_excluded(s)]

    aggs = {name: aggregate_window(rollup, dates) for name, dates in windows.items()}
    totals = {name: stock_window_totals(summary, dates) for name, dates in windows.items()}
    t1_closes = {
        r["symbol"]: float(r["close_price"])
        for r in t1_sum.iter_rows(named=True)
    }
    t1_chg = {
        r["symbol"]: float(r["price_change_pct"] or 0.0)
        for r in t1_sum.iter_rows(named=True)
    }
    t1_sectors = {
        r["symbol"]: r.get("sector")
        for r in t1_sum.iter_rows(named=True)
    }
    t1_caps = {
        r["symbol"]: float(r["market_cap"]) if r.get("market_cap") is not None else None
        for r in t1_sum.iter_rows(named=True)
    }
    t1_52w_high = {
        r["symbol"]: float(r["fifty_two_week_high"]) if r.get("fifty_two_week_high") is not None else None
        for r in t1_sum.iter_rows(named=True)
    }
    t1_52w_low = {
        r["symbol"]: float(r["fifty_two_week_low"]) if r.get("fifty_two_week_low") is not None else None
        for r in t1_sum.iter_rows(named=True)
    }
    t1_vwap = {
        r["symbol"]: float(r["vwap"]) if r.get("vwap") is not None else None
        for r in t1_sum.iter_rows(named=True)
    }

    results: list[dict] = []
    for row in top_universe.iter_rows(named=True):
        sym = row["symbol"]
        sec = t1_sectors.get(sym)
        mcap = t1_caps.get(sym)
        tier = classify_cap_tier(mcap)
        if sector and (not sec or sector.strip().lower() not in sec.strip().lower()):
            continue
        if cap_tier and tier.upper() != cap_tier.strip().upper():
            continue

        t1_agg = aggs["T_1"]
        if t1_agg.is_empty():
            continue
        buyers = _top_n_by_net(t1_agg, sym, 3, descending=True)
        sellers = _top_n_by_net(t1_agg, sym, 1, descending=False)
        top_seller = int(sellers["broker_id"][0]) if sellers.height else None

        def build_row(bid: int, rank: int, is_seller_row: bool = False) -> dict:
            nets = {}
            vwap = None
            for name, agg in aggs.items():
                hit = agg.filter((pl.col("symbol") == sym) & (pl.col("broker_id") == bid))
                nets[name] = int(hit["net_qty"][0]) if hit.height else 0
                if name == "T_66" and hit.height:
                    vwap = hit["buy_vwap"][0]
            close = t1_closes.get(sym)
            margin = None
            if vwap is not None and vwap and close:
                margin = (close - float(vwap)) / float(vwap) * 100.0
            t22_tot = totals["T_22"].filter(pl.col("symbol") == sym)
            t66_tot = totals["T_66"].filter(pl.col("symbol") == sym)
            t22_qty = int(t22_tot["total_qty"][0]) if t22_tot.height else 0
            t66_qty = int(t66_tot["total_qty"][0]) if t66_tot.height else 0
            t22_dom = (nets["T_22"] / t22_qty * 100.0) if t22_qty else 0.0
            t66_dom = (nets["T_66"] / t66_qty * 100.0) if t66_qty else 0.0
            was_dom = t22_dom >= 8.0 or t66_dom >= 8.0
            is_top_seller = top_seller == bid and nets["T_1"] < 0
            signal = classify_track_a(
                bid, nets, margin, t1_chg.get(sym, 0.0), was_dom, is_top_seller
            )
            return {
                "symbol": sym,
                "sector": sec,
                "market_cap": mcap,
                "cap_tier": tier,
                "fifty_two_week_high": t1_52w_high.get(sym),
                "fifty_two_week_low": t1_52w_low.get(sym),
                "session_vwap": t1_vwap.get(sym),
                "turnover_rank": int(row["turnover_rank"]),
                "t1_turnover": float(row["total_turnover"]),
                "broker_id": bid,
                "buyer_rank": rank,
                "net_t1": nets["T_1"],
                "net_t5": nets["T_5"],
                "net_t22": nets["T_22"],
                "net_t66": nets["T_66"],
                "buy_vwap": float(vwap) if vwap is not None else None,
                "close": close,
                "margin_pct": margin,
                "dominance_t22": t22_dom,
                "t1_change_pct": t1_chg.get(sym, 0.0),
                "signal": signal or "WATCH",
            }

        seen: set[int] = set()
        for rank, brow in enumerate(buyers.iter_rows(named=True), start=1):
            bid = int(brow["broker_id"])
            seen.add(bid)
            results.append(build_row(bid, rank))
        if top_seller is not None and top_seller not in seen:
            trap_row = build_row(top_seller, 0, is_seller_row=True)
            if trap_row["signal"] == "DISTRIBUTION_TRAP":
                results.append(trap_row)

    # Compute the top long-term holder per symbol (broker with the highest net
    # in the configured top_holder_window) and their most recent one-day move.
    # This supports the hold/sell early signal: a positive holder T1 = still accumulating (HOLD),
    # a negative holder T1 = starting to distribute (SELL warning).
    if results:
        sym_df = pl.DataFrame(results)
        net_col = f"net_t{top_holder_window}"
        holder_name = f"top_holder_net_{top_holder_window}d"
        # Use a distinct internal alias to avoid colliding with top_holder_net_1d
        # when top_holder_window == 1.
        _th_net = "__th_net__"
        top_holders = sym_df.group_by("symbol").agg(
            pl.col("broker_id").sort_by(net_col, descending=True).first().alias("top_holder_broker"),
            pl.col(net_col).sort_by(net_col, descending=True).first().alias(_th_net),
            pl.col("net_t1").sort_by(net_col, descending=True).first().alias("top_holder_net_1d"),
        )
        holder_map = {
            r["symbol"]: (r["top_holder_broker"], r[_th_net], r["top_holder_net_1d"])
            for r in top_holders.iter_rows(named=True)
        }
        for r in results:
            h = holder_map.get(r["symbol"])
            if h:
                r["top_holder_broker_id"], r[holder_name], r["top_holder_net_1d"] = h
            else:
                r["top_holder_broker_id"], r[holder_name], r["top_holder_net_1d"] = None, 0, 0
    else:
        for r in results:
            r["top_holder_broker_id"], r[f"top_holder_net_{top_holder_window}d"], r["top_holder_net_1d"] = None, 0, 0
    return results



def screen_track_b(
    rollup: pl.DataFrame,
    summary: pl.DataFrame,
    windows: dict[str, list[date]],
    sector: str | None = None,
    cap_tier: str | None = None,
) -> list[dict]:
    t1, t22 = windows["T_1"], windows["T_22"]
    if not t1 or not t22:
        return []
    t1_date = t1[-1]
    t22_agg = aggregate_window(rollup, t22)
    t1_sum = summary.filter(pl.col("trade_date") == t1_date)
    t22_tot = stock_window_totals(summary, t22)
    if t22_agg.is_empty() or t22_tot.is_empty() or t1_sum.is_empty():
        return []

    t1_sectors = {
        r["symbol"]: r.get("sector")
        for r in t1_sum.iter_rows(named=True)
    }
    t1_caps = {
        r["symbol"]: float(r["market_cap"]) if r.get("market_cap") is not None else None
        for r in t1_sum.iter_rows(named=True)
    }
    t1_52w_high = {
        r["symbol"]: float(r["fifty_two_week_high"]) if r.get("fifty_two_week_high") is not None else None
        for r in t1_sum.iter_rows(named=True)
    }
    t1_52w_low = {
        r["symbol"]: float(r["fifty_two_week_low"]) if r.get("fifty_two_week_low") is not None else None
        for r in t1_sum.iter_rows(named=True)
    }
    t1_vwap = {
        r["symbol"]: float(r["vwap"]) if r.get("vwap") is not None else None
        for r in t1_sum.iter_rows(named=True)
    }

    t22_raw = rollup.filter(pl.col("trade_date").is_in(t22))
    sell_share = t22_raw.group_by(["symbol", "broker_id"]).agg(
        sell_qty=pl.col("sell_qty").sum()
    )
    sell_tot = sell_share.group_by("symbol").agg(total_sell=pl.col("sell_qty").sum())
    top3_sell = (
        sell_share.sort(["symbol", "sell_qty"], descending=[False, True])
        .group_by("symbol", maintain_order=True)
        .head(3)
        .group_by("symbol")
        .agg(top3_sell_qty=pl.col("sell_qty").sum())
        .join(sell_tot, on="symbol", how="left")
        .with_columns(
            dispersion=pl.when(
                pl.col("total_sell").is_null() | (pl.col("total_sell") == 0)
            )
            .then(None)
            .otherwise(pl.col("top3_sell_qty") / pl.col("total_sell") * 100.0)
        )
    )

    t1_turn = {
        r["symbol"]: float(r["total_turnover"])
        for r in t1_sum.iter_rows(named=True)
    }
    results: list[dict] = []
    symbols = [s for s in t22_tot["symbol"].to_list() if not _is_excluded(s)]
    for sym in symbols:
        sec = t1_sectors.get(sym)
        mcap = t1_caps.get(sym)
        tier = classify_cap_tier(mcap)
        if sector and (not sec or sector.strip().lower() not in sec.strip().lower()):
            continue
        if cap_tier and tier.upper() != cap_tier.strip().upper():
            continue

        tot = t22_tot.filter(pl.col("symbol") == sym)
        if tot.height == 0:
            continue
        px = float(tot["price_change_pct"][0] or 0.0)
        if not (-4.0 <= px <= 4.0):
            continue
        qty = int(tot["total_qty"][0] or 0)
        avg_to = float(tot["avg_daily_turnover"][0] or 0.0)
        t1_to = t1_turn.get(sym, 0.0)
        if avg_to <= 0 or t1_to < 2.0 * avg_to:
            continue

        brokers = t22_agg.filter(pl.col("symbol") == sym).sort("net_qty", descending=True)
        if brokers.height == 0:
            continue
        top = brokers.row(0, named=True)
        net = int(top["net_qty"])
        absorption = (net / qty * 100.0) if qty else 0.0
        if absorption < 20.0:
            continue

        disp_row = top3_sell.filter(pl.col("symbol") == sym)
        dispersion = float(disp_row["dispersion"][0]) if disp_row.height and disp_row["dispersion"][0] is not None else 100.0
        if dispersion >= 25.0:
            continue

        vwap = top["buy_vwap"]
        close = float(t1_sum.filter(pl.col("symbol") == sym)["close_price"][0])
        margin = None
        if vwap is not None and vwap and close:
            margin = (close - float(vwap)) / float(vwap) * 100.0

        results.append(
            {
                "symbol": sym,
                "sector": sec,
                "market_cap": mcap,
                "cap_tier": tier,
                "fifty_two_week_high": t1_52w_high.get(sym),
                "fifty_two_week_low": t1_52w_low.get(sym),
                "session_vwap": t1_vwap.get(sym),
                "broker_id": int(top["broker_id"]),
                "net_t22": net,
                "absorption_pct": absorption,
                "dispersion_pct": dispersion,
                "t22_price_change_pct": px,
                "t1_turnover": t1_to,
                "t22_avg_turnover": avg_to,
                "volume_inflection": t1_to / avg_to if avg_to else None,
                "buy_vwap": float(vwap) if vwap is not None else None,
                "close": close,
                "margin_pct": margin,
                "signal": "STEALTH_ACCUMULATION",
            }
        )
    results.sort(key=lambda r: r["absorption_pct"], reverse=True)
    return results


def run_screener(
    as_of: date | None = None,
    persist: bool = True,
    top_turnover: int = 20,
    top_holder_window: int = 22,
    sector: str | None = None,
    cap_tier: str | None = None,
) -> tuple[list[dict], list[dict], dict]:
    """Run both tracks. If ``as_of`` is given, restrict to sessions <= that date
    (point-in-time backtest) and treat its last session as the T_1 date. When
    ``persist`` is True, upsert that session's actionable signals to history.
    """
    conn = get_conn()
    try:
        dates = fetch_trade_dates(conn)
        if as_of is not None:
            dates = [d for d in dates if d <= as_of]
        windows = window_dates(dates)
        all_needed = sorted({d for ds in windows.values() for d in ds})
        rollup = load_rollup(conn, all_needed)
        summary = load_summary(conn, all_needed)
        # Cast numeric-ish columns after DB round-trip
        if not rollup.is_empty():
            rollup = rollup.with_columns(
                pl.col("broker_id").cast(pl.Int32),
                pl.col("buy_qty").cast(pl.Int64),
                pl.col("sell_qty").cast(pl.Int64),
                pl.col("self_trade_qty").cast(pl.Int64),
                pl.col("matched_qty").cast(pl.Int64),
                pl.col("buy_amount").cast(pl.Float64),
                pl.col("sell_amount").cast(pl.Float64),
            )
        if not summary.is_empty():
            summary = summary.with_columns(
                pl.col("close_price").cast(pl.Float64),
                pl.col("price_change_pct").cast(pl.Float64),
                pl.col("total_qty").cast(pl.Int64),
                pl.col("total_turnover").cast(pl.Float64),
                pl.col("turnover_rank").cast(pl.Int32),
            )
        track_a = screen_track_a(
            rollup, summary, windows, top_turnover, top_holder_window, sector=sector, cap_tier=cap_tier
        )
        track_b = screen_track_b(rollup, summary, windows, sector=sector, cap_tier=cap_tier)
        t1 = windows["T_1"][-1] if windows["T_1"] else None

        persisted = 0
        if persist and t1 is not None and not sector and not cap_tier:
            from src.signals import persist_signals

            persisted = persist_signals(conn, track_a, track_b, t1)
            conn.commit()

        meta = {
            "sessions": len(dates),
            "t1": str(t1) if t1 else None,
            "as_of": str(as_of) if as_of else None,
            "persisted": persisted,
            "top_turnover": top_turnover,
            "top_holder_window": top_holder_window,
            "sector": sector,
            "cap_tier": cap_tier,
            "windows": {k: [str(d) for d in v] for k, v in windows.items()},
        }
        return track_a, track_b, meta
    finally:
        conn.close()


def inspect_symbol(symbol: str, sessions: int = 22) -> dict:
    """Deep-dive a single symbol: recent market history, per-broker net flows
    across all four windows, and its persisted signal history."""
    conn = get_conn()
    try:
        dates = fetch_trade_dates(conn)
        windows = window_dates(dates)
        all_needed = sorted({d for ds in windows.values() for d in ds})
        rollup = load_rollup(conn, all_needed)
        summary = load_summary(conn, all_needed)

        if summary.is_empty() or "symbol" not in summary.columns:
            return {
                "symbol": symbol,
                "sector": None,
                "market_cap": None,
                "cap_tier": "UNKNOWN",
                "fifty_two_week_high": None,
                "fifty_two_week_low": None,
                "vwap": None,
                "distance_52w_high_pct": None,
                "distance_52w_low_pct": None,
                "range_52w_pct": None,
                "recent": [],
                "brokers": [],
                "signals": [],
            }

        sym_sum = summary.filter(pl.col("symbol") == symbol)
        sym_roll = rollup.filter(pl.col("symbol") == symbol) if (not rollup.is_empty() and "symbol" in rollup.columns) else pl.DataFrame()
        if sym_sum.is_empty():
            return {
                "symbol": symbol,
                "sector": None,
                "market_cap": None,
                "cap_tier": "UNKNOWN",
                "fifty_two_week_high": None,
                "fifty_two_week_low": None,
                "vwap": None,
                "distance_52w_high_pct": None,
                "distance_52w_low_pct": None,
                "range_52w_pct": None,
                "recent": [],
                "brokers": [],
                "signals": [],
            }

        sym_sum = sym_sum.with_columns(
            pl.col("close_price").cast(pl.Float64),
            pl.col("price_change_pct").cast(pl.Float64),
            pl.col("total_qty").cast(pl.Int64),
            pl.col("total_turnover").cast(pl.Float64),
            pl.col("turnover_rank").cast(pl.Int32),
        )
        sym_roll = sym_roll.with_columns(
            pl.col("broker_id").cast(pl.Int32),
            pl.col("buy_amount").cast(pl.Float64),
            pl.col("sell_amount").cast(pl.Float64),
        )
        aggs = {name: aggregate_window(sym_roll, ds) for name, ds in windows.items()}

        recent = [
            {
                "trade_date": r["trade_date"],
                "close_price": r["close_price"],
                "change_pct": r["price_change_pct"],
                "qty": r["total_qty"],
                "turnover": r["total_turnover"],
                "rank": r["turnover_rank"],
            }
            for r in sym_sum.sort("trade_date").tail(sessions).iter_rows(named=True)
        ]

        close = recent[-1]["close_price"] if recent else None

        latest_row = sym_sum.sort("trade_date").tail(1).row(0, named=True)
        sec = latest_row.get("sector")
        mcap = float(latest_row["market_cap"]) if latest_row.get("market_cap") is not None else None
        h52 = float(latest_row["fifty_two_week_high"]) if latest_row.get("fifty_two_week_high") is not None else None
        l52 = float(latest_row["fifty_two_week_low"]) if latest_row.get("fifty_two_week_low") is not None else None
        vwap_val = float(latest_row["vwap"]) if latest_row.get("vwap") is not None else None

        if not sec or mcap is None or h52 is None:
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT sector, market_cap, fifty_two_week_high, fifty_two_week_low FROM securities_meta WHERE symbol = %s",
                        (symbol,),
                    )
                    m_row = cur.fetchone()
                    if m_row:
                        if not sec and m_row[0]:
                            sec = m_row[0]
                        if mcap is None and m_row[1] is not None:
                            mcap = float(m_row[1])
                        if h52 is None and m_row[2] is not None:
                            h52 = float(m_row[2])
                        if l52 is None and m_row[3] is not None:
                            l52 = float(m_row[3])
            except Exception:
                try:
                    conn.rollback()
                except Exception:
                    pass

        dist_high = round((close - h52) / h52 * 100.0, 2) if (close and h52 and h52 > 0) else None
        dist_low = round((close - l52) / l52 * 100.0, 2) if (close and l52 and l52 > 0) else None
        range_pos = round((close - l52) / (h52 - l52) * 100.0, 1) if (close and h52 and l52 and h52 > l52) else None

        broker_map: dict[int, dict] = {}
        for name, agg in aggs.items():
            for b in agg.iter_rows(named=True):
                bid = int(b["broker_id"])
                entry = broker_map.setdefault(
                    bid,
                    {
                        "broker_id": bid,
                        "net_1d": 0,
                        "net_5d": 0,
                        "net_22d": 0,
                        "net_66d": 0,
                        "buy_vwap": None,
                    },
                )
                entry[f"net_{name[2:]}d"] = int(b["net_qty"])  # T_1 -> net_1d
                if name == "T_66":
                    entry["buy_vwap"] = b["buy_vwap"]
        brokers = []
        for bid, e in broker_map.items():
            margin = None
            if close and e["buy_vwap"]:
                margin = (close - float(e["buy_vwap"])) / float(e["buy_vwap"]) * 100.0
            e["close"] = close
            e["margin_pct"] = margin
            brokers.append(e)
        brokers.sort(key=lambda r: r["net_22d"], reverse=True)
        accumulators_22d = [b for b in brokers if b.get("net_22d", 0) > 0]
        distributors_22d = sorted([b for b in brokers if b.get("net_22d", 0) < 0], key=lambda b: b.get("net_22d", 0))

        buyers_1d = sorted([b for b in brokers if b.get("net_1d", 0) > 0], key=lambda b: b.get("net_1d", 0), reverse=True)
        sellers_1d = sorted([b for b in brokers if b.get("net_1d", 0) < 0], key=lambda b: b.get("net_1d", 0))

        top_accumulator_22d = accumulators_22d[0] if accumulators_22d else None
        top_distributor_22d = distributors_22d[0] if distributors_22d else None
        top_accumulator_1d = buyers_1d[0] if buyers_1d else None
        top_distributor_1d = sellers_1d[0] if sellers_1d else None

        from src.signals import load_signal_history

        sig_rows = load_signal_history(conn, symbol=symbol, limit=100)
        mom = symbol_turnover_momentum(
            summary.with_columns(
                pl.col("close_price").cast(pl.Float64),
                pl.col("total_turnover").cast(pl.Float64),
                pl.col("turnover_rank").cast(pl.Int32),
            ),
            symbol=symbol,
            short_window=5,
            base_window=22,
            rollup=rollup.with_columns(
                pl.col("broker_id").cast(pl.Int32),
                pl.col("buy_amount").cast(pl.Float64),
                pl.col("sell_amount").cast(pl.Float64),
            ),
        )
        return {
            "symbol": symbol,
            "sector": sec,
            "market_cap": mcap,
            "cap_tier": classify_cap_tier(mcap),
            "fifty_two_week_high": h52,
            "fifty_two_week_low": l52,
            "vwap": vwap_val,
            "distance_52w_high_pct": dist_high,
            "distance_52w_low_pct": dist_low,
            "range_52w_pct": range_pos,
            "recent": recent,
            "brokers": brokers,
            "accumulators": accumulators_22d,
            "distributors": distributors_22d,
            "signals": sig_rows,
            "top_holder_22d": top_accumulator_22d,
            "top_distributor_22d": top_distributor_22d,
            "top_holder_1d": top_accumulator_1d,
            "top_distributor_1d": top_distributor_1d,
            "momentum": mom,
        }
    finally:
        conn.close()



def load_top_turnover(
    conn,
    as_of: date | None = None,
    limit: int = 20,
    sector: str | None = None,
    cap_tier: str | None = None,
) -> dict:
    """Clean per-session top-turnover ranking for a chosen date with sector & cap tier support.

    Returns ``{"date": "YYYY-MM-DD", "rows": [...]}`` where each row is
    ``{rank, symbol, close, change_pct, qty, turnover, sector, market_cap, cap_tier, fifty_two_week_high, fifty_two_week_low, vwap}``.
    If ``as_of`` is given but that exact session is absent (e.g. a non-trading day),
    it falls back to the most recent trading session on or before the requested date.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT MAX(trade_date) FROM daily_market_summary")
        fetch_res = cur.fetchone()
        latest = fetch_res[0] if fetch_res else None
        target = as_of if as_of is not None else latest
        if target is None:
            return {"date": None, "rows": []}
        cur.execute(
            "SELECT DISTINCT trade_date FROM daily_market_summary "
            "WHERE trade_date <= %s ORDER BY trade_date DESC LIMIT 1",
            (target,),
        )
        row = cur.fetchone()
        if not row:
            return {"date": None, "rows": []}
        d = row[0]
        try:
            cur.execute(
                "SELECT s.symbol, s.close_price, s.price_change_pct, s.total_qty, "
                "s.total_turnover, s.turnover_rank, "
                "COALESCE(s.sector, m.sector) AS sector, "
                "COALESCE(s.market_cap, m.market_cap) AS market_cap, "
                "COALESCE(s.fifty_two_week_high, m.fifty_two_week_high) AS fifty_two_week_high, "
                "COALESCE(s.fifty_two_week_low, m.fifty_two_week_low) AS fifty_two_week_low, "
                "s.vwap "
                "FROM daily_market_summary s "
                "LEFT JOIN securities_meta m ON s.symbol = m.symbol "
                "WHERE s.trade_date = %s ORDER BY s.turnover_rank ASC NULLS LAST",
                (d,),
            )
            raw_rows = cur.fetchall()
            has_extended = True
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass
            try:
                cur.execute(
                    "SELECT symbol, close_price, price_change_pct, total_qty, "
                    "total_turnover, turnover_rank, sector, market_cap, "
                    "fifty_two_week_high, fifty_two_week_low, vwap "
                    "FROM daily_market_summary "
                    "WHERE trade_date = %s ORDER BY turnover_rank ASC NULLS LAST",
                    (d,),
                )
                raw_rows = cur.fetchall()
                has_extended = True
            except Exception:
                try:
                    conn.rollback()
                except Exception:
                    pass
                cur.execute(
                    "SELECT symbol, close_price, price_change_pct, total_qty, "
                    "total_turnover, turnover_rank "
                    "FROM daily_market_summary "
                    "WHERE trade_date = %s ORDER BY turnover_rank ASC NULLS LAST",
                    (d,),
                )
                raw_rows = cur.fetchall()
                has_extended = False

        rows = []
        has_extended = bool(raw_rows and len(raw_rows[0]) >= 11)
        for idx, r in enumerate(raw_rows):
            if has_extended:
                mcap = float(r[7]) if r[7] is not None else None
                sec = r[6]
                tier = classify_cap_tier(mcap)
                high_52w = float(r[8]) if r[8] is not None else None
                low_52w = float(r[9]) if r[9] is not None else None
                vwap_val = float(r[10]) if r[10] is not None else None
            else:
                mcap = None
                sec = None
                tier = "UNKNOWN"
                high_52w = None
                low_52w = None
                vwap_val = None

            if sector and (not sec or sector.strip().lower() not in sec.strip().lower()):
                continue
            if cap_tier and tier.upper() != cap_tier.strip().upper():
                continue

            rank_val = int(r[5]) if (len(r) > 5 and r[5] is not None) else (idx + 1)
            rows.append(
                {
                    "rank": rank_val,
                    "symbol": r[0],
                    "close": float(r[1]) if r[1] is not None else None,
                    "change_pct": float(r[2]) if r[2] is not None else None,
                    "qty": int(r[3]) if r[3] is not None else 0,
                    "turnover": float(r[4]) if r[4] is not None else 0.0,
                    "sector": sec,
                    "market_cap": mcap,
                    "cap_tier": tier,
                    "fifty_two_week_high": high_52w,
                    "fifty_two_week_low": low_52w,
                    "vwap": vwap_val,
                }
            )
            if len(rows) >= int(limit if limit else 20):
                break
    return {"date": str(d), "rows": rows}


def broker_holdings(conn, broker_id: int, sessions: int = 66) -> list[dict]:
    """Top holdings for a broker across the four windows (returns list of dicts).

    Mirrors the CLI ``broker`` deep-dive but returns data only, so the CLI and
    the web API share a single implementation.
    """
    dates = fetch_trade_dates(conn)
    window_dates = dates[-sessions:] if sessions and dates else []
    if not window_dates:
        return []
    rollup = load_rollup(conn, window_dates)
    summary = load_summary(conn, window_dates)
    t1_date = window_dates[-1]
    t1_sum = summary.filter(pl.col("trade_date") == t1_date)
    if t1_sum.is_empty():
        return []

    window_aggs = {
        "T_1": window_dates[-1:],
        "T_5": window_dates[-5:],
        "T_22": window_dates[-22:],
        "T_66": window_dates[-66:],
    }
    aggs = {name: aggregate_window(rollup, dts) for name, dts in window_aggs.items()}
    if aggs["T_1"].is_empty():
        return []

    close_map = {
        r["symbol"]: float(r["close_price"])
        for r in t1_sum.iter_rows(named=True)
        if r.get("close_price") is not None
    }
    symbols = [
        s
        for s in aggs["T_1"].filter(pl.col("broker_id") == broker_id)["symbol"].to_list()
        if not _is_excluded(s)
    ]
    if not symbols:
        return []

    holdings: list[dict] = []
    for sym in symbols:
        nets: dict[str, int] = {}
        vwap = None
        for name, agg in aggs.items():
            hit = agg.filter(
                (pl.col("symbol") == sym) & (pl.col("broker_id") == broker_id)
            )
            nets[name] = int(hit["net_qty"][0]) if hit.height else 0
            if hit.height and name == "T_66":
                vwap = hit["buy_vwap"][0]
        close = close_map.get(sym)
        margin = None
        if vwap and close:
            margin = (close - float(vwap)) / float(vwap) * 100.0
        holdings.append(
            {
                "symbol": sym,
                "net_t1": nets["T_1"],
                "net_t5": nets["T_5"],
                "net_t22": nets["T_22"],
                "net_t66": nets["T_66"],
                "margin_pct": margin,
            }
        )
    holdings.sort(key=lambda h: h["net_t22"], reverse=True)
    return holdings


def wash_report(
    conn,
    window: int = 22,
    min_qty: int = 5000,
    include_all: bool = False,
    as_of: date | None = None,
) -> dict:
    """Broker-level + session-level internal-matching scan as plain data.

    Returns ``{"window", "latest_session", "broker": [...], "session": [...]}``
    where ``broker`` rows are ``{broker_id, buy_qty, sell_qty, matched_qty,
    gross_volume, match_pct}`` and ``session`` rows are ``{symbol, total_qty,
    crossed_qty, session_match_pct}``.
    """
    dates = fetch_trade_dates(conn)
    if as_of is not None:
        dates = [d for d in dates if d <= as_of]
    if not dates:
        return {"window": window, "latest_session": None, "broker": [], "session": []}
    window_dates = dates[-window:]
    summary = load_summary(conn, window_dates)
    rollup = load_rollup(conn, window_dates)
    if not rollup.is_empty():
        rollup = rollup.with_columns(
            pl.col("broker_id").cast(pl.Int32),
            pl.col("buy_qty").cast(pl.Int64),
            pl.col("sell_qty").cast(pl.Int64),
            pl.col("self_trade_qty").cast(pl.Int64),
            pl.col("matched_qty").cast(pl.Int64),
            pl.col("buy_amount").cast(pl.Float64),
            pl.col("sell_amount").cast(pl.Float64),
        )
    broker_match = compute_broker_match_pct(rollup, window_dates)
    session_match = compute_session_match_pct(rollup, summary, window_dates)

    unique_symbols: set[str] = set()
    if not rollup.is_empty():
        unique_symbols = {
            s for s in rollup["symbol"].unique().to_list() if not _is_excluded(s)
        }
    valid_brokers: set[int] = set()
    if not rollup.is_empty():
        valid_brokers = set(
            rollup.filter(
                (pl.col("symbol").is_in(unique_symbols))
                & (pl.col("matched_qty") * 2 >= min_qty)
            )["broker_id"].unique().to_list()
        )

    if not broker_match.is_empty():
        broker_match = broker_match.filter(pl.col("broker_id").is_in(valid_brokers))
    if not session_match.is_empty():
        session_match = session_match.filter(
            pl.col("symbol").is_in(unique_symbols)
            & (pl.col("crossed_qty") >= min_qty)
        )

    broker_rows = (
        broker_match.sort("match_pct", descending=True).to_dicts()
        if not broker_match.is_empty()
        else []
    )
    session_rows = (
        session_match.sort("session_match_pct", descending=True).to_dicts()
        if not session_match.is_empty()
        else []
    )
    return {
        "window": int(window),
        "latest_session": str(window_dates[-1]),
        "broker": broker_rows,
        "session": session_rows,
    }


def position_analysis(symbol: str) -> dict:
    """Long-only positioning breakdown for a single NEPSE symbol.

    Computes tick-level price extremes, relative volume (RVOL), broker
    buy/sell pressure, top-3 buyer/seller concentration, wash trade %,
    and produces a deterministic 3-line positioning verdict.

    Returns a dict with ``context`` (raw data metrics) and ``breakdown``
    (three structured analysis lines + verdict badge).
    """
    conn = get_conn()
    try:
        dates = fetch_trade_dates(conn)
        if not dates:
            return {"symbol": symbol, "error": "no trade dates available"}

        latest = dates[-1]
        lookback_20 = dates[-20:] if len(dates) >= 20 else dates

        # --- Latest session summary ---
        summary = load_summary(conn, [latest])
        if summary.is_empty():
            return {"symbol": symbol, "error": "no summary data for latest session"}

        sym_sum = summary.filter(pl.col("symbol") == symbol)
        if sym_sum.is_empty():
            return {"symbol": symbol, "error": f"{symbol} not traded on {latest}"}

        sym_sum = sym_sum.with_columns(
            pl.col("close_price").cast(pl.Float64),
            pl.col("price_change_pct").cast(pl.Float64),
            pl.col("total_qty").cast(pl.Int64),
            pl.col("total_turnover").cast(pl.Float64),
        )
        row = sym_sum.sort("trade_date").tail(1).row(0, named=True)
        ltp = float(row["close_price"])
        change_pct = float(row["price_change_pct"])
        total_qty = int(row["total_qty"])
        total_turnover = float(row["total_turnover"])

        # Sector / cap tier / 52w
        sec = row.get("sector")
        mcap = float(row["market_cap"]) if row.get("market_cap") is not None else None
        cap_tier = classify_cap_tier(mcap)
        h52 = float(row["fifty_two_week_high"]) if row.get("fifty_two_week_high") is not None else None
        l52 = float(row["fifty_two_week_low"]) if row.get("fifty_two_week_low") is not None else None

        # --- Share Structure ---
        if mcap and mcap > 0 and ltp > 0:
            total_shares = int((mcap * 1_000_000) / ltp)
        else:
            total_shares = 10_000_000  # Fallback

        if sec in ["Commercial Banks", "Development Banks", "Microfinance"]:
            public_ratio = 0.49
        else:
            public_ratio = 0.30
            
        public_shares = int(total_shares * public_ratio)
        promoter_shares = total_shares - public_shares
        
        public_ratio_pct = (public_shares / total_shares) * 100 if total_shares > 0 else 0.0
        float_turnover_pct = (total_qty / public_shares) * 100 if public_shares > 0 else 0.0

        # --- Day high/low from floorsheet ---
        day_high = ltp
        day_low = ltp
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT MAX(rate), MIN(rate) FROM floorsheet "
                    "WHERE symbol = %s AND trade_date = %s",
                    (symbol, latest),
                )
                hilo = cur.fetchone()
                if hilo and hilo[0] is not None:
                    day_high = float(hilo[0])
                    day_low = float(hilo[1])
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass

        # --- RVOL (20-day average volume) ---
        avg_20_vol = float(total_qty)
        if len(lookback_20) > 1:
            hist_summary = load_summary(conn, lookback_20[:-1])
            if not hist_summary.is_empty():
                hist_sym = hist_summary.filter(pl.col("symbol") == symbol)
                if not hist_sym.is_empty():
                    hist_sym = hist_sym.with_columns(pl.col("total_qty").cast(pl.Int64))
                    avg_20_vol = hist_sym["total_qty"].mean()
                    if avg_20_vol is None or avg_20_vol == 0:
                        avg_20_vol = float(total_qty)
                    else:
                        avg_20_vol = float(avg_20_vol)

        rvol = round(total_qty / avg_20_vol, 2) if avg_20_vol > 0 else 1.0

        # --- Broker rollup for latest session ---
        rollup = load_rollup(conn, [latest])
        sym_roll = (
            rollup.filter(pl.col("symbol") == symbol)
            if not rollup.is_empty() and "symbol" in rollup.columns
            else pl.DataFrame()
        )

        buy_turnover = 0.0
        sell_turnover = 0.0
        top3_buyers = []
        top3_sellers = []
        top3_buy_qty = 0
        top3_sell_qty = 0
        matched_qty_total = 0
        lead_buyer_id = None
        lead_seller_id = None

        if not sym_roll.is_empty():
            sym_roll = sym_roll.with_columns(
                pl.col("broker_id").cast(pl.Int32),
                pl.col("buy_qty").cast(pl.Int64),
                pl.col("sell_qty").cast(pl.Int64),
                pl.col("buy_amount").cast(pl.Float64),
                pl.col("sell_amount").cast(pl.Float64),
                pl.col("matched_qty").cast(pl.Int64),
            )
            buy_turnover = float(sym_roll["buy_amount"].sum())
            sell_turnover = float(sym_roll["sell_amount"].sum())
            matched_qty_total = int(sym_roll["matched_qty"].sum())

            # Top 3 buyers by buy_qty
            buyers = sym_roll.sort("buy_qty", descending=True).head(3)
            top3_buyers = buyers.to_dicts()
            top3_buy_qty = int(buyers["buy_qty"].sum())
            if buyers.height > 0:
                lead_buyer_id = int(buyers[0, "broker_id"])

            # Top 3 sellers by sell_qty
            sellers = sym_roll.sort("sell_qty", descending=True).head(3)
            top3_sellers = sellers.to_dicts()
            top3_sell_qty = int(sellers["sell_qty"].sum())
            if sellers.height > 0:
                lead_seller_id = int(sellers[0, "broker_id"])

        # --- Derived percentages ---
        top3_buy_pct = round(top3_buy_qty / total_qty * 100, 2) if total_qty > 0 else 0.0
        top3_sell_pct = round(top3_sell_qty / total_qty * 100, 2) if total_qty > 0 else 0.0
        wash_pct = round(matched_qty_total / total_qty * 100, 2) if total_qty > 0 else 0.0
        pressure_ratio = round(buy_turnover / (sell_turnover + 1e-5), 2)
        net_absorption_ratio = round(top3_buy_qty / (top3_sell_qty + 1e-5), 2)

        # Day range and rejection
        day_range = day_high - day_low
        if day_range > 0:
            upper_rejection = round((day_high - ltp) / day_range, 4)
        else:
            upper_rejection = 0.0

        # --- Deterministic scoring engine ---
        score = 0

        # Price action signals
        if upper_rejection < 0.25:
            score += 1  # Closed near highs
        elif upper_rejection > 0.60:
            score -= 2  # Severe rejection from highs

        if change_pct > 0:
            score += 1
        elif change_pct < -2.0:
            score -= 1

        # Volume signal
        if rvol >= 1.5:
            if upper_rejection < 0.30:
                score += 1  # High volume + close near highs = absorption
            elif upper_rejection > 0.50:
                score -= 1  # High volume + rejection = distribution

        # Buyer/seller concentration
        if net_absorption_ratio > 1.3:
            score += 1
        elif net_absorption_ratio < 0.7:
            score -= 1

        if top3_buy_pct > top3_sell_pct + 10:
            score += 1
        elif top3_sell_pct > top3_buy_pct + 10:
            score -= 1

        # Wash trade penalty
        if wash_pct > 25:
            score -= 1

        # Clamp
        score = max(-5, min(5, score))

        # --- 3-line breakdown ---
        # 1. Supply & Price Action
        if upper_rejection < 0.25 and rvol >= 1.5:
            supply_line = (
                f"Traded {float_turnover_pct:.1f}% of public float today ({rvol:.1f}x RVOL); "
                f"strong float absorption near daily highs ({ltp} NPR, rejection {upper_rejection:.0%}). "
                f"Buyers maintained control through the session."
            )
        elif upper_rejection < 0.25:
            supply_line = (
                f"Traded {float_turnover_pct:.1f}% of public float today ({rvol:.1f}x RVOL); "
                f"price held near session highs ({ltp} NPR, rejection {upper_rejection:.0%}). "
                f"Steady absorption, no panic selling."
            )
        elif upper_rejection > 0.50 and rvol >= 1.5:
            supply_line = (
                f"Traded {float_turnover_pct:.1f}% of public float today ({rvol:.1f}x RVOL). "
                f"Price rejected off highs — intraday high {day_high} NPR saw {upper_rejection:.0%} rejection to close {ltp} NPR. "
                f"Supply overwhelmed demand at higher levels."
            )
        elif upper_rejection > 0.50:
            supply_line = (
                f"Traded {float_turnover_pct:.1f}% of public float today ({rvol:.1f}x RVOL). "
                f"Weak close with {upper_rejection:.0%} rejection from session high {day_high} NPR. "
                f"Sellers dominated the tape."
            )
        else:
            supply_line = (
                f"Traded {float_turnover_pct:.1f}% of public float today ({rvol:.1f}x RVOL). "
                f"Mixed session: {ltp} NPR with {upper_rejection:.0%} rejection from high. "
                f"Neither buyers nor sellers decisively in control."
            )

        # 2. Operator Intent
        if net_absorption_ratio > 1.3 and top3_buy_pct > 40:
            intent_line = (
                f"Smart money concentrated on buy side — Top 3 buyers absorbed {top3_buy_pct:.1f}% "
                f"of volume (Lead: Broker {lead_buyer_id}) vs sellers at {top3_sell_pct:.1f}%. "
                f"Net absorption ratio {net_absorption_ratio:.2f}x signals institutional accumulation."
            )
        elif net_absorption_ratio < 0.7 and top3_sell_pct > 40:
            intent_line = (
                f"Major brokers offloading — Top 3 sellers dumped {top3_sell_pct:.1f}% of volume "
                f"(Lead: Broker {lead_seller_id}) to fragmented buyers at {top3_buy_pct:.1f}%. "
                f"Distribution pattern with {net_absorption_ratio:.2f}x absorption ratio."
            )
        elif wash_pct > 25:
            intent_line = (
                f"High wash/matching volume at {wash_pct:.1f}% signals artificial turnover. "
                f"Top 3 buyers: {top3_buy_pct:.1f}% vs sellers: {top3_sell_pct:.1f}%. "
                f"Exercise caution — genuine directional flow unclear."
            )
        else:
            intent_line = (
                f"Balanced broker activity — Top 3 buyers hold {top3_buy_pct:.1f}% "
                f"vs sellers at {top3_sell_pct:.1f}% (Lead buyer: Broker {lead_buyer_id}). "
                f"No extreme concentration on either side; monitoring for directional commitment."
            )

        # 3. Verdict
        if score >= 4:
            verdict = "Strong Buy"
            verdict_detail = (
                f"Score {score}/5 — Clean float absorption with concentrated institutional buying. "
                f"Strong close on elevated volume supports a long entry."
            )
        elif score >= 2:
            verdict = "Buy"
            verdict_detail = (
                f"Score {score}/5 — Constructive accumulation pattern with moderate conviction. "
                f"Lean long with measured position sizing."
            )
        elif score <= -3:
            verdict = "Avoid / Exit"
            verdict_detail = (
                f"Score {score}/5 — Distribution signals dominate: heavy rejection, "
                f"seller concentration, or wash trading. Exit longs or stay flat."
            )
        elif score <= -1:
            verdict = "Avoid / Exit"
            verdict_detail = (
                f"Score {score}/5 — Weak price action and/or unfavorable broker flow. "
                f"Not a clean setup for long positioning."
            )
        else:
            verdict = "Hold"
            verdict_detail = (
                f"Score {score}/5 — Neutral signals. Neither strong accumulation nor distribution. "
                f"Wait for clearer directional commitment before acting."
            )

        return {
            "symbol": symbol,
            "trade_date": str(latest),
            "context": {
                "ltp": ltp,
                "price_change_pct": change_pct,
                "day_high": day_high,
                "day_low": day_low,
                "day_range": round(day_range, 2),
                "upper_rejection": upper_rejection,
                "total_qty": total_qty,
                "total_turnover": total_turnover,
                "rvol": rvol,
                "avg_20_volume": round(avg_20_vol, 0),
                "sector": sec,
                "market_cap": mcap,
                "cap_tier": cap_tier,
                "fifty_two_week_high": h52,
                "fifty_two_week_low": l52,
                "buy_turnover": buy_turnover,
                "sell_turnover": sell_turnover,
                "pressure_ratio": pressure_ratio,
                "top_3_buy_pct": top3_buy_pct,
                "top_3_sell_pct": top3_sell_pct,
                "top_buyer_broker": lead_buyer_id,
                "top_seller_broker": lead_seller_id,
                "net_absorption_ratio": net_absorption_ratio,
                "wash_pct": wash_pct,
                "matched_qty": matched_qty_total,
            },
            "share_structure": {
                "public_shares": public_shares,
                "promoter_shares": promoter_shares,
                "total_shares": total_shares,
                "public_ratio_pct": round(public_ratio_pct, 1),
                "float_turnover_pct": round(float_turnover_pct, 2),
            },
            "breakdown": {
                "supply_price_action": supply_line,
                "operator_intent": intent_line,
                "verdict": verdict,
                "verdict_detail": verdict_detail,
                "score": score,
            },
        }
    finally:
        conn.close()

def screen_track_c_smart_money(conn, dates: list[date]) -> dict:
    """
    TRACK_C: SMART MONEY ABSORPTION
    Integrates Playwright scraping, AI analysis, and Discord alerts.
    """
    if not dates:
        return {"error": "no dates"}
        
    target_date = dates[-1].isoformat()
    top_brokers, detected_date = get_top_brokers_today(num_brokers=3, side='buyer', target_date=target_date)
    
    if not top_brokers:
        return {"date": target_date, "message": "No smart money accumulation detected today.", "brokers": []}
        
    results = []
    
    with conn.cursor() as cur:
        for broker in top_brokers:
            # Query the DB to find what this top broker actually accumulated the most today
            cur.execute("""
                SELECT 
                    r.symbol, 
                    (r.buy_qty - r.sell_qty) AS net_qty,
                    m.close_price,
                    m.fifty_two_week_high,
                    m.sector
                FROM daily_broker_rollup r
                JOIN daily_market_summary m ON r.symbol = m.symbol AND r.trade_date = m.trade_date
                WHERE r.broker_id = %s AND r.trade_date = %s
                ORDER BY (r.buy_qty - r.sell_qty) DESC
                LIMIT 1
            """, (broker['id'], target_date))
            
            row = cur.fetchone()
            if not row:
                continue
                
            stock_symbol = row[0]
            net_qty = row[1]
            if net_qty <= 0:
                continue # Only care if they are net buyers of their top stock
                
            close_price = row[2]
            high_52 = row[3]
            sector = row[4]
            
            stock_data = f"LTP: {close_price}, 52W High: {high_52}, Sector: {sector}, Broker Net Shares: {net_qty}"
            
            # Fetch deterministic verdict to prevent AI hallucination and contradictions
            try:
                pos_data = position_analysis(stock_symbol)
                verdict = pos_data["breakdown"]["verdict"]
                verdict_detail = pos_data["breakdown"]["verdict_detail"]
                stock_data += f"\nDeterministic Scoring Engine Overall Verdict: {verdict}\nEngine Detail: {verdict_detail}"
            except Exception as e:
                print(f"Failed to fetch position analysis for {stock_symbol}: {e}")
            
            # 2. Generate AI Alert
            insight = generate_broker_alert(stock_symbol, broker['id'], broker['name'], net_qty, stock_data)
            
            # 3. Send Discord Alert
            send_alert(insight, title=f"🚨 SMART MONEY ALERT: {stock_symbol}")
            
            results.append({
                "broker_id": broker['id'],
                "broker_name": broker['name'],
                "net_qty": f"{net_qty:,}",
                "stock_symbol": stock_symbol,
                "ai_insight": insight
            })
            
    return {
        "date": detected_date,
        "brokers": results
    }

def market_overview() -> list:
    """Run position analysis for all active symbols concurrently."""
    import concurrent.futures
    conn = get_conn()
    try:
        dates = fetch_trade_dates(conn)
        if not dates:
            return []
        latest = dates[-1]
        
        with conn.cursor() as cur:
            cur.execute(
                "SELECT DISTINCT symbol FROM daily_market_summary "
                "WHERE trade_date = %s AND close_price > 0 AND total_qty > 0 "
                "ORDER BY symbol ASC",
                (latest,)
            )
            symbols = [r[0] for r in cur.fetchall()]
    finally:
        conn.close()
        
    def _analyze(sym):
        try:
            res = position_analysis(sym)
            if not res or res.get("error"):
                return None
            return res
        except Exception:
            return None
            
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
        results = list(executor.map(_analyze, symbols))
        
    return [r for r in results if r]

def sector_overview(conn) -> list[dict]:
    """Aggregate turnover, price change, and top broker flow by sector."""
    dates = fetch_trade_dates(conn)
    if not dates:
        return []
    latest = dates[-1]
    
    query_market = 'SELECT symbol, sector, total_turnover, price_change_pct FROM daily_market_summary WHERE trade_date = %s'
    market_df = pl.read_database(query=query_market, connection=conn, execute_options={'parameters': (latest,)})
    
    if market_df.is_empty():
        return []
        
    query_broker = 'SELECT symbol, broker_id, (buy_qty - sell_qty) as net_qty FROM daily_broker_rollup WHERE trade_date = %s'
    broker_df = pl.read_database(query=query_broker, connection=conn, execute_options={'parameters': (latest,)})
    
    if broker_df.is_empty():
        return []
        
    joined = broker_df.join(market_df.select(['symbol', 'sector']), on='symbol', how='inner').drop_nulls('sector')
    broker_sector = joined.group_by(['sector', 'broker_id']).agg(pl.col('net_qty').sum())
    top_brokers = broker_sector.sort(['sector', 'net_qty'], descending=[False, True]).group_by('sector').first()
    
    market_stats = market_df.drop_nulls('sector').group_by('sector').agg([
        pl.col('total_turnover').sum(),
        pl.col('price_change_pct').mean().alias('avg_price_change'),
        pl.count('symbol').alias('num_stocks')
    ])
    
    final_df = market_stats.join(top_brokers, on='sector', how='left').sort('total_turnover', descending=True)
    
    # Convert Decimals and NaNs appropriately
    res = final_df.to_dicts()
    for row in res:
        if row.get('total_turnover') is not None:
            row['total_turnover'] = float(row['total_turnover'])
        if row.get('avg_price_change') is not None:
            row['avg_price_change'] = float(row['avg_price_change'])
    
    return res

def detect_syndicates(conn, window: int = 22) -> list[dict]:
    """Identify broker pairs that co-accumulate the same stocks."""
    import itertools
    dates = fetch_trade_dates(conn)
    if not dates:
        return []
    if len(dates) > window:
        dates = dates[-window:]
    
    query = 'SELECT symbol, broker_id, (buy_qty - sell_qty) as net_qty FROM daily_broker_rollup WHERE trade_date >= %s'
    df = pl.read_database(query=query, connection=conn, execute_options={'parameters': (dates[0],)})
    
    if df.is_empty():
        return []
        
    agg_df = df.group_by(['symbol', 'broker_id']).agg(pl.col('net_qty').sum())
    agg_df = agg_df.filter(pl.col('net_qty') > 0)
    agg_df = agg_df.sort(['symbol', 'net_qty'], descending=[False, True])
    
    top3 = agg_df.group_by('symbol', maintain_order=True).head(3)
    top3_dict = top3.group_by('symbol').agg(pl.col('broker_id').alias('brokers')).to_dicts()
    
    pair_counts = {}
    pair_stocks = {}
    
    for row in top3_dict:
        brokers = sorted(row['brokers'])
        if len(brokers) >= 2:
            for pair in itertools.combinations(brokers, 2):
                pair_counts[pair] = pair_counts.get(pair, 0) + 1
                if pair not in pair_stocks:
                    pair_stocks[pair] = []
                pair_stocks[pair].append(row['symbol'])
                
    sorted_pairs = sorted(pair_counts.items(), key=lambda x: x[1], reverse=True)
    
    results = []
    for pair, count in sorted_pairs[:50]:
        results.append({
            'broker_a': pair[0],
            'broker_b': pair[1],
            'co_occurrences': count,
            'symbols': pair_stocks[pair]
        })
        
    return results

def backtest_signals(conn) -> list[dict]:
    """Calculate historical T+5 and T+20 win rates for algorithmic signals."""
    dates = fetch_trade_dates(conn)
    dates_map = {d: i for i, d in enumerate(dates)}
    
    with conn.cursor() as cur:
        cur.execute('SELECT trade_date, symbol, signal FROM screener_signals_history')
        signals = cur.fetchall()
        
        cur.execute('SELECT trade_date, symbol, close_price FROM daily_market_summary')
        market = cur.fetchall()
        
    prices = {(row[0], row[1]): float(row[2]) for row in market}
    
    results = {}
    
    for s_date, symbol, signal in signals:
        if s_date not in dates_map: continue
        idx = dates_map[s_date]
        
        base_price = prices.get((s_date, symbol))
        if not base_price: continue
            
        t5_ret = None
        if idx + 5 < len(dates):
            t5_price = prices.get((dates[idx + 5], symbol))
            if t5_price: t5_ret = ((t5_price - base_price) / base_price * 100)
            
        t20_ret = None
        if idx + 20 < len(dates):
            t20_price = prices.get((dates[idx + 20], symbol))
            if t20_price: t20_ret = ((t20_price - base_price) / base_price * 100)
            
        if signal not in results:
            results[signal] = {'count':0, 't5_sum':0, 't5_wins':0, 't5_count':0, 't20_sum':0, 't20_wins':0, 't20_count':0}
            
        results[signal]['count'] += 1
        if t5_ret is not None:
            results[signal]['t5_count'] += 1
            results[signal]['t5_sum'] += t5_ret
            if t5_ret > 0: results[signal]['t5_wins'] += 1
        if t20_ret is not None:
            results[signal]['t20_count'] += 1
            results[signal]['t20_sum'] += t20_ret
            if t20_ret > 0: results[signal]['t20_wins'] += 1
            
    final = []
    for sig, data in results.items():
        t5_avg = data['t5_sum'] / data['t5_count'] if data['t5_count'] else None
        t5_win = data['t5_wins'] / data['t5_count'] * 100 if data['t5_count'] else None
        t20_avg = data['t20_sum'] / data['t20_count'] if data['t20_count'] else None
        t20_win = data['t20_wins'] / data['t20_count'] * 100 if data['t20_count'] else None
        final.append({
            'signal': sig, 
            'count': data['count'],
            't5_avg': t5_avg, 
            't5_win': t5_win,
            't20_avg': t20_avg, 
            't20_win': t20_win
        })
        
    return sorted(final, key=lambda x: x['count'], reverse=True)


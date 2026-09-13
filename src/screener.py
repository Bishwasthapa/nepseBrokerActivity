"""Dual-track NEPSE screener: Track A momentum/traps, Track B stealth radar."""

from __future__ import annotations

from datetime import date

import polars as pl

from src.db import get_conn

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


def load_summary(conn, dates: list[date]) -> pl.DataFrame:
    return _load_frame(
        conn,
        """
        SELECT trade_date, symbol, close_price, price_change_pct,
               total_qty, total_turnover, turnover_rank
        FROM daily_market_summary
        WHERE trade_date = ANY(%s)
        """,
        (dates,),
    )


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

    Returns (gainers, losers). Symbols missing data for the shorter window are
    simply excluded (no crash) — the edge case of a symbol newer than the
    baseline window.
    """
    if summary_df.is_empty():
        return [], []
    dates = sorted(summary_df["trade_date"].unique().to_list())
    if not dates:
        return [], []

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

    dominators = _dominant_brokers(rollup, short_dates)

    def _to_rows(frame: pl.DataFrame, drift_asc: bool) -> list[dict]:
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

    results: list[dict] = []
    for row in top_universe.iter_rows(named=True):
        sym = row["symbol"]
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
        track_a = screen_track_a(rollup, summary, windows, top_turnover, top_holder_window)
        track_b = screen_track_b(rollup, summary, windows)
        t1 = windows["T_1"][-1] if windows["T_1"] else None

        persisted = 0
        if persist and t1 is not None:
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

        sym_sum = summary.filter(pl.col("symbol") == symbol)
        sym_roll = rollup.filter(pl.col("symbol") == symbol)
        if sym_sum.is_empty():
            return {"symbol": symbol, "recent": [], "brokers": [], "signals": []}

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
        top_holder_22d = brokers[0] if brokers else None
        brokers_by_1d = sorted(brokers, key=lambda r: r["net_1d"], reverse=True)
        top_holder_1d = brokers_by_1d[0] if brokers_by_1d else None

        from src.signals import load_signal_history

        sig_rows = load_signal_history(conn, symbol=symbol, limit=100)
        return {
            "symbol": symbol,
            "recent": recent,
            "brokers": brokers,
            "signals": sig_rows,
            "top_holder_22d": top_holder_22d,
            "top_holder_1d": top_holder_1d,
        }
    finally:
        conn.close()



def load_top_turnover(
    conn, as_of: date | None = None, limit: int = 20
) -> dict:
    """Clean per-session top-turnover ranking for a chosen date.

    Returns ``{"date": "YYYY-MM-DD", "rows": [...]}`` where each row is
    ``{rank, symbol, close, change_pct, qty, turnover}``. If ``as_of`` is given
    but that exact session is absent (e.g. a non-trading day), it falls back to
    the most recent trading session on or before the requested date.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT MAX(trade_date) FROM daily_market_summary")
        latest = cur.fetchone()[0]
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
        cur.execute(
            "SELECT symbol, close_price, price_change_pct, total_qty, "
            "total_turnover, turnover_rank "
            "FROM daily_market_summary "
            "WHERE trade_date = %s ORDER BY turnover_rank ASC LIMIT %s",
            (d, int(limit)),
        )
        rows = [
            {
                "rank": int(r[5]),
                "symbol": r[0],
                "close": float(r[1]) if r[1] is not None else None,
                "change_pct": float(r[2]) if r[2] is not None else None,
                "qty": int(r[3]) if r[3] is not None else 0,
                "turnover": float(r[4]) if r[4] is not None else 0.0,
            }
            for r in cur.fetchall()
        ]
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

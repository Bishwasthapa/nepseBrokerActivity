"""Dual-track NEPSE screener: Track A momentum/traps, Track B stealth radar."""

from __future__ import annotations

from datetime import date

import polars as pl

from src.db import get_conn

WINDOWS = {"T_1": 1, "T_5": 5, "T_22": 22, "T_66": 66}


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
        .otherwise(pl.col("buy_amount").sum() / pl.col("buy_qty").sum()),
        sell_vwap=pl.when(pl.col("sell_qty").sum() == 0)
        .then(None)
        .otherwise(pl.col("sell_amount").sum() / pl.col("sell_qty").sum()),
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
    return crossed.join(sm, on=["trade_date", "symbol"], how="left").with_columns(
        total_qty=pl.col("total_qty").fill_null(0),
        session_match_pct=pl.when(
            pl.col("total_qty").fill_null(0) > 0
        )
        .then(100.0 * pl.col("crossed_qty") / pl.col("total_qty").fill_null(0))
        .otherwise(0.0),
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
) -> list[dict]:
    t1 = windows["T_1"]
    if not t1:
        return []
    t1_date = t1[-1]
    t1_sum = summary.filter(pl.col("trade_date") == t1_date)
    if t1_sum.is_empty():
        return []
    top_universe = t1_sum.filter(pl.col("turnover_rank") <= top_turnover)
    symbols = top_universe["symbol"].to_list()

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
    symbols = t22_tot["symbol"].to_list()
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
        track_a = screen_track_a(rollup, summary, windows, top_turnover)
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


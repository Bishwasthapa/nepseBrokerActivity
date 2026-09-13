"""Per-symbol rank <-> broker <-> price analysis and forward-return prediction.

Looks at a single symbol across recent trading sessions and combines three
dimensions:

* **Turnover rank** -- where the symbol ranks by turnover each session and how
  that rank moves vs. its forward price change (Spearman) and rank buckets.
* **Broker accumulation signature** -- who is net-buying vs distributing, how
  broad the accumulation is (MULTI vs SINGLE), and whether the dominant broker
  repeated (``sustained``).
* **Forward returns** -- close-to-close return over T+N sessions, bucketed by
  the accumulation signature, plus a next-trade prediction with a simple
  confidence band.

Everything is expressible in pure Polars + Python (no scipy dependency); the
Spearman correlation is computed by ranking then taking Pearson on the ranks.
"""

from __future__ import annotations

import math

import polars as pl

from src.screener import fetch_trade_dates, load_rollup, load_summary

# Range of horizons we measure forward returns over (sessions).
DEFAULT_HORIZONS = (1, 3)
# Turnover-rank buckets expressed as fractions of the session's traded symbols.
RANK_BUCKETS = (("LEADER", 0.15), ("MID", 0.50), ("MINOR", 1.0))
SIGNATURES = ("MULTI", "SINGLE", "DISTRIBUTE", "NEUTRAL")


class SessionCalendar:
    """Maps trade dates to zero-based trading-session indexes."""

    def __init__(self, dates: list) -> None:
        self.index = {d: i for i, d in enumerate(sorted(dates))}

    def get(self, d):
        return self.index.get(d)


def classify_signature(top_accum_net, net_breadth) -> str:
    """Classify a session's broker crowd into one of four signatures.

    * DISTRIBUTE -- the top netting broker sold, so net flow is negative.
    * MULTI      -- >= 3 brokers net-bought: broad accumulation.
    * SINGLE     -- exactly one net-buying broker: single dominant accumulator.
    * NEUTRAL    -- no clear signal (no activity, or 2 positive brokers, etc.).
    """
    if top_accum_net is None:
        return "NEUTRAL"
    if top_accum_net < 0:
        return "DISTRIBUTE"
    if net_breadth is not None and net_breadth >= 3:
        return "MULTI"
    if net_breadth == 1:
        return "SINGLE"
    return "NEUTRAL"


def daily_flows(rollup: pl.DataFrame) -> pl.DataFrame:
    """Per (trade_date, symbol) broker-crowd stats for a single symbol rollup.

    Expects ``rollup`` already filtered to one symbol. Returns one row per
    (trade_date, symbol) with the top accumulator/distributor, net breadth,
    concentration (|top net| / sum of |broker nets|) and the dominant buyer.
    """
    if rollup.is_empty():
        return pl.DataFrame()
    per_broker = (
        rollup.group_by(["trade_date", "symbol", "broker_id"])
        .agg(net_qty=(pl.col("buy_qty") - pl.col("sell_qty")).sum())
    )
    net = pl.col("net_qty")
    return (
        per_broker.group_by(["trade_date", "symbol"])
        .agg(
            top_accum_id=pl.col("broker_id")
            .sort_by(net, descending=True).first(),
            top_accum_net=net.sort_by(net, descending=True).first(),
            top_distrib_id=pl.col("broker_id")
            .sort_by(net, descending=False).first(),
            top_distrib_net=net.sort_by(net, descending=False).first(),
            net_breadth=(net > 0).sum(),
            sum_abs_net=net.abs().sum(),
        )
        .with_columns(
            concentration=pl.when(pl.col("sum_abs_net") > 0)
            .then((pl.col("top_accum_net").abs() / pl.col("sum_abs_net")).cast(pl.Float64))
            .otherwise(None)
        )
        .drop("sum_abs_net")
    )
def _pearson(x: list[float], y: list[float]) -> float | None:
    n = len(x)
    if n < 3:
        return None
    mx = sum(x) / n
    my = sum(y) / n
    num = sum((a - mx) * (b - my) for a, b in zip(x, y))
    dx = math.sqrt(sum((a - mx) ** 2 for a in x))
    dy = math.sqrt(sum((b - my) ** 2 for b in y))
    if dx == 0 or dy == 0:
        return None
    return num / (dx * dy)


def _spearman(df: pl.DataFrame, col_a: str, col_b: str) -> float | None:
    """Spearman rank correlation between two columns (ties averaged)."""
    d = df.select(
        [
            pl.col(col_a).rank("average").alias("ra"),
            pl.col(col_b).rank("average").alias("rb"),
        ]
    ).drop_nulls()
    if d.height < 3:
        return None
    return _pearson(d["ra"].to_list(), d["rb"].to_list())


def _win_stats(values: list[float]) -> dict:
    n = len(values)
    if n < 1:
        return {"samples": 0, "win_rate_pct": None, "avg_return_pct": None,
                "worst_return_pct": None, "best_return_pct": None}
    wins = sum(1 for v in values if v > 0)
    return {
        "samples": n,
        "win_rate_pct": round(wins / n * 100.0, 2),
        "avg_return_pct": round(sum(values) / n, 2),
        "worst_return_pct": round(min(values), 2),
        "best_return_pct": round(max(values), 2),
    }


def _rank_bucket(rank_pctile: float) -> str:
    for name, cutoff in RANK_BUCKETS:
        if rank_pctile <= cutoff:
            return name
def symbol_timeline(conn, symbol: str, sessions: int = 30,
                    horizons=DEFAULT_HORIZONS) -> tuple[list[dict], list[date]]:
    """Load + enrich a symbol's recent session timeline.

    Returns ``(rows, all_dates)``. Each row carries the market summary for the
    session, the broker-crowd signature, and T+N forward returns for the
    requested horizons (measured against the session close).
    """
    all_dates = fetch_trade_dates(conn)
    if not all_dates:
        return [], all_dates
    max_h = max(horizons)
    span = sessions + max_h
    span_dates = all_dates[-span:]
    summary = load_summary(conn, span_dates)
    rollup = load_rollup(conn, span_dates)
    if summary.is_empty():
        return [], all_dates

    session_totals = summary.group_by("trade_date").agg(n=pl.len())
    sym = summary.filter(pl.col("symbol") == symbol)
    if sym.is_empty():
        return [], all_dates

    sym = sym.with_columns(
        pl.col("close_price").cast(pl.Float64),
        pl.col("price_change_pct").cast(pl.Float64),
        pl.col("total_qty").cast(pl.Int64),
        pl.col("total_turnover").cast(pl.Float64),
        pl.col("turnover_rank").cast(pl.Int32),
    )
    sym_roll = rollup.filter(pl.col("symbol") == symbol).with_columns(
        pl.col("broker_id").cast(pl.Int32),
        pl.col("buy_qty").cast(pl.Int64),
        pl.col("sell_qty").cast(pl.Int64),
    )

    flows = daily_flows(sym_roll)
    tl = (
        sym.join(flows, on=["trade_date", "symbol"], how="left")
        .join(session_totals, on="trade_date", how="left")
        .sort("trade_date")
    )
    prev_accum = tl["top_accum_id"].shift(1)
    tl = tl.with_columns(
        sustained=(pl.col("top_accum_id") == prev_accum).fill_null(False),
        signature=pl.struct(["top_accum_net", "net_breadth"])
        .map_elements(
            lambda d: classify_signature(d["top_accum_net"], d["net_breadth"]),
            return_dtype=pl.Utf8,
        ),
    )

    cal = SessionCalendar(all_dates)
    rows = []
    for r in tl.iter_rows(named=True):
        close = r.get("close_price")
        total = r.get("n")
        out = {
            "trade_date": r["trade_date"],
            "rank": r.get("turnover_rank"),
            "symbol_total": total,
            "rank_pctile": None if total in (None, 0)
            else round(float(r["turnover_rank"]) / float(total), 4),
            "close": close,
            "change_pct": r.get("price_change_pct"),
            "qty": r.get("total_qty"),
            "turnover": r.get("total_turnover"),
            "top_accum_id": r.get("top_accum_id"),
            "top_accum_net": r.get("top_accum_net"),
            "top_distrib_net": r.get("top_distrib_net"),
            "net_breadth": r.get("net_breadth"),
            "concentration": None if r.get("concentration") is None
            else round(float(r["concentration"]), 3),
            "sustained": bool(r.get("sustained")),
            "signature": r.get("signature", "NEUTRAL"),
        }
        for h in horizons:
            out[f"fwd_{h}"] = None
        rows.append(out)

    # Forward returns measured against the symbol's own future close.
    close_by_idx = {
        cal.index[r["trade_date"]]: float(r["close_price"])
        for r in tl.iter_rows(named=True)
        if r.get("trade_date") in cal.index and r.get("close_price") is not None
    }
    for out in rows:
        close = out["close"]
        if close is None:
            continue
        i = cal.index.get(out["trade_date"])
        if i is None:
            continue
        for h in horizons:
            c_h = close_by_idx.get(i + h)
            out[f"fwd_{h}"] = None if c_h is None else round(
                (float(c_h) - float(close)) / float(close) * 100.0, 2
            )
    return rows, all_dates
def rank_price_relation(timeline_rows: list[dict], horizons=DEFAULT_HORIZONS,
                        sessions: int = 30) -> dict:
    """Spearman rank<->price stats + bucket forward returns over the window."""
    inner = max(horizons)
    pred = [r for r in timeline_rows if r.get(f"fwd_{inner}") is not None][-sessions:]
    df = pl.DataFrame(
        [
            {
                "rank": r.get("rank"),
                "rank_pctile": r.get("rank_pctile"),
                "close": r.get("close"),
                **{f"fwd_{h}": r.get(f"fwd_{h}") for h in horizons},
            }
            for r in pred
        ]
    )
    df_rank = df.filter(pl.col("rank").is_not_null(), pl.col("rank_pctile").is_not_null())
    relation: dict = {
        "n": df_rank.height,
        "spearman_rank_t1_return": _spearman(df_rank, "rank", "fwd_1")
        if "fwd_1" in df_rank.columns else None,
        "spearman_rank_close": _spearman(df_rank, "rank", "close"),
        "buckets": [],
    }
    for name, _ in RANK_BUCKETS:
        bucket = [
            r for r in df.iter_rows(named=True)
            if _rank_bucket(r["rank_pctile"] or 1.0) == name
        ]
        stats = {}
        for h in horizons:
            vals = [b[f"fwd_{h}"] for b in bucket if b.get(f"fwd_{h}") is not None]
            stats[f"fwd_{h}"] = _win_stats(vals)
        relation["buckets"].append({"bucket": name, "n": len(bucket), **stats})
    return relation


def forward_returns(timeline_rows: list[dict], horizons=DEFAULT_HORIZONS,
                    sessions: int = 30) -> dict:
    """Per-signature T+N forward-return stats within the prediction window."""
    inner = max(horizons)
    pred = [r for r in timeline_rows if r.get(f"fwd_{inner}") is not None][-sessions:]
    out: dict[str, dict] = {}
    for sig in SIGNATURES:
        matches = [r for r in pred if r["signature"] == sig]
        per_h = {}
        for h in horizons:
            vals = [m[f"fwd_{h}"] for m in matches if m.get(f"fwd_{h}") is not None]
            per_h[h] = _win_stats(vals)
        out[sig] = {"samples": len(matches), "horizons": per_h}
    return out


def confidence(n: int) -> str:
    if n < 3:
        return "LOW"
    if n < 8:
        return "MEDIUM"
    return "HIGH"


def predict_next(timeline_rows: list[dict], horizons=DEFAULT_HORIZONS,
                 sessions: int = 30) -> list[dict]:
    """Next-trade prediction for each horizon from the latest predictable row.

    Uses the most recent session from which we can still observe the full
    forward window, and blends the row's own signature with the historical
    per-signature return distribution.
    """
    inner = max(horizons)
    pred = [r for r in timeline_rows if r.get(f"fwd_{inner}") is not None]
    latest = pred[-1] if pred else None
    if latest is None:
        return []
    sig_perf = forward_returns(timeline_rows, horizons, sessions)
    preds = []
    for h in horizons:
        hist = sig_perf[latest["signature"]]["horizons"][h]
        n_hist = hist["samples"]
        expected = hist["avg_return_pct"]
        if expected is None or n_hist == 0:
            bias = "NEUTRAL"
        elif expected > 0:
            bias = "BULLISH"
        else:
            bias = "BEARISH"
        preds.append(
            {
                "horizon": h,
                "signature": latest["signature"],
                "as_of": latest["trade_date"],
                "bias": bias,
                "confidence": confidence(n_hist),
                "n": n_hist,
                "hist_avg_return_pct": expected,
                "hist_win_rate_pct": hist["win_rate_pct"],
                "fwd_actual_pct": latest[f"fwd_{h}"],
            }
        )
    return preds


def symbol_analyze(conn, symbol: str, sessions: int = 30,
                   horizons=DEFAULT_HORIZONS) -> dict:
    """Full analysis bundle for a symbol (rows + rank/broker/price breakdown)."""
    rows, _ = symbol_timeline(conn, symbol, sessions=sessions, horizons=horizons)
    if not rows:
        return {"symbol": symbol, "error": "no data for symbol"}
    return {
        "symbol": symbol,
        "sessions": sessions,
        "horizons": sorted(horizons),
        "timeline": rows,
        "rank_relation": rank_price_relation(rows, horizons, sessions),
        "signature_perf": forward_returns(rows, horizons, sessions),
        "prediction": predict_next(rows, horizons, sessions),
    }
    return "MINOR"
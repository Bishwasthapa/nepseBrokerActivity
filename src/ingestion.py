"""High-speed Polars ingestion: floorsheet -> daily rollups and market summaries."""

from __future__ import annotations

import io
from datetime import date
from typing import Iterable

import polars as pl
import psycopg2.extras

from src.db import get_conn

FLOORSheet_COLUMNS = [
    "trade_date",
    "contract_id",
    "symbol",
    "buyer_broker",
    "seller_broker",
    "quantity",
    "rate",
    "amount",
]


def floorsheet_frame(rows: Iterable[dict] | pl.DataFrame) -> pl.DataFrame:
    if isinstance(rows, pl.DataFrame):
        df = rows
    else:
        df = pl.DataFrame(list(rows))
    return df.select(
        pl.col("trade_date").cast(pl.Date),
        pl.col("contract_id").cast(pl.Int64),
        pl.col("symbol").cast(pl.Utf8),
        pl.col("buyer_broker").cast(pl.Int16),
        pl.col("seller_broker").cast(pl.Int16),
        pl.col("quantity").cast(pl.Int32),
        pl.col("rate").cast(pl.Float64),
        pl.col("amount").cast(pl.Float64),
        pl.when(pl.col("buyer_broker") == pl.col("seller_broker"))
        .then(pl.col("quantity"))
        .otherwise(0)
        .alias("matched_quantity"),
    )


def compute_daily_broker_rollup(df: pl.DataFrame) -> pl.DataFrame:
    """Aggregate buy/sell/self-trade metrics per (date, symbol, broker).

    Wash trades (buyer_broker == seller_broker) are excluded from directional
    buy/sell qty and amount, and logged separately as self_trade_qty / matched_qty.
    """
    if "matched_quantity" not in df.columns:
        df = df.with_columns(
            pl.when(pl.col("buyer_broker") == pl.col("seller_broker"))
            .then(pl.col("quantity"))
            .otherwise(0)
            .alias("matched_quantity")
        )
    directional = df.filter(pl.col("buyer_broker") != pl.col("seller_broker"))
    wash = df.filter(pl.col("buyer_broker") == pl.col("seller_broker"))

    buys = (
        directional.group_by(
            ["trade_date", "symbol", pl.col("buyer_broker").alias("broker_id")]
        ).agg(
            buy_qty=pl.col("quantity").sum(),
            buy_amount=pl.col("amount").sum(),
        )
    )
    sells = (
        directional.group_by(
            ["trade_date", "symbol", pl.col("seller_broker").alias("broker_id")]
        ).agg(
            sell_qty=pl.col("quantity").sum(),
            sell_amount=pl.col("amount").sum(),
        )
    )
    self_trades = wash.group_by(
        ["trade_date", "symbol", pl.col("buyer_broker").alias("broker_id")]
    ).agg(
        self_trade_qty=pl.col("quantity").sum(),
        matched_qty=pl.col("matched_quantity").sum(),
    )

    keys = pl.concat(
        [
            buys.select(["trade_date", "symbol", "broker_id"]),
            sells.select(["trade_date", "symbol", "broker_id"]),
            self_trades.select(["trade_date", "symbol", "broker_id"]),
        ]
    ).unique()

    return (
        keys.join(buys, on=["trade_date", "symbol", "broker_id"], how="left")
        .join(sells, on=["trade_date", "symbol", "broker_id"], how="left")
        .join(self_trades, on=["trade_date", "symbol", "broker_id"], how="left")
        .with_columns(
            pl.col("buy_qty").fill_null(0).cast(pl.Int64),
            pl.col("buy_amount").fill_null(0.0),
            pl.col("sell_qty").fill_null(0).cast(pl.Int64),
            pl.col("sell_amount").fill_null(0.0),
            pl.col("self_trade_qty").fill_null(0).cast(pl.Int64),
            pl.col("matched_qty").fill_null(0).cast(pl.Int64),
        )
    )


def compute_daily_market_summary(
    df: pl.DataFrame,
    prev_close: dict[tuple[date, str], float] | None = None,
    meta: dict[str, dict] | None = None,
) -> pl.DataFrame:
    """Stock-level daily summary: close, % change, qty, turnover, rank, vwap, sector, market_cap, 52w range."""
    last_print = (
        df.sort(["trade_date", "symbol", "contract_id"])
        .group_by(["trade_date", "symbol"], maintain_order=True)
        .agg(close_price=pl.col("rate").last())
    )
    totals = df.group_by(["trade_date", "symbol"]).agg(
        total_qty=pl.col("quantity").sum(),
        total_turnover=pl.col("amount").sum(),
    )
    summary = last_print.join(totals, on=["trade_date", "symbol"])

    if prev_close:
        prev_rows = [
            {"trade_date": d, "symbol": s, "prev_close": p}
            for (d, s), p in prev_close.items()
        ]
        prev_df = (
            pl.DataFrame(prev_rows)
            if prev_rows
            else pl.DataFrame(
                schema={"trade_date": pl.Date, "symbol": pl.Utf8, "prev_close": pl.Float64}
            )
        )
        summary = summary.join(prev_df, on=["trade_date", "symbol"], how="left")
    else:
        summary = summary.with_columns(pl.lit(None).cast(pl.Float64).alias("prev_close"))

    summary = summary.with_columns(
        price_change_pct=pl.when(
            pl.col("prev_close").is_null() | (pl.col("prev_close") == 0)
        )
        .then(0.0)
        .otherwise(
            (pl.col("close_price") - pl.col("prev_close")) / pl.col("prev_close") * 100.0
        )
    ).drop("prev_close")

    summary = summary.with_columns(
        turnover_rank=pl.col("total_turnover")
        .rank(method="ordinal", descending=True)
        .over("trade_date")
        .cast(pl.Int32),
        vwap=pl.when(pl.col("total_qty") > 0)
        .then((pl.col("total_turnover") / pl.col("total_qty").cast(pl.Float64)).round(2))
        .otherwise(None),
    )

    if meta:
        meta_rows = [
            {
                "symbol": sym,
                "sector": m.get("sector"),
                "market_cap": m.get("market_cap"),
                "fifty_two_week_high": m.get("fifty_two_week_high"),
                "fifty_two_week_low": m.get("fifty_two_week_low"),
            }
            for sym, m in meta.items()
        ]
        meta_df = pl.DataFrame(meta_rows)
        summary = summary.join(meta_df, on="symbol", how="left")
    else:
        for col in ["sector", "market_cap", "fifty_two_week_high", "fifty_two_week_low"]:
            if col not in summary.columns:
                summary = summary.with_columns(pl.lit(None).alias(col))

    return summary.select(
        "trade_date",
        "symbol",
        "close_price",
        "price_change_pct",
        "total_qty",
        "total_turnover",
        "turnover_rank",
        "sector",
        "market_cap",
        "fifty_two_week_high",
        "fifty_two_week_low",
        "vwap",
    )


def _copy_dataframe(conn, table: str, df: pl.DataFrame, columns: list[str]) -> None:
    if df.is_empty():
        return
    buf = io.StringIO()
    df.select(columns).write_csv(buf, include_header=False)
    buf.seek(0)
    with conn.cursor() as cur:
        cur.copy_expert(
            f"COPY {table} ({', '.join(columns)}) FROM STDIN WITH (FORMAT CSV)",
            buf,
        )



def _upsert_rollup(conn, df: pl.DataFrame) -> None:
    if df.is_empty():
        return
    rows = list(df.iter_rows(named=True))
    sql = """
        INSERT INTO daily_broker_rollup
            (trade_date, symbol, broker_id, buy_qty, buy_amount,
             sell_qty, sell_amount, self_trade_qty, matched_qty)
        VALUES %s
        ON CONFLICT (trade_date, symbol, broker_id) DO UPDATE SET
            buy_qty = EXCLUDED.buy_qty,
            buy_amount = EXCLUDED.buy_amount,
            sell_qty = EXCLUDED.sell_qty,
            sell_amount = EXCLUDED.sell_amount,
            self_trade_qty = EXCLUDED.self_trade_qty,
            matched_qty = EXCLUDED.matched_qty
    """
    values = [
        (
            r["trade_date"],
            r["symbol"],
            int(r["broker_id"]),
            int(r["buy_qty"]),
            float(r["buy_amount"]),
            int(r["sell_qty"]),
            float(r["sell_amount"]),
            int(r["self_trade_qty"]),
            int(r["matched_qty"]),
        )
        for r in rows
    ]
    with conn.cursor() as cur:
        psycopg2.extras.execute_values(cur, sql, values, page_size=5000)


def fetch_securities_meta(conn) -> dict[str, dict]:
    """Fetch cached company metadata (sector, market cap, 52-week range) from DB."""
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT symbol, sector, market_cap, fifty_two_week_high, fifty_two_week_low FROM securities_meta"
            )
            return {
                r[0]: {
                    "sector": r[1],
                    "market_cap": float(r[2]) if r[2] is not None else None,
                    "fifty_two_week_high": float(r[3]) if r[3] is not None else None,
                    "fifty_two_week_low": float(r[4]) if r[4] is not None else None,
                }
                for r in cur.fetchall()
            }
    except Exception:
        return {}


def _upsert_summary(conn, df: pl.DataFrame) -> None:
    if df.is_empty():
        return
    for col in ["sector", "market_cap", "fifty_two_week_high", "fifty_two_week_low", "vwap"]:
        if col not in df.columns:
            df = df.with_columns(pl.lit(None).alias(col))

    rows = list(df.iter_rows(named=True))
    sql = """
        INSERT INTO daily_market_summary
            (trade_date, symbol, close_price, price_change_pct,
             total_qty, total_turnover, turnover_rank,
             sector, market_cap, fifty_two_week_high, fifty_two_week_low, vwap)
        VALUES %s
        ON CONFLICT (trade_date, symbol) DO UPDATE SET
            close_price = EXCLUDED.close_price,
            price_change_pct = EXCLUDED.price_change_pct,
            total_qty = EXCLUDED.total_qty,
            total_turnover = EXCLUDED.total_turnover,
            turnover_rank = EXCLUDED.turnover_rank,
            sector = COALESCE(EXCLUDED.sector, daily_market_summary.sector),
            market_cap = COALESCE(EXCLUDED.market_cap, daily_market_summary.market_cap),
            fifty_two_week_high = COALESCE(EXCLUDED.fifty_two_week_high, daily_market_summary.fifty_two_week_high),
            fifty_two_week_low = COALESCE(EXCLUDED.fifty_two_week_low, daily_market_summary.fifty_two_week_low),
            vwap = COALESCE(EXCLUDED.vwap, daily_market_summary.vwap)
    """
    values = [
        (
            r["trade_date"],
            r["symbol"],
            float(r["close_price"]),
            float(r["price_change_pct"]),
            int(r["total_qty"]),
            float(r["total_turnover"]),
            int(r["turnover_rank"]),
            r["sector"],
            float(r["market_cap"]) if r["market_cap"] is not None else None,
            float(r["fifty_two_week_high"]) if r["fifty_two_week_high"] is not None else None,
            float(r["fifty_two_week_low"]) if r["fifty_two_week_low"] is not None else None,
            float(r["vwap"]) if r["vwap"] is not None else None,
        )
        for r in rows
    ]
    with conn.cursor() as cur:
        psycopg2.extras.execute_values(cur, sql, values, page_size=2000)



def fetch_prev_closes(
    conn, dates: list[date], symbols: list[str]
) -> dict[tuple[date, str], float]:
    if not dates or not symbols:
        return {}
    with conn.cursor() as cur:
        cur.execute(
            "SELECT DISTINCT trade_date FROM daily_market_summary ORDER BY trade_date"
        )
        known = [r[0] for r in cur.fetchall()]
    if not known:
        return {}

    ordered = sorted(set(known + dates))
    prev_map: dict[date, date] = {}
    for i, d in enumerate(ordered):
        if i > 0:
            prev_map[d] = ordered[i - 1]

    needed_prev = sorted({prev_map[d] for d in dates if d in prev_map})
    if not needed_prev:
        return {}

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT trade_date, symbol, close_price
            FROM daily_market_summary
            WHERE trade_date = ANY(%s) AND symbol = ANY(%s)
            """,
            (needed_prev, symbols),
        )
        closes = {(r[0], r[1]): float(r[2]) for r in cur.fetchall()}

    result: dict[tuple[date, str], float] = {}
    for d in dates:
        prev = prev_map.get(d)
        if not prev:
            continue
        for s in symbols:
            key = (prev, s)
            if key in closes:
                result[(d, s)] = closes[key]
    return result


def ingest_floorsheet(df: pl.DataFrame, *, replace_dates: bool = True) -> dict[str, int]:
    df = floorsheet_frame(df)
    if df.is_empty():
        return {"floorsheet": 0, "rollup": 0, "summary": 0}

    dates = df["trade_date"].unique().to_list()
    symbols = df["symbol"].unique().to_list()
    rollup = compute_daily_broker_rollup(df)

    conn = get_conn()
    try:
        if replace_dates:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM floorsheet WHERE trade_date = ANY(%s)", (dates,)
                )
        _copy_dataframe(conn, "floorsheet", df, FLOORSheet_COLUMNS)

        prev = fetch_prev_closes(conn, dates, symbols)
        intra = (
            df.sort(["trade_date", "symbol", "contract_id"])
            .group_by(["trade_date", "symbol"], maintain_order=True)
            .agg(close_price=pl.col("rate").last())
        )
        intra_map = {
            (r["trade_date"], r["symbol"]): float(r["close_price"])
            for r in intra.iter_rows(named=True)
        }
        ordered_dates = sorted(set(dates))
        for i, d in enumerate(ordered_dates):
            if i == 0:
                continue
            prev_d = ordered_dates[i - 1]
            for s in symbols:
                if (d, s) not in prev and (prev_d, s) in intra_map:
                    prev[(d, s)] = intra_map[(prev_d, s)]

        summary = compute_daily_market_summary(df, prev, meta=fetch_securities_meta(conn))
        _upsert_rollup(conn, rollup)
        _upsert_summary(conn, summary)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    return {
        "floorsheet": df.height,
        "rollup": rollup.height,
        "summary": summary.height,
    }


def ingest_csv(path: str) -> dict[str, int]:
    df = pl.read_csv(path, try_parse_dates=True)
    return ingest_floorsheet(df)


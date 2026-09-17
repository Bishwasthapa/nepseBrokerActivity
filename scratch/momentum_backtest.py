import sys
import os
import polars as pl
from datetime import date
sys.path.append(os.path.abspath('.'))
from src.db import get_conn
from src.screener import _compute_all_turnover_momentum, classify_momentum_status, load_summary
def fetch_trade_dates_local(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT DISTINCT trade_date FROM daily_market_summary ORDER BY trade_date ASC")
        return [r[0] for r in cur.fetchall()]
def run():
    conn = get_conn()
    dates = fetch_trade_dates_local(conn)
    start_date = date(2026, 8, 1)
    end_date = date(2026, 9, 17)
    
    test_dates = [d for d in dates if start_date <= d <= end_date]
    start_idx = dates.index(test_dates[0])
    
    all_needed_dates = dates[max(0, start_idx - 45):]
    
    df_all = load_summary(conn, all_needed_dates)
    
    combinations = [(3, 10), (5, 22), (10, 44)]
    results = []
    for d in test_dates:
        d_idx = dates.index(d)
        for short_w, base_w in combinations:
            if d_idx < base_w:
                continue
            needed_dates = dates[d_idx - base_w + 1 : d_idx + 1]
            df_slice = df_all.filter(pl.col("trade_date").is_in(needed_dates))
            joined, dominators = _compute_all_turnover_momentum(df_slice, short_window=short_w, base_window=base_w, rollup=None)
            phcl = joined.filter(pl.col("symbol") == "PHCL")
            if not phcl.is_empty():
                row = phcl.row(0, named=True)
                status = classify_momentum_status(
                    row["avg_rank_short"],
                    row["avg_rank_base"],
                    row["rank_drift"],
                    row["turnover_ratio"]
                )
                results.append({
                    "date": d,
                    "combo": f"{short_w}v{base_w}",
                    "status": status,
                    "ratio": round(row["turnover_ratio"], 2) if row["turnover_ratio"] else None,
                    "drift": row["rank_drift"],
                    "close": row["close"]
                })
    import pandas as pd
    res_df = pd.DataFrame(results)
    pivot_status = res_df.pivot(index='date', columns='combo', values='status')
    pivot_ratio = res_df.pivot(index='date', columns='combo', values='ratio')
    print("=== PHCL Status Over Time ===")
    pd.set_option('display.max_rows', 100)
    print(pivot_status)
    print("\n=== PHCL Turnover Ratio Over Time ===")
    print(pivot_ratio)
if __name__ == "__main__":
    run()

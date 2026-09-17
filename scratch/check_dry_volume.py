import sys
import polars as pl
from datetime import date
sys.path.append('.')
from src.db import get_conn
from src.screener import load_summary, fetch_trade_dates

conn = get_conn()
dates = fetch_trade_dates(conn)
needed = dates[-10:]
summary = load_summary(conn, needed)
summary = summary.with_columns(pl.col("total_turnover").cast(pl.Float64))

symbols = ["PMHPL", "KBL", "VLUCL", "KKHC", "HEIP"]

print("Recent Average Turnover (in NRS):")
for sym in symbols:
    df = summary.filter(pl.col("symbol") == sym)
    if not df.is_empty():
        avg_t = df["total_turnover"].mean()
        print(f"{sym}: {avg_t:,.2f} NRS (approx {avg_t/100000:,.2f} Lakhs)")

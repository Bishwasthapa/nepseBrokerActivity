import sys, os
from datetime import date
sys.path.append(os.path.abspath('.'))
from src.db import get_conn
from src.screener import screen_turnover_momentum, load_summary
import polars as pl
conn = get_conn()
with conn.cursor() as cur:
    cur.execute("SELECT DISTINCT trade_date FROM daily_market_summary ORDER BY trade_date ASC")
    dates = [r[0] for r in cur.fetchall()]
d_idx = dates.index(date(2026, 8, 21))
needed = dates[d_idx-22+1: d_idx+1]
df = load_summary(conn, needed)
gainers, losers = screen_turnover_momentum(df, 5, 22, None)
for idx, g in enumerate(gainers):
    print(f"{idx+1}. {g['symbol']} - {g['status']} - Ratio: {g['turnover_ratio']}")

import sys
from datetime import date
sys.path.append('.')
from src.db import get_conn
from src.screener import _compute_all_turnover_momentum, classify_momentum_status, load_summary

conn = get_conn()
with conn.cursor() as cur:
    cur.execute("SELECT DISTINCT trade_date FROM daily_market_summary ORDER BY trade_date ASC")
    dates = [r[0] for r in cur.fetchall()]

d = date(2026, 8, 18)
d_idx = dates.index(d)
needed = dates[d_idx - 22 + 1: d_idx + 1]
df = load_summary(conn, needed)

joined, _ = _compute_all_turnover_momentum(df, 5, 22, None)
phcl = joined.filter(__import__('polars').col('symbol') == 'PHCL')
if phcl.is_empty():
    print("PHCL NOT in joined df at all")
else:
    r = phcl.row(0, named=True)
    status = classify_momentum_status(r['avg_rank_short'], r['avg_rank_base'], r['rank_drift'], r['turnover_ratio'])
    print(f"ratio={r['turnover_ratio']:.2f}, drift={r['rank_drift']:.2f}, status={status}")
    print(f"Filter would pass (ratio>=1.25 AND drift>=5)? {r['turnover_ratio'] >= 1.25 and r['rank_drift'] >= 5}")

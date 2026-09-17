"""
Research: What signals were present for PHCL BEFORE the Aug 19 STEALTH_BUILDING flag?
Tests:
1. Turnover ratio acceleration (ratio trending up even if < 1.25)
2. 3v10 hyper-fast window
3. Broker persistence (same top buyer for N consecutive days)
4. Price compression (price flat while volume expands)
"""
import sys
import polars as pl
from datetime import date
sys.path.append('.')
from src.db import get_conn
from src.screener import load_summary, load_rollup, _compute_all_turnover_momentum, classify_momentum_status

conn = get_conn()
with conn.cursor() as cur:
    cur.execute("SELECT DISTINCT trade_date FROM daily_market_summary ORDER BY trade_date ASC")
    all_dates = [r[0] for r in cur.fetchall()]

start_idx = all_dates.index(date(2026, 7, 16))  # start from early July, 44 days before breakout
needed = all_dates[start_idx:]
df_all = load_summary(conn, needed)
rollup_all = load_rollup(conn, needed)

print("=" * 90)
print("PHCL PRE-BREAKOUT SIGNAL ANALYSIS")
print("=" * 90)

results = []
for d in [dt for dt in all_dates if date(2026, 7, 28) <= dt <= date(2026, 9, 15)]:
    d_idx = all_dates.index(d)
    
    # -- Signal 1: Turnover acceleration (5 consecutive days ratio trend)
    ratios = []
    for lookback in range(4, -1, -1):
        lb_idx = d_idx - lookback
        if lb_idx < 22:
            continue
        needed_slice = all_dates[lb_idx - 22 + 1: lb_idx + 1]
        df_slice = df_all.filter(pl.col("trade_date").is_in(needed_slice))
        joined, _ = _compute_all_turnover_momentum(df_slice, 5, 22, None)
        phcl = joined.filter(pl.col("symbol") == "PHCL")
        if not phcl.is_empty():
            ratios.append(float(phcl.row(0, named=True)["turnover_ratio"] or 0))
    
    accel = None
    if len(ratios) >= 3:
        # Is ratio consistently increasing over last 3 observations?
        accel = ratios[-1] - ratios[0]  # positive = accelerating
    
    # -- Signal 2: 3v10 fast window
    if d_idx >= 10:
        needed_fast = all_dates[d_idx - 10 + 1: d_idx + 1]
        df_fast = df_all.filter(pl.col("trade_date").is_in(needed_fast))
        joined_fast, _ = _compute_all_turnover_momentum(df_fast, 3, 10, None)
        phcl_fast = joined_fast.filter(pl.col("symbol") == "PHCL")
        fast_status = None
        fast_ratio = None
        if not phcl_fast.is_empty():
            r = phcl_fast.row(0, named=True)
            fast_ratio = round(r["turnover_ratio"] or 0, 2)
            fast_status = classify_momentum_status(r["avg_rank_short"], r["avg_rank_base"], r["rank_drift"], r["turnover_ratio"])
    else:
        fast_status = None
        fast_ratio = None
    
    # -- Signal 3: Broker persistence (same top buyer for last 3 days)
    last3 = all_dates[d_idx - 2: d_idx + 1]
    broker_streak = 0
    prev_top = None
    streak_broken = False
    for ld in last3:
        day_roll = rollup_all.filter(pl.col("trade_date") == ld).filter(pl.col("symbol") == "PHCL")
        if day_roll.is_empty():
            continue
        top = day_roll.sort("buy_qty", descending=True).row(0, named=True)["broker_id"]
        if prev_top is None:
            prev_top = top
            broker_streak = 1
        elif top == prev_top:
            broker_streak += 1
        else:
            streak_broken = True
            broker_streak = 1
            prev_top = top
    
    # -- Signal 4: Standard 5v22
    if d_idx >= 22:
        needed_std = all_dates[d_idx - 22 + 1: d_idx + 1]
        df_std = df_all.filter(pl.col("trade_date").is_in(needed_std))
        joined_std, _ = _compute_all_turnover_momentum(df_std, 5, 22, None)
        phcl_std = joined_std.filter(pl.col("symbol") == "PHCL")
        std_status = None
        std_ratio = None
        if not phcl_std.is_empty():
            r = phcl_std.row(0, named=True)
            std_ratio = round(r["turnover_ratio"] or 0, 2)
            std_status = classify_momentum_status(r["avg_rank_short"], r["avg_rank_base"], r["rank_drift"], r["turnover_ratio"])
    
    results.append({
        "date": d,
        "std_ratio": std_ratio,
        "std_status": std_status,
        "fast_ratio": fast_ratio,
        "fast_status": fast_status,
        "accel_5d": round(accel, 3) if accel is not None else None,
        "broker_streak": broker_streak,
    })

import pandas as pd
df = pd.DataFrame(results)
pd.set_option('display.max_rows', 60)
pd.set_option('display.width', 150)
print(df.to_string(index=False))

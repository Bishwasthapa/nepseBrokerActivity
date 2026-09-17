"""Optimizer for sweeping historical window combinations."""

from __future__ import annotations

import pandas as pd
from src.ml.features import build_dataset

def grid_search_windows(conn):
    """
    Evaluate different (Short, Base) combinations and rank them.
    """
    windows_to_test = [
        (3, 10),
        (5, 15),
        (5, 22),
        (10, 30),
        (10, 66)
    ]
    
    results = []
    
    for short_w, base_w in windows_to_test:
        df = build_dataset(conn, short_w, base_w)
        
        # We only care about moments when momentum is surging
        # (e.g. Turnover Ratio > 1.5 and Rank Drift > 0)
        signals = df[(df['turnover_ratio'] > 1.5) & (df['rank_drift'] > 0)]
        
        if len(signals) == 0:
            continue
            
        t5_win_rate = (signals['fwd_t5_ret'] > 0).mean() * 100
        t5_avg_ret = signals['fwd_t5_ret'].mean() * 100
        t20_win_rate = (signals['fwd_t20_ret'] > 0).mean() * 100
        t20_avg_ret = signals['fwd_t20_ret'].mean() * 100
        
        results.append({
            'Recent Window': short_w,
            'Baseline Window': base_w,
            'Signals Triggered': len(signals),
            'T+5 Win Rate (%)': t5_win_rate,
            'T+5 Avg Yield (%)': t5_avg_ret,
            'T+20 Win Rate (%)': t20_win_rate,
            'T+20 Avg Yield (%)': t20_avg_ret
        })
        
    res_df = pd.DataFrame(results).sort_values('T+5 Win Rate (%)', ascending=False)
    
    print("\n" + "="*50)
    print("MOMENTUM WINDOW OPTIMIZATION RESULTS")
    print("="*50)
    print(res_df.to_string(index=False))
    return res_df

if __name__ == "__main__":
    from src.db import get_conn
    conn = get_conn()
    try:
        grid_search_windows(conn)
    finally:
        conn.close()

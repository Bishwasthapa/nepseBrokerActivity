"""Machine Learning feature generation pipeline."""

from __future__ import annotations

import pandas as pd
import numpy as np

def build_dataset(conn, short_window: int = 5, base_window: int = 22) -> pd.DataFrame:
    """
    Build a feature matrix X and labels y for a given short/long window pair.
    
    Features:
    - Turnover Ratio (short avg / long avg)
    - Rank Drift (long rank - short rank)
    - Distance to VWAP
    
    Labels:
    - Forward T+5 return > 3% (Binary classification)
    - Forward T+20 return
    """
    query = """
    SELECT trade_date, symbol, close_price, total_turnover, turnover_rank, vwap
    FROM daily_market_summary
    ORDER BY symbol, trade_date
    """
    print(f"Loading market summary (W: {short_window}v{base_window})...")
    df = pd.read_sql(query, conn)
    df['trade_date'] = pd.to_datetime(df['trade_date'])
    
    # Calculate rolling averages
    print("Calculating rolling features...")
    df['short_turnover'] = df.groupby('symbol')['total_turnover'].transform(lambda x: x.rolling(short_window).mean())
    df['base_turnover'] = df.groupby('symbol')['total_turnover'].transform(lambda x: x.rolling(base_window).mean())
    
    df['short_rank'] = df.groupby('symbol')['turnover_rank'].transform(lambda x: x.rolling(short_window).mean())
    df['base_rank'] = df.groupby('symbol')['turnover_rank'].transform(lambda x: x.rolling(base_window).mean())
    
    # Feature: Turnover Ratio
    df['turnover_ratio'] = df['short_turnover'] / df['base_turnover']
    
    # Feature: Rank Drift (positive means it climbed the ranks)
    df['rank_drift'] = df['base_rank'] - df['short_rank']
    
    # Feature: Distance to VWAP (margin)
    df['vwap_dist_pct'] = (df['close_price'] - df['vwap']) / df['vwap']
    
    # Labels: Forward returns
    print("Calculating forward returns...")
    df['fwd_t5_ret'] = df.groupby('symbol')['close_price'].shift(-5) / df['close_price'] - 1
    df['fwd_t20_ret'] = df.groupby('symbol')['close_price'].shift(-20) / df['close_price'] - 1
    
    # Binary target: T+5 yield > 3%
    df['target_t5_up3'] = (df['fwd_t5_ret'] > 0.03).astype(int)
    
    # Drop rows where we don't have enough history for the base window
    df = df.dropna(subset=['turnover_ratio', 'rank_drift', 'fwd_t5_ret'])
    
    return df

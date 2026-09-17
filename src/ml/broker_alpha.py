"""
Institutional Broker Alpha & Accumulation/Distribution Intelligence Engine.
Analyzes multi-window broker net flows, cost basis, stealth accumulation patterns, 
and distribution/exit signals for NEPSE long positioning.
"""

from __future__ import annotations

import pandas as pd
import numpy as np

def analyze_broker_accumulation(conn, min_net_qty: int = 10000) -> pd.DataFrame:
    """
    Analyze broker accumulation and distribution patterns across symbols.
    Calculates net quantities over 5d, 22d, and 66d windows, estimated cost basis,
    and identifies stealth accumulation vs distribution exit patterns.
    """
    query = """
    SELECT 
        r.symbol,
        r.broker_id,
        SUM(CASE WHEN r.trade_date >= (SELECT MAX(trade_date) FROM daily_broker_rollup WHERE symbol = r.symbol) - INTERVAL '5 days' THEN r.buy_qty - r.sell_qty ELSE 0 END) AS net_5d,
        SUM(CASE WHEN r.trade_date >= (SELECT MAX(trade_date) FROM daily_broker_rollup WHERE symbol = r.symbol) - INTERVAL '22 days' THEN r.buy_qty - r.sell_qty ELSE 0 END) AS net_22d,
        SUM(CASE WHEN r.trade_date >= (SELECT MAX(trade_date) FROM daily_broker_rollup WHERE symbol = r.symbol) - INTERVAL '66 days' THEN r.buy_qty - r.sell_qty ELSE 0 END) AS net_66d,
        SUM(r.buy_amount) / NULLIF(SUM(r.buy_qty), 0) AS avg_buy_price,
        MAX(m.close_price) AS current_price
    FROM daily_broker_rollup r
    JOIN daily_market_summary m ON r.symbol = m.symbol AND r.trade_date = m.trade_date
    GROUP BY r.symbol, r.broker_id
    HAVING SUM(r.buy_qty) > 0
    """
    print("Loading multi-window broker rollup data...")
    df = pd.read_sql(query, conn)
    if df.empty:
        return df

    # Unrealized profit/loss margin for the broker's holdings
    df['unrealized_margin_pct'] = np.where(
        df['avg_buy_price'].notnull() & (df['avg_buy_price'] > 0),
        (df['current_price'] - df['avg_buy_price']) / df['avg_buy_price'] * 100,
        0.0
    )

    # Classification of Broker Action
    # 1. Stealth Accumulator: Net positive over 22d and 66d, active in recent 5d.
    # 2. Distributor / Dumping: Heavy net buyer over 66d but flipping negative in 5d/22d.
    # Enhanced NEPSE distribution & exit early warning logic:
    # DISTRIBUTION_WARNING: Previously accumulated over 66d, but 5d net is sharply negative (early exit sign)
    conditions = [
        (df['net_66d'] > min_net_qty) & (df['net_22d'] > 0) & (df['net_5d'] > 0),
        (df['net_66d'] > min_net_qty) & (df['net_5d'] < -(0.2 * df['net_66d'].abs())),
        (df['net_66d'] > min_net_qty) & ((df['net_22d'] < 0) | (df['net_5d'] < 0)),
        (df['net_66d'] < -min_net_qty) & (df['net_22d'] < 0)
    ]
    choices = ['STEALTH_ACCUMULATION', 'DISTRIBUTION_WARNING', 'DISTRIBUTION_EXIT', 'HEAVY_SELLING']
    df['broker_pattern'] = np.select(conditions, choices, default='NEUTRAL')

    return df

def get_top_broker_alpha_leaderboard(conn) -> pd.DataFrame:
    """
    Identify which brokers historically accumulate stocks that exhibit strong performance.
    """
    df = analyze_broker_accumulation(conn)
    if df.empty:
        return pd.DataFrame()

    # Aggregate by broker_id to see which broker has the most successful stealth accumulation patterns
    leaderboard = df[df['broker_pattern'] == 'STEALTH_ACCUMULATION'].groupby('broker_id').agg(
        stealth_accumulations=('symbol', 'count'),
        avg_unrealized_margin=('unrealized_margin_pct', 'mean'),
        total_accumulated_qty=('net_66d', 'sum')
    ).reset_index().sort_values(by=['stealth_accumulations', 'avg_unrealized_margin'], ascending=False)

    return leaderboard

if __name__ == "__main__":
    from src.db import get_conn
    conn = get_conn()
    try:
        print("--- TOP BROKER ALPHA LEADERBOARD ---")
        lb = get_top_broker_alpha_leaderboard(conn)
        print(lb.head(15).to_string(index=False))
    finally:
        conn.close()

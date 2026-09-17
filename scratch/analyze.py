import psycopg2
import pandas as pd
import numpy as np

DATABASE_URL = "postgresql://quant:quantpass@localhost:5432/nepse_analytics"

def get_db():
    return psycopg2.connect(DATABASE_URL)

def analyze_momentum_windows():
    print("Fetching market data...")
    conn = get_db()
    query = """
    SELECT trade_date, symbol, close_price, total_turnover
    FROM daily_market_summary
    ORDER BY symbol, trade_date
    """
    df = pd.read_sql(query, conn)
    df['trade_date'] = pd.to_datetime(df['trade_date'])
    
    # Pre-calculate future returns
    df['t5_ret'] = df.groupby('symbol')['close_price'].shift(-5) / df['close_price'] - 1
    df['t20_ret'] = df.groupby('symbol')['close_price'].shift(-20) / df['close_price'] - 1
    
    print("Evaluating Momentum Windows...")
    windows = [(3, 10), (5, 15), (5, 22), (10, 30), (10, 66)]
    results = []
    
    for short_w, long_w in windows:
        # Calculate moving averages of turnover
        df['short_ma'] = df.groupby('symbol')['total_turnover'].transform(lambda x: x.rolling(short_w).mean())
        df['long_ma'] = df.groupby('symbol')['total_turnover'].transform(lambda x: x.rolling(long_w).mean())
        
        df['turnover_ratio'] = df['short_ma'] / df['long_ma']
        
        # Signal: turnover ratio > 1.5
        signals = df[df['turnover_ratio'] > 1.5]
        
        t5_win = (signals['t5_ret'] > 0).mean() * 100
        t5_avg = signals['t5_ret'].mean() * 100
        t20_win = (signals['t20_ret'] > 0).mean() * 100
        t20_avg = signals['t20_ret'].mean() * 100
        
        results.append({
            'short_w': short_w,
            'long_w': long_w,
            'signals_count': len(signals),
            't5_win_rate': t5_win,
            't5_avg_ret': t5_avg,
            't20_win_rate': t20_win,
            't20_avg_ret': t20_avg
        })
    
    res_df = pd.DataFrame(results).sort_values('t20_avg_ret', ascending=False)
    print(res_df.to_string(index=False))
    return df

def analyze_best_stock(df):
    print("\nAnalyzing best stock with broker accumulation (Highest Historical Return after Accumulation)")
    conn = get_db()
    # Find broker accumulation events (e.g. top buyer for a stock over 22 days)
    # We can use screener_signals_history for stealth accumulation or momentum gainer
    query = """
    SELECT symbol, broker_id, trade_date, signal
    FROM screener_signals_history
    WHERE signal IN ('STEALTH_BUILDING', 'MOMENTUM_GAINER', 'SILENT_ACCUMULATION')
    """
    try:
        signals_df = pd.read_sql(query, conn)
        signals_df['trade_date'] = pd.to_datetime(signals_df['trade_date'])
        
        merged = pd.merge(signals_df, df[['trade_date', 'symbol', 't20_ret', 't5_ret']], on=['trade_date', 'symbol'])
        merged = merged.dropna(subset=['t20_ret'])
        
        if len(merged) == 0:
            print("No signals have T+20 data yet.")
            merged = pd.merge(signals_df, df[['trade_date', 'symbol', 't20_ret', 't5_ret']], on=['trade_date', 'symbol'])
            merged = merged.dropna(subset=['t5_ret'])
            if len(merged) == 0:
                print("No signals have T+5 data either.")
                return
            best_stock = merged.groupby('symbol')['t5_ret'].mean().reset_index()
            best_stock = best_stock.sort_values('t5_ret', ascending=False).head(5)
            print("\nTop 5 Stocks Yielding Best after Accumulation Signal (Avg T+5 Return %):")
            best_stock['t5_ret'] *= 100
            print(best_stock.to_string(index=False))
            
            best_broker = merged.groupby('broker_id')['t5_ret'].mean().reset_index()
            best_broker = best_broker[merged.groupby('broker_id')['t5_ret'].count().values > 1]
            best_broker = best_broker.sort_values('t5_ret', ascending=False).head(5)
            print("\nTop 5 Brokers by Post-Accumulation Yield (Avg T+5 Return %):")
            best_broker['t5_ret'] *= 100
            print(best_broker.to_string(index=False))
            return
        
        best_stock = merged.groupby('symbol')['t20_ret'].mean().reset_index()
        print("\nTop 5 Stocks Yielding Best after Accumulation Signal (Avg T+20 Return %):")
        best_stock['t20_ret'] *= 100
        print(best_stock.to_string(index=False))
        
        best_broker = merged.groupby('broker_id')['t20_ret'].mean().reset_index()
        best_broker = best_broker[merged.groupby('broker_id')['t20_ret'].count().values > 5] # At least 5 signals
        best_broker = best_broker.sort_values('t20_ret', ascending=False).head(5)
        print("\nTop 5 Brokers by Post-Accumulation Yield (Avg T+20 Return %):")
        best_broker['t20_ret'] *= 100
        print(best_broker.to_string(index=False))
        
    except Exception as e:
        print("Error analyzing signals:", e)

if __name__ == "__main__":
    df = analyze_momentum_windows()
    analyze_best_stock(df)

import sys
import psycopg2
sys.path.append('/app')
from datetime import timedelta
from src.screener import fetch_trade_dates, load_summary, load_rollup, screen_prebreakout

def run():
    db = psycopg2.connect("postgresql://quant:quantpass@db:5432/nepse_analytics")
    dates = fetch_trade_dates(db)
    
    # Pre-load ALL data for the required history window (max last 120 days to be safe)
    required_dates = dates[-110:]
    full_summary = load_summary(db, required_dates)
    full_rollup = load_rollup(db, required_dates)
    
    target_dates = dates[-40:]
    
    symbol = 'SHEL'
    print(f"Analyzing {symbol} over the last 40 trading sessions...")
    print(f"{'Date':<10} | {'Close':<6} | {'Status (Radar)':<24} | {'Score':<5} | {'F-Ratio':<7} | {'S-Ratio':<7} | {'Streak':<6}")
    print("-" * 80)
    
    for d in target_dates:
        idx = required_dates.index(d)
        if idx < 60: continue
        
        hist_dates = required_dates[idx-60:idx+1]
        
        # Filter the pre-loaded dataframes
        summary_df = full_summary.filter(full_summary['trade_date'].is_in(hist_dates))
        rollup_df = full_rollup.filter(full_rollup['trade_date'].is_in(hist_dates))
        
        res = screen_prebreakout(summary_df, rollup_df, 3, 10, 5, 22, 5000000)
        
        acc = next((x for x in res['candidates'] if x['symbol'] == symbol), None)
        dist = next((x for x in res['dist_candidates'] if x['symbol'] == symbol), None)
        
        cur = db.cursor()
        cur.execute("SELECT close_price FROM daily_market_summary WHERE trade_date = %s AND symbol = %s", (d, symbol))
        row = cur.fetchone()
        close_px = row[0] if row else 0
        cur.close()
        
        if acc:
            print(f"{str(d):<10} | {close_px:<6.0f} | {acc['conviction']:<24} | {acc['score']:<5} | {acc['fast_ratio']:<7.2f} | {acc['std_ratio']:<7.2f} | {acc['broker_streak']:<6}")
        elif dist:
            print(f"{str(d):<10} | {close_px:<6.0f} | {dist['conviction']:<24} | {dist['score']:<5} | {dist['fast_ratio']:<7.2f} | {dist['std_ratio']:<7.2f} | {dist['dist_streak']:<6}")
        else:
            print(f"{str(d):<10} | {close_px:<6.0f} | {'-':<24} | {'-':<5} | {'-':<7} | {'-':<7} | {'-':<6}")

run()

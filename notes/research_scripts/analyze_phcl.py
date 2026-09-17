from src.db import get_conn
import pandas as pd

def analyze():
    conn = get_conn()
    
    query = """
    SELECT 
        d.trade_date,
        d.close_price,
        d.price_change_pct,
        d.total_qty,
        d.total_turnover
    FROM daily_market_summary d
    WHERE d.symbol = 'PHCL'
    ORDER BY d.trade_date ASC
    """
    df = pd.read_sql(query, conn)
    
    rollup_query = """
    SELECT 
        trade_date,
        broker_id,
        buy_qty,
        sell_qty,
        matched_qty
    FROM daily_broker_rollup
    WHERE symbol = 'PHCL'
    ORDER BY trade_date ASC
    """
    rollup = pd.read_sql(rollup_query, conn)
    
    results = []
    for _, row in df.iterrows():
        date = row['trade_date']
        day_roll = rollup[rollup['trade_date'] == date]
        
        top3_buy = day_roll.nlargest(3, 'buy_qty')['buy_qty'].sum()
        top3_sell = day_roll.nlargest(3, 'sell_qty')['sell_qty'].sum()
        matched = day_roll['matched_qty'].sum()
        
        total_qty = row['total_qty']
        
        top3_buy_pct = (top3_buy / total_qty * 100) if total_qty > 0 else 0
        top3_sell_pct = (top3_sell / total_qty * 100) if total_qty > 0 else 0
        match_pct = (matched / total_qty * 100) if total_qty > 0 else 0
        
        abs_ratio = (top3_buy / (top3_sell + 1e-5)) if top3_sell > 0 else 0
        
        results.append({
            'date': date,
            'close': row['close_price'],
            'change': row['price_change_pct'],
            'qty': total_qty,
            'top3_buy': top3_buy_pct,
            'top3_sell': top3_sell_pct,
            'abs_ratio': abs_ratio,
            'match_pct': match_pct
        })
        
    res_df = pd.DataFrame(results)
    res_df['sma20_qty'] = res_df['qty'].rolling(20).mean().shift(1)
    res_df['rvol'] = res_df['qty'] / res_df['sma20_qty']
    
    pd.set_option('display.max_rows', 100)
    pd.set_option('display.max_columns', 20)
    pd.set_option('display.width', 200)
    
    print(res_df.tail(60).to_string(index=False))

if __name__ == "__main__":
    analyze()

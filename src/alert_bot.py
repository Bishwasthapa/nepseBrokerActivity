import os
import json
import urllib.request
from typing import List, Dict
from src.screener import market_overview

def send_discord_alert(webhook_url: str, content: str):
    """Sends a markdown message to the Discord webhook."""
    if not webhook_url or webhook_url.startswith("https://discord.com/api/webhooks/your-webhook-id"):
        print("[-] Invalid or missing DISCORD_WEBHOOK_URL. Skipping alert.")
        return
        
    data = {"content": content}
    req = urllib.request.Request(webhook_url, json.dumps(data).encode('utf-8'), headers={'Content-Type': 'application/json'})
    
    try:
        response = urllib.request.urlopen(req)
        if response.status in (200, 204):
            print("[+] Discord alert sent successfully!")
        else:
            print(f"[-] Failed to send alert. Status code: {response.status}")
    except Exception as e:
        print(f"[-] Error sending Discord alert: {e}")

def run_daily_alerts():
    """Scans the market and triggers alerts for top setups."""
    print("[*] Running daily alert scan...")
    market_data = market_overview()
    
    # Filter for Strong Buys
    strong_buys = [item for item in market_data if item.get('verdict') == 'Strong Buy']
    
    # Sort by Top Accumulator Net Qty (descending) to get the strongest conviction setups
    # Note: net_qty might be null, so we default to 0
    strong_buys.sort(
        key=lambda x: x.get('top_accum_net') if x.get('top_accum_net') is not None else 0, 
        reverse=True
    )
    
    if not strong_buys:
        print("[-] No 'Strong Buy' setups found today.")
        return
        
    # Pick top 5 setups
    top_setups = strong_buys[:5]
    
    # Format message
    lines = ["🚀 **NEPSE Smart Money Alerts - Daily Top Setups** 🚀\n"]
    for setup in top_setups:
        symbol = setup.get('symbol', 'UNKNOWN')
        price = setup.get('close', 0.0)
        broker = setup.get('top_accum_id', 'N/A')
        net_qty = setup.get('top_accum_net', 0)
        wash = setup.get('wash_pct', 0.0)
        
        msg = (
            f"🔹 **{symbol}** @ NRS {price}\n"
            f"   - **Top Accumulator**: Broker {broker} (Net +{net_qty:,} shares)\n"
            f"   - **Wash %**: {wash:.1f}%\n"
        )
        lines.append(msg)
        
    content = "\n".join(lines)
    
    from dotenv import load_dotenv
    load_dotenv()
    webhook_url = os.environ.get("DISCORD_WEBHOOK_URL")
    
    send_discord_alert(webhook_url, content)
    
if __name__ == "__main__":
    run_daily_alerts()

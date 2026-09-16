import os
import requests

DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL")

def send_alert(message: str, title: str = "Smart Money Alert"):
    """
    Sends an alert to Discord via Webhook.
    """
    if not DISCORD_WEBHOOK_URL:
        print("[Notifier] DISCORD_WEBHOOK_URL not set. Skipping Discord alert.")
        print(f"--- {title} ---\n{message}\n-------------------")
        return False
        
    data = {
        "content": "",
        "embeds": [
            {
                "title": title,
                "description": message,
                "color": 3447003, # Blue
            }
        ]
    }
    
    try:
        response = requests.post(DISCORD_WEBHOOK_URL, json=data)
        response.raise_for_status()
        return True
    except Exception as e:
        print(f"Failed to send Discord alert: {e}")
        return False

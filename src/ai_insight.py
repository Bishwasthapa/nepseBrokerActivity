import os
from google import genai

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

if not GEMINI_API_KEY:
    # Just a placeholder for local running without erroring on import
    client = None
else:
    client = genai.Client(api_key=GEMINI_API_KEY)

def generate_broker_alert(stock_symbol, broker_id, broker_name, net_qty, stock_data):
    """
    Generates a response from the Google Gemini model for smart money alerts.
    """
    if not client:
        return f"[AI Skipped] GEMINI_API_KEY not set. Alert for {stock_symbol} by Broker {broker_id}."
        
    prompt = f"""
    You are an expert financial analyst for the Nepal stock market (NEPSE).
    
    A "Smart Money" absorption event has been detected:
    Broker {broker_id} ({broker_name}) has aggressively accumulated a net of {net_qty} shares of {stock_symbol}.
    
    Here is the recent market data for {stock_symbol}:
    {stock_data}
    
    Write a clear, 2-paragraph alert synthesizing this specific broker's accumulation with the overall market context.
    If the Deterministic Scoring Engine Verdict is 'Avoid / Exit' or 'Hold', explain the contradiction: why this broker might be making a risky move against the broader market flow (e.g. catching a falling knife, absorbing distribution from others).
    If the Verdict is 'Buy' or 'Strong Buy', reinforce how this broker's buying aligns with the broader bullish momentum.
    Keep it professional, highly analytical, and actionable. Do not simply regurgitate the raw data.
    """
    
    try:
        response = client.models.generate_content(
            model="gemini-3.5-flash",
            contents=prompt
        )
        return response.text
    except Exception as e:
        return f"An error occurred with the LLM API: {e}"

import os
import json
import requests
import kalshi_python
from datetime import datetime

# --- CONFIGURATION ---
HISTORY_FILE = "balance_history.json"
# If this is your first run, we use this as the 'start' value if file is missing
INITIAL_DEPOSIT = 236.68 

def send_discord_report(current_val, change_val, change_pct, total_cash, total_positions):
    webhook_url = os.getenv("DISCORD_WEBHOOK_URL")
    if not webhook_url: return

    # Color code: Green for profit, Red for loss
    color = 3066993 if change_val >= 0 else 15158332
    emoji = "📈" if change_val >= 0 else "📉"
    
    embed = {
        "title": f"{emoji} Daily Account Report",
        "color": color,
        "fields": [
            {"name": "Total Value", "value": f"**${current_val:,.2f}**", "inline": True},
            {"name": "24h Change", "value": f"${change_val:,.2f} ({change_pct:+.2f}%)", "inline": True},
            {"name": "\u200b", "value": "\u200b", "inline": False}, # Spacer
            {"name": "Cash Hand", "value": f"${total_cash:,.2f}", "inline": True},
            {"name": "Active Positions", "value": f"${total_positions:,.2f}", "inline": True}
        ],
        "footer": {"text": f"Date: {datetime.now().strftime('%Y-%m-%d')}"}
    }

    payload = {"embeds": [embed]}
    try:
        requests.post(webhook_url, json=payload)
    except Exception as e:
        print(f"Discord Error: {e}")

def main():
    # 1. Setup API
    api_key_id = os.getenv("KALSHI_KEY")
    private_key_pem = os.getenv("KALSHI_PRIVATE_KEY")
    
    config = kalshi_python.Configuration(host="https://api.elections.kalshi.com/trade-api/v2")
    config.api_key_id = api_key_id
    config.private_key_pem = private_key_pem
    
    try:
        api_client = kalshi_python.ApiClient(config)
        portfolio_api = kalshi_python.PortfolioApi(api_client)
        
        # 2. Get Current Data
        # Kalshi returns values in CENTS, so we divide by 100
        balance_data = portfolio_api.get_balance()
        cash = balance_data.balance / 100
        # In newer API versions, 'portfolio_value' might be separate or calculated
        # If 'portfolio_value' is missing, check documentation or sum positions manually
        # Standard response usually includes portfolio_value in cents
        positions_val = getattr(balance_data, 'portfolio_value', 0) / 100 
        
        # Note: If portfolio_value is 0 but you have positions, you might need to
        # fetch get_positions() and sum (qty * current_price) manually.
        # For now, we assume the API returns the total correctly.
        
        total_value = cash + positions_val # Sometimes balance includes collateral, check your dashboard
        # Kalshi 'balance' is typically "Available to trade". 
        # Total Equity = Balance + Portfolio Value.
        
    except Exception as e:
        print(f"API Error: {e}")
        return

    # 3. Load History (Memory)
    last_value = INITIAL_DEPOSIT
    try:
        if os.path.exists(HISTORY_FILE):
            with open(HISTORY_FILE, "r") as f:
                data = json.load(f)
                last_value = data.get("last_value", INITIAL_DEPOSIT)
    except Exception as e:
        print(f"History Read Error: {e}")

    # 4. Calculate PnL
    change_val = total_value - last_value
    change_pct = (change_val / last_value) * 100 if last_value != 0 else 0

    print(f"Today: ${total_value} | Yesterday: ${last_value} | Change: {change_pct}%")

    # 5. Send Report
    send_discord_report(total_value, change_val, change_pct, cash, positions_val)

    # 6. Save Today's Value for Tomorrow
    with open(HISTORY_FILE, "w") as f:
        json.dump({"last_value": total_value, "date": str(datetime.now())}, f)

if __name__ == "__main__":
    main()

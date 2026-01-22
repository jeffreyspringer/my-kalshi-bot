import os
import json
import time
import base64
import requests
from datetime import datetime
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.serialization import load_pem_private_key

HISTORY_FILE = "balance_history.json"
HOST = "https://api.elections.kalshi.com"

def get_raw_balance():
    key_id = os.getenv("KALSHI_KEY", "").strip()
    key_str = os.getenv("KALSHI_PRIVATE_KEY", "").strip().replace('\\n', '\n')
    if "-----BEGIN" not in key_str:
        key_str = f"-----BEGIN RSA PRIVATE KEY-----\n{key_str}\n-----END RSA PRIVATE KEY-----"
    
    private_key = load_pem_private_key(key_str.encode(), password=None)
    timestamp = str(int(time.time() * 1000))
    path = "/trade-api/v2/portfolio/balance"
    msg = f"{timestamp}GET{path}"
    
    signature = private_key.sign(
        msg.encode('utf-8'),
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.MAX_LENGTH),
        hashes.SHA256()
    )
    headers = {
        "KALSHI-ACCESS-KEY": key_id,
        "KALSHI-ACCESS-SIGNATURE": base64.b64encode(signature).decode('utf-8'),
        "KALSHI-ACCESS-TIMESTAMP": timestamp
    }
    return requests.get(f"{HOST}{path}", headers=headers).json().get("balance", 0)

def send_discord_report(curr, change, pct):
    webhook = os.getenv("DISCORD_REPORT_WEBHOOK_URL") or os.getenv("DISCORD_WEBHOOK_URL")
    if not webhook: return
    
    emoji = "📈" if change >= 0 else "📉"
    color = 3066993 if change >= 0 else 15158332
    embed = {
        "title": f"{emoji} Daily Account Report",
        "color": color,
        "fields": [
            {"name": "Total Value", "value": f"**${curr/100:,.2f}**", "inline": True},
            {"name": "24h Change", "value": f"${change/100:,.2f} ({pct:+.2f}%)", "inline": True}
        ]
    }
    requests.post(webhook, json={"embeds": [embed]})

def main():
    try:
        total_balance = get_raw_balance() # In cents
    except Exception as e:
        print(f"Error fetching balance: {e}")
        return

    last_val = 10000 # Default to $100.00 (in cents)
    if os.path.exists(HISTORY_FILE):
        with open(HISTORY_FILE, "r") as f:
            last_val = json.load(f).get("last_value", 10000)

    change = total_balance - last_val
    pct = (change / last_val) * 100 if last_val != 0 else 0
    
    print(f"Balance: {total_balance}¢ | Change: {pct}%")
    send_discord_report(total_balance, change, pct)
    
    with open(HISTORY_FILE, "w") as f:
        json.dump({"last_value": total_balance, "date": str(datetime.now())}, f)

if __name__ == "__main__":
    main()

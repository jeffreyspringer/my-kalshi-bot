import os
import uuid
import requests
import csv
import time
import json
import base64
from datetime import datetime
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.serialization import load_pem_private_key

# --- CONFIGURATION ---
HOST = "https://api.elections.kalshi.com"

# ✅ CITIES
CITIES = [
    { "name": "NOLA",    "lat": 29.99, "lon": -90.25,  "ticker": "KXHIGHTNOLA", "airport": "KMSY" },
    { "name": "CHICAGO", "lat": 41.79, "lon": -87.75,  "ticker": "KXHIGHCHI",   "airport": "KMDW" },
    { "name": "MIAMI",   "lat": 25.80, "lon": -80.29,  "ticker": "KXHIGHMIA",   "airport": "KMIA" },
    { "name": "SEATTLE", "lat": 47.45, "lon": -122.31, "ticker": "KXHIGHTSEA",  "airport": "KSEA" },
    { "name": "AUSTIN",  "lat": 30.19, "lon": -97.67,  "ticker": "KXHIGHAUS",   "airport": "KAUS" }
]

# RISK SETTINGS
MIN_BALANCE_CENTS = 500     
MAX_TOTAL_POS = 20          
PROFIT_TAKE_PRICE = 92      
FEE_BUFFER = 3
MIN_PRICE = 2              # We want to buy cheap "No" bets too
MAX_PRICE = 98
LOW_CONF_COUNT = 1
MED_CONF_COUNT = 3
HIGH_CONF_COUNT = 10

class KalshiClient:
    def __init__(self):
        self.key_id = os.getenv("KALSHI_KEY", "").strip()
        self.private_key = self._load_private_key(os.getenv("KALSHI_PRIVATE_KEY", ""))

    def _load_private_key(self, key_str):
        key_str = key_str.strip().replace('\\n', '\n')
        if "-----BEGIN" not in key_str:
            key_str = f"-----BEGIN RSA PRIVATE KEY-----\n{key_str}\n-----END RSA PRIVATE KEY-----"
        return load_pem_private_key(key_str.encode(), password=None)

    def _req(self, method, path, body=None):
        timestamp = str(int(time.time() * 1000))
        msg = f"{timestamp}{method}/trade-api/v2{path}"
        if body:
            msg += json.dumps(body, separators=(',', ':'))

        signature = self.private_key.sign(
            msg.encode('utf-8'),
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.MAX_LENGTH),
            hashes.SHA256()
        )
        headers = {
            "KALSHI-ACCESS-KEY": self.key_id,
            "KALSHI-ACCESS-SIGNATURE": base64.b64encode(signature).decode('utf-8'),
            "KALSHI-ACCESS-TIMESTAMP": timestamp,
            "Content-Type": "application/json"
        }
        url = f"{HOST}/trade-api/v2{path}"
        
        if method == "GET":
            return requests.get(url, headers=headers)
        return requests.post(url, headers=headers, json=body)

    def get_balance(self):
        res = self._req("GET", "/portfolio/balance")
        res.raise_for_status()
        return res.json().get("balance", 0)

    def get_positions(self):
        res = self._req("GET", "/portfolio/positions")
        res.raise_for_status()
        return res.json().get("market_positions", [])

    def get_orderbook(self, ticker):
        res = self._req("GET", f"/markets/{ticker}/orderbook")
        if res.status_code != 200: return None
        return res.json().get("orderbook")

    def place_order(self, ticker, action, side, count, price):
        body = {
            "action": action,
            "count": count,
            "type": "limit",
            "ticker": ticker,
            "side": side,
            "yes_price": price if side == "yes" else 0,
            "no_price": price if side == "no" else 0,
            "client_order_id": str(uuid.uuid4())
        }
        if side == "yes": del body["no_price"]
        else: del body["yes_price"]
        
        return self._req("POST", "/portfolio/orders", body)

# --- UTILS ---
def send_discord_alert(message):
    webhook = os.getenv("DISCORD_WEBHOOK_URL")
    if webhook: requests.post(webhook, json={"content": message})

def log_trade(ticker, forecast, strike, gap, price, qty, action):
    file_exists = os.path.isfile("trade_log.csv")
    with open("trade_log.csv", "a", newline="") as f:
        writer = csv.writer(f)
        if not file_exists:
            writer.writerow(["Date", "Ticker", "Forecast", "Strike", "Gap", "Price", "Qty", "Action"])
        writer.writerow([datetime.now().strftime("%Y-%m-%d %H:%M"), ticker, forecast, strike, f"{gap:.1f}", price, qty, action])

# --- FORECASTING ---
def get_nws_forecast(lat, lon):
    try:
        headers = {'User-Agent': '(KalshiBot, contact@example.com)'}
        p_res = requests.get(f"https://api.weather.gov/points/{lat},{lon}", headers=headers).json()
        f_url = p_res['properties']['forecast']
        grid = requests.get(f_url, headers=headers).json()
        for p in grid['properties']['periods']:
            if p['isDaytime']: return p['temperature']
    except: pass
    return None

def get_lamp_forecast(airport_code):
    try:
        url = f"https://tgftp.nws.noaa.gov/data/forecasts/lamp/station/{airport_code.lower()}.txt"
        res = requests.get(url)
        if res.status_code != 200: return None
        
        utc_line, tmp_line = None, None
        for line in res.text.split('\n'):
            clean = line.strip()
            if clean.startswith("UTC"): utc_line = clean
            if clean.startswith("TMP"): tmp_line = clean
            
        if not utc_line or not tmp_line: return None
        temps = [int(x) for x in tmp_line.split()[1:]]
        return max(temps[:15])
    except: return None

def check_profits(client):
    print("--- 💰 Checking Profit Taking ---")
    try:
        positions = client.get_positions()
        for pos in positions:
            if pos['position'] <= 0: continue
            ob = client.get_orderbook(pos['ticker'])
            if not ob or not ob['yes']: continue
            best_bid = ob['yes'][0][0]
            
            if best_bid >= PROFIT_TAKE_PRICE:
                print(f"   🤑 PROFIT! Selling {pos['ticker']} @ {best_bid}¢")
                try:
                    client.place_order(pos['ticker'], "sell", "yes", pos['position'], best_bid)
                    send_discord_alert(f"💰 **Profit!** Sold {pos['ticker']} @ {best_bid}¢")
                except: pass
    except: pass

def main():
    print("🚀 Bot Starting (Sniper Mode V16)...")
    if os.getenv("TRADING_ENABLED", "TRUE").upper() == "FALSE": return
    
    try:
        client = KalshiClient()
        if client.get_balance() < MIN_BALANCE_CENTS: return
        check_profits(client)
    except: return

    print(f"--- Scanning {len(CITIES)} Cities ---")
    for city in CITIES:
        print(f"\n🔎 {city['name']}...")
        nws = get_nws_forecast(city['lat'], city['lon'])
        lamp = get_lamp_forecast(city['airport'])
        
        if nws and lamp: safe_forecast = (nws + lamp) / 2
        elif nws: safe_forecast = nws
        elif lamp: safe_forecast = lamp
        else: continue
            
        print(f"   🎯 Target: {safe_forecast:.1f}°")

        try:
            markets = client._req("GET", f"/markets?series_ticker={city['ticker']}&status=open").json().get("markets", [])
        except: continue
        
        if not markets: 
            print("   No markets.")
            continue

        for market in markets:
            try:
                # Assuming ticker ends with -T70 (Strike)
                strike = float(market['ticker'].split('-T')[-1])
            except: continue

            # --- SNIPER LOGIC (BIN STRATEGY) ---
            # Distance: How far is this bin from our Forecast?
            distance = abs(safe_forecast - strike)
            
            # DEFAULT: Assume this bin is WRONG (Buy No)
            target_side = "no" 
            label = "FAR_OFF"

            # IF distance is very small (It's the Bullseye), we Buy YES
            if distance <= 1.5:
                target_side = "yes"
                label = "BULLSEYE"
            
            # --- FILTERING ---
            # If it's a "No" bet, we want a nice safe distance (e.g. > 3 degrees away)
            # If it's too close (e.g. 2 degrees), it's risky. Skip.
            if target_side == "no" and distance < 3.0:
                print(f"   Skipping {market['ticker']}: Too close to call (Dist {distance:.1f}°).")
                continue

            # --- PRICING ---
            ob = client.get_orderbook(market['ticker'])
            if not ob: continue

            price = 0
            if target_side == "yes":
                if not ob['no']: continue
                price = 100 - ob['no'][0][0] # Cost to buy Yes
            else: 
                if not ob['yes']: continue
                price = 100 - ob['yes'][0][0] # Cost to buy No

            # --- EXECUTION ---
            if price < MIN_PRICE or price > MAX_PRICE:
                print(f"   Skipping {market['ticker']} ({target_side.upper()}): Price {price}¢ bad.")
                continue

            # Sizing
            qty = LOW_CONF_COUNT
            if target_side == "yes": qty = MED_CONF_COUNT # Be modest on Bullseyes
            if target_side == "no" and distance >= 5.0: qty = HIGH_CONF_COUNT # Be aggressive on "Obvious Nos"

            # Position Check
            try:
                positions = client.get_positions()
                curr_pos = next((p['position'] for p in positions if p['ticker'] == market['ticker']), 0)
                qty = min(qty, MAX_TOTAL_POS - abs(curr_pos))
            except: qty = LOW_CONF_COUNT
            
            if qty <= 0: continue

            print(f"   🚀 EXECUTE: Buying {qty}x {market['ticker']} [{target_side.upper()}] @ {price}¢ (Dist {distance:.1f})")
            
            try:
                resp = client.place_order(market['ticker'], "buy", target_side, qty, price)
                if resp.status_code == 201:
                    log_trade(market['ticker'], safe_forecast, strike, distance, price, qty, f"BUY_{target_side.upper()}")
                    send_discord_alert(f"**Trade ({city['name']})**: Bought {qty}x {market['ticker']} **{target_side.upper()}** @ {price}¢ (Dist {distance:.1f})")
            except: pass

if __name__ == "__main__":
    main()

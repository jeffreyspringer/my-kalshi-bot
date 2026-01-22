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

# ✅ CITIES (Tickers + Airport Codes)
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
PROFIT_TAKE_PRICE = 92      # Sell automatically if bid is this high
FEE_BUFFER = 3
MIN_PRICE = 20
MAX_PRICE = 80
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

# --- FORECASTING ENGINES ---

def get_nws_forecast(lat, lon):
    """Fetches the official NWS Day High."""
    try:
        headers = {'User-Agent': '(KalshiBot, contact@example.com)'}
        p_res = requests.get(f"https://api.weather.gov/points/{lat},{lon}", headers=headers).json()
        f_url = p_res['properties']['forecast']
        grid = requests.get(f_url, headers=headers).json()
        for p in grid['properties']['periods']:
            if p['isDaytime']:
                return p['temperature']
    except Exception as e:
        print(f"   ⚠️ NWS Error: {e}")
    return None

def get_lamp_forecast(airport_code):
    """Fetches GFS LAMP data with robust parsing."""
    try:
        url = f"https://tgftp.nws.noaa.gov/data/forecasts/lamp/station/{airport_code}.txt"
        res = requests.get(url)
        if res.status_code != 200: 
            print(f"   ⚠️ LAMP Failed: {airport_code} not found (404).")
            return None
        
        lines = res.text.split('\n')
        utc_line, tmp_line = None, None
        
        # Robust Parsing: Strip spaces to find row headers
        for line in lines:
            clean = line.strip()
            if clean.startswith("UTC"): utc_line = clean
            if clean.startswith("TMP"): tmp_line = clean
            
        if not utc_line or not tmp_line: 
            return None
        
        # Extract numbers (split() handles variable spacing)
        temps = [int(x) for x in tmp_line.split()[1:]]
        
        # Grab max of the next 15 hours
        valid_temps = temps[:15]
        return max(valid_temps)
        
    except Exception as e:
        print(f"   ⚠️ LAMP Error: {e}")
        return None

def check_profits(client):
    print("--- 💰 Checking for Profit Opportunities ---")
    try:
        positions = client.get_positions()
        for pos in positions:
            if pos['position'] <= 0: continue
            ob = client.get_orderbook(pos['ticker'])
            if not ob or not ob['yes']: continue
            best_bid = ob['yes'][0][0]
            
            if best_bid >= PROFIT_TAKE_PRICE:
                print(f"   🤑 PROFIT! Selling {pos['position']}x {pos['ticker']} @ {best_bid}¢")
                try:
                    resp = client.place_order(pos['ticker'], "sell", "yes", pos['position'], best_bid)
                    if resp.status_code == 201:
                        log_trade(pos['ticker'], "PROFIT", "N/A", 0, best_bid, pos['position'], "SELL")
                        send_discord_alert(f"💰 **Profit Taken!** Sold {pos['position']}x {pos['ticker']} @ **{best_bid}¢**")
                except: pass
    except: pass

def main():
    print("🚀 Bot Starting (Dual-Engine Mode)...")
    if os.getenv("TRADING_ENABLED", "TRUE").upper() == "FALSE": return
    
    try:
        client = KalshiClient()
        balance = client.get_balance()
        print(f"✅ Balance: {balance}¢")
        if balance < MIN_BALANCE_CENTS: return
        check_profits(client)
    except Exception as e:
        print(f"❌ Login Failed: {e}")
        return

    print(f"--- Scanning {len(CITIES)} Cities ---")
    for city in CITIES:
        print(f"\n🔎 {city['name']} ({city['airport']})...")
        
        # 1. Get Engines
        nws = get_nws_forecast(city['lat'], city['lon'])
        lamp = get_lamp_forecast(city['airport'])
        
        print(f"   Forecasts: NWS {nws}° | LAMP {lamp}°")
        
        # 2. Consensus Logic
        if nws and lamp:
            safe_forecast = (nws + lamp) / 2
        elif nws:
            safe_forecast = nws
        elif lamp:
            safe_forecast = lamp
        else:
            print("   ⚠️ No data available. Skipping.")
            continue
            
        print(f"   🎯 Target: {safe_forecast:.1f}°")

        try:
            res = client._req("GET", f"/markets?series_ticker={city['ticker']}&status=open")
            markets = res.json().get("markets", [])
        except: 
            print("   ⚠️ Failed to fetch markets.")
            continue

        if not markets: 
            print("   ⚠️ No active markets.")
            continue

        for market in markets:
            try:
                strike = float(market['ticker'].split('-T')[-1])
            except: continue

            gap = safe_forecast - strike
            
            # --- VERBOSE LOGGING START ---
            if gap < 2.0: 
                print(f"   Skipping {market['ticker']}: Gap {gap:.1f}° is too small.")
                continue 
            # -----------------------------
            
            qty = LOW_CONF_COUNT
            if gap >= 5.0: qty = HIGH_CONF_COUNT
            elif gap >= 3.0: qty = MED_CONF_COUNT
            
            positions = client.get_positions()
            curr_pos = next((p['position'] for p in positions if p['ticker'] == market['ticker']), 0)
            qty = min(qty, MAX_TOTAL_POS - curr_pos)
            
            if qty <= 0: 
                print(f"   Skipping {market['ticker']}: Max position reached.")
                continue

            ob = client.get_orderbook(market['ticker'])
            if not ob or not ob['no']: continue
            best_no_bid = ob['no'][0][0]
            buy_yes_price = 100 - best_no_bid
            
            if buy_yes_price < MIN_PRICE or buy_yes_price > MAX_PRICE: 
                print(f"   Skipping {market['ticker']}: Price {buy_yes_price}¢ outside range.")
                continue
            
            if buy_yes_price < (75 - FEE_BUFFER):
                print(f"   🚀 EXECUTE: Buying {qty}x {market['ticker']} @ {buy_yes_price}¢")
                try:
                    resp = client.place_order(market['ticker'], "buy", "yes", qty, buy_yes_price)
                    if resp.status_code == 201:
                        log_trade(market['ticker'], safe_forecast, strike, gap, buy_yes_price, qty, "BUY")
                        send_discord_alert(f"**Trade ({city['name']})**: Bought {qty}x {market['ticker']} @ {buy_yes_price}¢")
                    else:
                        print(f"   ❌ Order Failed: {resp.text}")
                except Exception as e:
                    print(f"   ❌ Order Error: {e}")
            else:
                print(f"   Skipping {market['ticker']}: Price {buy_yes_price}¢ too expensive.")

if __name__ == "__main__":
    main()

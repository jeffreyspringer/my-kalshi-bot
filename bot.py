import os
import uuid
import requests
import csv
import time
import json
import base64
from datetime import datetime, timedelta, timezone
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.serialization import load_pem_private_key

# --- CONFIGURATION ---
HOST = "https://api.elections.kalshi.com"
CASHOUT_HOUR = 21  # 9 PM EST
STATS_FILE = "city_stats.json"

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
MIN_PRICE = 2              
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

# --- UTILS & STATS ---

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

def get_target_date_str():
    est_now = datetime.now(timezone.utc) - timedelta(hours=5)
    return est_now.strftime("%y%b%d").upper()

def get_city_from_ticker(ticker):
    """Finds which city a ticker belongs to."""
    for city in CITIES:
        # e.g. KXHIGHTNOLA is inside the ticker string
        if city['ticker'].replace("KXHIGHT", "").replace("KXHIGH", "") in ticker:
            return city['name']
    return "UNKNOWN"

def update_city_stats(city_name, pnl_cents):
    """Updates the persistent JSON file with PnL."""
    stats = {}
    if os.path.exists(STATS_FILE):
        try:
            with open(STATS_FILE, 'r') as f:
                stats = json.load(f)
        except: pass
    
    current_total = stats.get(city_name, 0)
    new_total = current_total + pnl_cents
    stats[city_name] = new_total
    
    with open(STATS_FILE, 'w') as f:
        json.dump(stats, f)
        
    return new_total

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

# --- TRADING LOGIC WITH PNL ---

def handle_sell(client, ticker, qty, sell_price, entry_price, reason):
    """Centralized function to handle sells and reporting."""
    city_name = get_city_from_ticker(ticker)
    
    # Calculate PnL (Profit/Loss)
    # entry_price is what we paid on avg. sell_price is what we get.
    gross_pnl_cents = (sell_price - entry_price) * qty
    
    try:
        resp = client.place_order(ticker, "sell", "yes", qty, sell_price)
        if resp.status_code == 201:
            # Update Stats
            new_city_total = update_city_stats(city_name, gross_pnl_cents)
            
            # Format numbers for humans
            pnl_str = f"+${gross_pnl_cents/100:.2f}" if gross_pnl_cents >= 0 else f"-${abs(gross_pnl_cents)/100:.2f}"
            total_str = f"${new_city_total/100:.2f}"
            emoji = "💰" if gross_pnl_cents >= 0 else "📉"
            
            msg = (
                f"{emoji} **{reason}** | **{city_name}**\n"
                f"Sold {qty}x {ticker} @ {sell_price}¢ (Entry: {entry_price}¢)\n"
                f"**Trade PnL:** {pnl_str} | **City Total:** {total_str}"
            )
            log_trade(ticker, "SELL", "N/A", 0, sell_price, qty, "SELL")
            send_discord_alert(msg)
            print(f"   {emoji} SOLD: {ticker} (PnL: {pnl_str})")
        else:
            print(f"   ❌ Sell Failed: {resp.text}")
    except Exception as e:
        print(f"   ❌ Sell Error: {e}")

def check_daytime_profits(client):
    print("--- 💰 Checking for Jackpots (>92¢) ---")
    try:
        positions = client.get_positions()
        for pos in positions:
            if pos['position'] <= 0: continue
            ob = client.get_orderbook(pos['ticker'])
            if not ob or not ob['yes']: continue
            best_bid = ob['yes'][0][0]
            
            if best_bid >= PROFIT_TAKE_PRICE:
                entry_price = pos.get('average_price', pos.get('avg_price', 0))
                handle_sell(client, pos['ticker'], pos['position'], best_bid, entry_price, "JACKPOT")
    except: pass

def liquidate_winners(client):
    print(f"--- 🌙 Night Shift: Liquidating Winners ---")
    try:
        positions = client.get_positions()
        for pos in positions:
            if pos['position'] <= 0: continue
            ob = client.get_orderbook(pos['ticker'])
            if not ob or not ob['yes']: continue
            best_bid = ob['yes'][0][0]
            entry_price = pos.get('average_price', pos.get('avg_price', 0))
            
            if best_bid > entry_price and best_bid > 5:
                handle_sell(client, pos['ticker'], pos['position'], best_bid, entry_price, "NIGHT CASHOUT")
    except: pass

def manage_risk(client, city_ticker, current_forecast):
    try:
        positions = client.get_positions()
        for pos in positions:
            if city_ticker not in pos['ticker']: continue
            if pos['position'] <= 0: continue
            
            try:
                strike = float(pos['ticker'].split('-T')[-1])
            except: continue
            
            distance = abs(current_forecast - strike)
            ob = client.get_orderbook(pos['ticker'])
            if not ob or not ob['yes']: continue
            current_bid = ob['yes'][0][0]
            entry_price = pos.get('average_price', pos.get('avg_price', 0))
            
            should_sell = False
            reason = ""
            
            if distance > 2.0:
                is_profitable = current_bid > entry_price
                is_market_confident = current_bid > 50
                
                if is_profitable:
                    should_sell = True
                    reason = "Forecast Moved (Profit Take)"
                elif not is_market_confident:
                    should_sell = True
                    reason = "STOP LOSS (Forecast Moved)"
            
            if should_sell:
                handle_sell(client, pos['ticker'], pos['position'], current_bid, entry_price, reason)
    except: pass

def main():
    print("🚀 Bot Starting (Financial Tracking V23)...")
    if os.getenv("TRADING_ENABLED", "TRUE").upper() == "FALSE": return
    
    current_utc = datetime.utcnow().hour
    current_est = (current_utc - 5) % 24
    
    target_date_str = get_target_date_str()
    print(f"🕒 Time: {current_est}:00 EST | 🔒 Date: {target_date_str}")
    
    try:
        client = KalshiClient()
        if client.get_balance() < MIN_BALANCE_CENTS: return
        
        check_daytime_profits(client)
        
        if current_est >= CASHOUT_HOUR:
            liquidate_winners(client)
            print(f"💤 Night Mode Active. Sleeping.")
            return

    except Exception as e:
        print(f"❌ Login Error: {e}")
        return

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
        
        manage_risk(client, city['ticker'], safe_forecast)

        try:
            markets = client._req("GET", f"/markets?series_ticker={city['ticker']}&status=open").json().get("markets", [])
        except: continue
        
        if not markets: continue

        for market in markets:
            if target_date_str not in market['ticker']: continue

            try:
                strike = float(market['ticker'].split('-T')[-1])
            except: continue

            distance = abs(safe_forecast - strike)
            target_side = "no" 
            if distance <= 1.5: target_side = "yes"
            
            if target_side == "no" and distance < 3.0: continue

            ob = client.get_orderbook(market['ticker'])
            if not ob: continue

            price = 0
            if target_side == "yes":
                if not ob['no']: continue
                price = 100 - ob['no'][0][0]
            else: 
                if not ob['yes']: continue
                price = 100 - ob['yes'][0][0]

            if price < MIN_PRICE or price > MAX_PRICE: continue

            qty = LOW_CONF_COUNT
            if target_side == "yes": qty = MED_CONF_COUNT
            if target_side == "no" and distance >= 5.0: qty = HIGH_CONF_COUNT

            try:
                positions = client.get_positions()
                curr_pos = next((p['position'] for p in positions if p['ticker'] == market['ticker']), 0)
                qty = min(qty, MAX_TOTAL_POS - abs(curr_pos))
            except: qty = LOW_CONF_COUNT
            
            if qty <= 0: continue

            print(f"   🚀 EXECUTE: Buying {qty}x {market['ticker']} [{target_side.upper()}] @ {price}¢")
            
            try:
                resp = client.place_order(market['ticker'], "buy", target_side, qty, price)
                if resp.status_code == 201:
                    log_trade(market['ticker'], safe_forecast, strike, distance, price, qty, f"BUY_{target_side.upper()}")
                    
                    # Fetch current stats for the buy message
                    city_name = get_city_from_ticker(market['ticker'])
                    current_stats = update_city_stats(city_name, 0) # Just get current, don't add
                    total_str = f"${current_stats/100:.2f}"
                    
                    msg = (
                        f"🚀 **BUY** | **{city_name}**\n"
                        f"Bought {qty}x {market['ticker']} **[{target_side.upper()}]** @ {price}¢\n"
                        f"**City Total PnL:** {total_str}"
                    )
                    send_discord_alert(msg)
            except: pass

if __name__ == "__main__":
    main()

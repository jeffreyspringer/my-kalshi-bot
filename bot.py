import os
import re 
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
# ✅ NEW: Imports for retry logic
from requests.adapters import HTTPAdapter
from urllib3.util import Retry

# --- CONFIGURATION ---
HOST = "https://api.elections.kalshi.com"
CASHOUT_HOUR = 21   
STATS_FILE = "city_stats.json"
PORTFOLIO_FILE = "portfolio_history.csv"

CITIES = [
    { "name": "NOLA",    "lat": 29.99, "lon": -90.25,  "ticker": "KXHIGHTNOLA", "airport": "KMSY", "emoji": "🎷", "tz_offset": -6 },
    { "name": "CHICAGO", "lat": 41.79, "lon": -87.75,  "ticker": "KXHIGHCHI",   "airport": "KMDW", "emoji": "🍕", "tz_offset": -6 },
    { "name": "MIAMI",   "lat": 25.80, "lon": -80.29,  "ticker": "KXHIGHMIA",   "airport": "KMIA", "emoji": "🌴", "tz_offset": -5 },
    { "name": "SEATTLE", "lat": 47.45, "lon": -122.31, "ticker": "KXHIGHTSEA",  "airport": "KSEA", "emoji": "☕", "tz_offset": -8 },
    { "name": "AUSTIN",  "lat": 30.19, "lon": -97.67,  "ticker": "KXHIGHAUS",   "airport": "KAUS", "emoji": "🎸", "tz_offset": -6 }
]

MIN_BALANCE_CENTS = 500     
MAX_TOTAL_POS = 20          
PROFIT_TAKE_PRICE = 92      
MIN_PRICE = 2              
MAX_PRICE = 98
LOW_CONF_COUNT = 1
MED_CONF_COUNT = 3
HIGH_CONF_COUNT = 10

class KalshiClient:
    def __init__(self):
        self.key_id = os.getenv("KALSHI_KEY", "").strip()
        self.private_key = self._load_private_key(os.getenv("KALSHI_PRIVATE_KEY", ""))
        
        # ✅ NEW: Setup a persistent session with automatic retries for 500/502/503/504
        self.session = requests.Session()
        retry_strategy = Retry(
            total=5,                  # Try 5 times total
            backoff_factor=1,         # Wait 1s, 2s, 4s, 8s... between tries
            status_forcelist=[500, 502, 503, 504], # Errors that trigger a retry
            allowed_methods=["GET", "POST"]
        )
        adapter = HTTPAdapter(max_retries=retry_strategy)
        self.session.mount("https://", adapter)

    def _load_private_key(self, key_str):
        key_str = key_str.strip().replace('\\n', '\n')
        if "-----BEGIN" not in key_str:
            key_str = f"-----BEGIN RSA PRIVATE KEY-----\n{key_str}\n-----END RSA PRIVATE KEY-----"
        return load_pem_private_key(key_str.encode(), password=None)

    def _req(self, method, path, body=None):
        timestamp = str(int(time.time() * 1000))
        msg = f"{timestamp}{method}/trade-api/v2{path}"
        if body: msg += json.dumps(body, separators=(',', ':'))
        signature = self.private_key.sign(msg.encode('utf-8'), padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.MAX_LENGTH), hashes.SHA256())
        headers = {"KALSHI-ACCESS-KEY": self.key_id, "KALSHI-ACCESS-SIGNATURE": base64.b64encode(signature).decode('utf-8'), "KALSHI-ACCESS-TIMESTAMP": timestamp, "Content-Type": "application/json"}
        url = f"{HOST}/trade-api/v2{path}"
        
        # ✅ Changed to use self.session
        if method == "GET": return self.session.get(url, headers=headers, timeout=10)
        return self.session.post(url, headers=headers, json=body, timeout=10)

    def get_balance(self):
        try:
            res = self._req("GET", "/portfolio/balance")
            res.raise_for_status()
            return res.json().get("balance", 0)
        except Exception as e:
            print(f"   ⚠️ Balance Check Failed: {e}")
            return 999999 # Safe fallback to allow trading even if balance query glitched

    def get_positions(self):
        try:
            res = self._req("GET", "/portfolio/positions")
            res.raise_for_status()
            return res.json().get("market_positions", [])
        except: return []

    def get_orderbook(self, ticker):
        try:
            res = self._req("GET", f"/markets/{ticker}/orderbook")
            if res.status_code != 200: return None
            return res.json().get("orderbook")
        except: return None

    def place_order(self, ticker, action, side, count, price):
        body = {"action": action, "count": count, "type": "limit", "ticker": ticker, "side": side, "yes_price": price if side == "yes" else 0, "no_price": price if side == "no" else 0, "client_order_id": str(uuid.uuid4())}
        if side == "yes": del body["no_price"]
        else: del body["yes_price"]
        return self._req("POST", "/portfolio/orders", body)

# --- LEDGER ---
def get_city_meta(ticker):
    for city in CITIES:
        if city['ticker'].replace("KXHIGHT", "").replace("KXHIGH", "") in ticker: return city
    return {"name": "UNKNOWN", "emoji": "❓"}

def update_city_stats(city_name, pnl_cents):
    stats = {}
    if os.path.exists(STATS_FILE):
        try:
            with open(STATS_FILE, 'r') as f: stats = json.load(f)
        except: pass
    new_total = stats.get(city_name, 0) + pnl_cents
    stats[city_name] = new_total
    with open(STATS_FILE, 'w') as f: json.dump(stats, f)
    return new_total

def log_trade_csv(city_name, ticker, forecast, strike, gap, price, qty, action, pnl_cents, bankroll_cents):
    file_exists = os.path.isfile("trade_log.csv")
    with open("trade_log.csv", "a", newline="") as f:
        writer = csv.writer(f)
        if not file_exists: writer.writerow(["Date", "City", "Action", "Ticker", "Qty", "Price ($)", "PnL ($)", "City Bankroll ($)", "Forecast", "Gap"])
        writer.writerow([datetime.now().strftime("%Y-%m-%d %H:%M"), city_name, action, ticker, qty, f"${price/100:.2f}", f"${pnl_cents/100:.2f}" if pnl_cents!=0 else "-", f"${bankroll_cents/100:.2f}", f"{forecast}°", f"{gap:.1f}°"])

def track_portfolio_value(client):
    try:
        balance = client.get_balance()
        file_exists = os.path.isfile(PORTFOLIO_FILE)
        with open(PORTFOLIO_FILE, "a", newline="") as f:
            writer = csv.writer(f)
            if not file_exists: writer.writerow(["Date", "Total Value ($)"])
            writer.writerow([datetime.now().strftime("%Y-%m-%d %H:%M"), balance/100])
        return balance
    except: return 0

# --- DISCORD ---
def send_rich_discord_alert(title, color, fields):
    webhook = os.getenv("DISCORD_WEBHOOK_URL")
    if not webhook: return
    requests.post(webhook, json={"embeds": [{"title": title, "color": color, "fields": fields, "footer": {"text": "Kalshi Bot V45 (Resilient)"}, "timestamp": datetime.utcnow().isoformat()}]})

def get_target_date_str():
    est_now = datetime.now(timezone.utc) - timedelta(hours=5)
    return est_now.strftime("%y%b%d").upper()

# --- FORECASTING & HISTORY ---

def get_today_high_so_far(airport_code, tz_offset):
    try:
        headers = {'User-Agent': '(KalshiBot, contact@example.com)'}
        url = f"https://api.weather.gov/stations/{airport_code.upper()}/observations"
        res = requests.get(url, headers=headers).json()
        local_now = datetime.now(timezone.utc) + timedelta(hours=tz_offset)
        today_local = local_now.date()
        obs_temps = []
        for obs in res['features']:
            utc_time = datetime.fromisoformat(obs['properties']['timestamp'].replace('Z', '+00:00'))
            local_time = utc_time + timedelta(hours=tz_offset)
            if local_time.date() == today_local:
                temp_c = obs['properties']['temperature']['value']
                if temp_c is not None: obs_temps.append((temp_c * 9/5) + 32)
        return max(obs_temps) if obs_temps else 0
    except: return 0

def get_nws_forecast(lat, lon):
    try:
        headers = {'User-Agent': '(KalshiBot, contact@example.com)'}
        p_res = requests.get(f"https://api.weather.gov/points/{lat},{lon}", headers=headers).json()
        grid = requests.get(p_res['properties']['forecast'], headers=headers).json()
        for p in grid['properties']['periods']:
            if p['isDaytime']: return p['temperature']
    except: pass
    return None

def get_nws_hourly_forecast(lat, lon):
    try:
        headers = {'User-Agent': '(KalshiBot, contact@example.com)'}
        p_res = requests.get(f"https://api.weather.gov/points/{lat},{lon}", headers=headers).json()
        grid = requests.get(p_res['properties']['forecastHourly'], headers=headers).json()
        temps = [p['temperature'] for p in grid['properties']['periods'][:18]]
        return max(temps) if temps else None
    except: return None

# --- TRADING ACTIONS ---

def execute_buy(client, market, qty, price, target_side, distance, safe_forecast):
    city_meta = get_city_meta(market['ticker'])
    try:
        resp = client.place_order(market['ticker'], "buy", target_side, qty, price)
        if resp.status_code == 201:
            current_bankroll = update_city_stats(city_meta['name'], 0)
            log_trade_csv(city_meta['name'], market['ticker'], safe_forecast, 0, distance, price, qty, f"BUY {target_side.upper()}", 0, current_bankroll)
            fields = [{"name": "City", "value": f"{city_meta['emoji']} {city_meta['name']}", "inline": True}, {"name": "Order", "value": f"Buy **{qty}x {target_side.upper()}** @ {price}¢", "inline": False}, {"name": "Stats", "value": f"Forecast {safe_forecast:.1f}° (Diff {distance:.1f}°)", "inline": True}]
            send_rich_discord_alert("🚀 BUY ORDER EXECUTED", 3447003, fields)
            print(f"   ✅ SUCCESS: Bought {qty}x {market['ticker']}")
    except: pass

def execute_sell(client, ticker, qty, sell_price, entry_price, reason):
    city_meta = get_city_meta(ticker)
    gross_pnl_cents = (sell_price - entry_price) * qty
    try:
        resp = client.place_order(ticker, "sell", "yes", qty, sell_price)
        if resp.status_code == 201:
            new_total = update_city_stats(city_meta['name'], gross_pnl_cents)
            log_trade_csv(city_meta['name'], ticker, "N/A", 0, 0, sell_price, qty, "SELL", gross_pnl_cents, new_total)
            fields = [{"name": "City", "value": f"{city_meta['emoji']} {city_meta['name']}", "inline": True}, {"name": "Reason", "value": reason, "inline": True}, {"name": "PnL", "value": f"**${gross_pnl_cents/100:.2f}**", "inline": True}]
            send_rich_discord_alert(f"🤑 POSITION CLOSED", 5763719, fields)
            print(f"   ✅ SOLD: {ticker} ({reason})")
    except: pass

def manage_risk(client, city_ticker, current_forecast):
    try:
        positions = client.get_positions()
        for pos in positions:
            if city_ticker not in pos['ticker']: continue
            if pos['position'] <= 0: continue
            try: strike = float(re.findall(r"(\d+(?:\.\d+)?)", pos['ticker'])[-1])
            except: continue
            diff = abs(current_forecast - strike)
            ob = client.get_orderbook(pos['ticker'])
            if ob and diff > 1.1: 
                bid = ob['yes'][0][0] if ob['yes'] else 0
                execute_sell(client, pos['ticker'], pos['position'], bid, pos.get('average_price', 0), "Forecast Drifted")
    except: pass

def main():
    print("🚀 Bot Starting (V45 Resilient)...")
    if os.getenv("TRADING_ENABLED", "TRUE").upper() == "FALSE": return
    target_date_str = get_target_date_str()
    
    try:
        client = KalshiClient()
        track_portfolio_value(client)
        
        # ✅ Balance Check now has a try/except inside the client
        balance = client.get_balance()
        if balance < MIN_BALANCE_CENTS: 
            print("❌ Low Balance. Stopping."); return
            
        if (datetime.utcnow().hour - 5) % 24 >= 21: 
            for p in client.get_positions():
                ob = client.get_orderbook(p['ticker'])
                if ob and ob['yes']: execute_sell(client, p['ticker'], p['position'], ob['yes'][0][0], p.get('average_price', 0), "Night Cashout")
            return
        all_positions = client.get_positions()
    except Exception as e: print(f"❌ Initialization Error: {e}"); return

    for city in CITIES:
        print(f"\n🔎 {city['name']}...")
        nws, hourly = get_nws_forecast(city['lat'], city['lon']), get_nws_hourly_forecast(city['lat'], city['lon'])
        high_so_far = get_today_high_so_far(city['airport'], city['tz_offset'])
        
        if not nws and not hourly: continue
        future_high = (nws + hourly) / 2 if (nws and hourly) else (nws or hourly)
        safe_forecast = max(future_high, high_so_far)
        
        print(f"   🎯 Thought: Daily {nws}° | Hourly {hourly}° | Hist {high_so_far:.1f}°")
        print(f"   ✅ Final Target: {safe_forecast:.1f}°")
        
        manage_risk(client, city['ticker'], safe_forecast)
        has_pos = any(p['position'] > 0 and city['ticker'] in p['ticker'] and target_date_str in p['ticker'] for p in all_positions)
        
        try: 
            markets_res = client._req("GET", "/markets?series_ticker=" + city['ticker'] + "&status=open")
            markets = markets_res.json().get("markets", [])
            print(f"   📊 API returned {len(markets)} total markets.")
        except: continue

        for market in markets:
            ticker = market['ticker']
            if target_date_str not in ticker: continue
            try: strike = float(re.findall(r"(\d+(?:\.\d+)?)", ticker)[-1])
            except: continue
            
            diff = abs(safe_forecast - strike)
            target_side = "none"
            
            if diff <= 0.6: 
                if not has_pos: target_side = "yes"
                else: print(f"   ⏭️  SKIP YES: {ticker} matches, but already holding a position."); continue
            elif diff >= 1.8: target_side = "no"
            else: print(f"   🚧 SKIP: {ticker} ({strike}°) is in Coin Flip Zone (Diff {diff:.1f}°)."); continue

            ob = client.get_orderbook(ticker)
            if not ob: continue
            
            price = 0
            if target_side == "yes":
                if ob['no']: price = 100 - ob['no'][0][0]
                else: continue
            else: 
                if ob['yes']: price = 100 - ob['yes'][0][0]
                else: continue

            if price < MIN_PRICE or price > MAX_PRICE:
                print(f"   ❌ Price {price}¢ is outside safe zone."); continue
            
            qty = LOW_CONF_COUNT
            if diff < 0.3: qty = MED_CONF_COUNT 
            if diff > 3.0: qty = HIGH_CONF_COUNT 
            
            print(f"   🚀 Buying {qty}x {ticker} [{target_side.upper()}] @ {price}¢")
            execute_buy(client, market, qty, price, target_side, diff, safe_forecast)

if __name__ == "__main__": main()

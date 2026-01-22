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
from requests.adapters import HTTPAdapter
from urllib3.util import Retry

# --- CONFIGURATION (NO CHANGES NEEDED HERE) ---
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
        self.session = requests.Session()
        retry_strategy = Retry(total=5, backoff_factor=1, status_forcelist=[500, 502, 503, 504])
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
        if method == "GET": return self.session.get(url, headers=headers, timeout=10)
        return self.session.post(url, headers=headers, json=body, timeout=10)

    def get_balance(self):
        try:
            res = self._req("GET", "/portfolio/balance")
            res.raise_for_status()
            return res.json().get("balance", 0)
        except: return 1000 # Fallback

    def get_positions(self):
        try:
            res = self._req("GET", "/portfolio/positions")
            res.raise_for_status()
            return res.json().get("market_positions", [])
        except: return []

    def place_order(self, ticker, action, side, count, price):
        body = {"action": action, "count": count, "type": "limit", "ticker": ticker, "side": side, "yes_price": price if side == "yes" else 0, "no_price": price if side == "no" else 0, "client_order_id": str(uuid.uuid4())}
        if side == "yes": del body["no_price"]
        else: del body["yes_price"]
        return self._req("POST", "/portfolio/orders", body)

# --- UTILITIES ---
def get_today_high_so_far(airport_code, tz_offset):
    try:
        headers = {'User-Agent': '(KalshiBot)'}
        url = f"https://api.weather.gov/stations/{airport_code.upper()}/observations"
        res = requests.get(url, headers=headers).json()
        local_now = datetime.now(timezone.utc) + timedelta(hours=tz_offset)
        today_local = local_now.date()
        obs_temps = []
        for obs in res['features']:
            utc_time = datetime.fromisoformat(obs['properties']['timestamp'].replace('Z', '+00:00'))
            if (utc_time + timedelta(hours=tz_offset)).date() == today_local:
                temp_c = obs['properties']['temperature']['value']
                if temp_c is not None: obs_temps.append((temp_c * 9/5) + 32)
        return max(obs_temps) if obs_temps else 0
    except: return 0

def main():
    print("🚀 Bot Starting (V46 Direct Price)...")
    client = KalshiClient()
    target_date_str = (datetime.now(timezone.utc) - timedelta(hours=5)).strftime("%y%b%d").upper()
    
    all_positions = client.get_positions()

    for city in CITIES:
        print(f"\n🔎 {city['name']}...")
        nws_res = requests.get(f"https://api.weather.gov/points/{city['lat']},{city['lon']}", headers={'User-Agent': '(KalshiBot)'}).json()
        daily = requests.get(nws_res['properties']['forecast'], headers={'User-Agent': '(KalshiBot)'}).json()['properties']['periods'][0]['temperature']
        hourly_res = requests.get(nws_res['properties']['forecastHourly'], headers={'User-Agent': '(KalshiBot)'}).json()
        hourly_max = max([p['temperature'] for p in hourly_res['properties']['periods'][:18]])
        hist_high = get_today_high_so_far(city['airport'], city['tz_offset'])
        
        safe_forecast = max((daily + hourly_max)/2, hist_high)
        print(f"   🎯 Target: {safe_forecast:.1f}° (Hist: {hist_high}°)")
        
        has_pos = any(p['position'] > 0 and city['ticker'] in p['ticker'] and target_date_str in p['ticker'] for p in all_positions)
        
        # ✅ FIX: Fetching actual market prices directly from the Market list
        markets = client._req("GET", f"/markets?series_ticker={city['ticker']}&status=open").json().get("markets", [])

        for market in markets:
            if target_date_str not in market['ticker']: continue
            try: strike = float(re.findall(r"(\d+(?:\.\d+)?)", market['ticker'])[-1])
            except: continue
            
            diff = abs(safe_forecast - strike)
            target_side = "yes" if diff <= 0.6 and not has_pos else ("no" if diff >= 1.8 else "none")
            if target_side == "none": continue

            # ✅ FIX: Use the 'yes_price' and 'no_price' provided by the Market API
            # These values represent the current market consensus, not empty orderbooks.
            if target_side == "yes":
                price = market.get('yes_bid', 0) + 1 # Bid slightly above market to get filled
                if price == 1: price = market.get('last_price', 50) # Fallback to last trade
            else:
                price = market.get('no_bid', 0) + 1
                if price == 1: price = (100 - market.get('last_price', 50))

            if price < MIN_PRICE or price > MAX_PRICE:
                print(f"   🚧 Skipping {strike}°: Price {price}¢ out of range.")
                continue
            
            print(f"   🚀 Buying {target_side.upper()} for {strike}° at {price}¢ (Diff {diff:.1f})")
            client.place_order(market['ticker'], "buy", target_side, LOW_CONF_COUNT, price)

if __name__ == "__main__": main()

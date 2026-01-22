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
        
        # 1. Prepare JSON Body (Standard formatting with sort_keys for consistency)
        json_str = ""
        if body:
            json_str = json.dumps(body, sort_keys=True)
            
        # 2. Build Message String for Signing
        # Format: timestamp + method + path + body
        msg_str = f"{timestamp}{method}/trade-api/v2{path}{json_str}"
        
        # 3. Sign the UTF-8 Bytes of the message
        signature = self.private_key.sign(
            msg_str.encode('utf-8'),
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
        
        # 4. Send exact JSON bytes
        if method == "GET": 
            return self.session.get(url, headers=headers, timeout=10)
        
        # Send the exact string we signed, encoded as UTF-8 bytes
        return self.session.post(url, headers=headers, data=json_str.encode('utf-8'), timeout=10)

    def place_order(self, ticker, action, side, count, price):
        # Explicit integer casting to avoid float issues in JSON
        body = {
            "action": action, 
            "count": int(count), 
            "type": "limit", 
            "ticker": ticker, 
            "side": side, 
            "client_order_id": str(uuid.uuid4())
        }
        if side == "yes": body["yes_price"] = int(price)
        else: body["no_price"] = int(price)
        
        res = self._req("POST", "/portfolio/orders", body)
        
        if res.status_code == 201:
            print(f"   ✅ ORDER SUCCESS: {ticker} @ {price}¢ [ID: {res.json().get('order_id')}]")
        else:
            # Print the failure details for debugging
            print(f"   ❌ REJECTED: {res.status_code}")
            print(f"   ⚠️ Payload: {json.dumps(body)}")
            print(f"   ⚠️ Response: {res.text}")
            
        return res

# --- UTILS ---
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
    print("🚀 Bot Starting (V49 Authenticator)...")
    client = KalshiClient()
    target_date_str = (datetime.now(timezone.utc) - timedelta(hours=5)).strftime("%y%b%d").upper()
    
    # Check Balance
    try:
        bal_res = client._req("GET", "/portfolio/balance")
        if bal_res.status_code == 200:
            print(f"💰 Account Balance: ${bal_res.json().get('balance', 0)/100:.2f}")
    except: pass

    for city in CITIES:
        print(f"\n🔎 {city['name']}...")
        try:
            nws_res = requests.get(f"https://api.weather.gov/points/{city['lat']},{city['lon']}", headers={'User-Agent': '(KalshiBot)'}).json()
            daily = requests.get(nws_res['properties']['forecast'], headers={'User-Agent': '(KalshiBot)'}).json()['properties']['periods'][0]['temperature']
            hourly_res = requests.get(nws_res['properties']['forecastHourly'], headers={'User-Agent': '(KalshiBot)'}).json()
            hourly_max = max([p['temperature'] for p in hourly_res['properties']['periods'][:18]])
            hist_high = get_today_high_so_far(city['airport'], city['tz_offset'])
            safe_forecast = max((daily + hourly_max)/2, hist_high)
            print(f"   🎯 Target: {safe_forecast:.1f}°")
        except: continue

        markets = client._req("GET", f"/markets?series_ticker={city['ticker']}&status=open").json().get("markets", [])

        for market in markets:
            if target_date_str not in market['ticker']: continue
            try: strike = float(re.findall(r"(\d+(?:\.\d+)?)", market['ticker'])[-1])
            except: continue
            
            diff = abs(safe_forecast - strike)
            target_side = "yes" if diff <= 0.6 else ("no" if diff >= 1.8 else "none")
            if target_side == "none": continue

            if target_side == "yes":
                price = market.get('yes_bid', 0) + 1
                if price <= 1: price = market.get('last_price', 20)
            else:
                price = market.get('no_bid', 0) + 1
                if price <= 1: price = (100 - market.get('last_price', 80))

            if price < MIN_PRICE or price > MAX_PRICE: continue
            
            print(f"   🚀 Buying {target_side.upper()} for {strike}° @ {price}¢")
            client.place_order(market['ticker'], "buy", target_side, LOW_CONF_COUNT, price)
            time.sleep(0.5) 

if __name__ == "__main__": main()

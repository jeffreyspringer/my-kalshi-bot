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

# --- CONFIGURATION ---
HOST = "https://api.elections.kalshi.com"
CASHOUT_HOUR = 21   # 9 PM EST
STATS_FILE = "city_stats.json"
PORTFOLIO_FILE = "portfolio_history.csv"

# ✅ CITIES
CITIES = [
    { "name": "NOLA",    "lat": 29.99, "lon": -90.25,  "ticker": "KXHIGHTNOLA", "airport": "KMSY", "emoji": "🎷" },
    { "name": "CHICAGO", "lat": 41.79, "lon": -87.75,  "ticker": "KXHIGHCHI",   "airport": "KMDW", "emoji": "🍕" },
    { "name": "MIAMI",   "lat": 25.80, "lon": -80.29,  "ticker": "KXHIGHMIA",   "airport": "KMIA", "emoji": "🌴" },
    { "name": "SEATTLE", "lat": 47.45, "lon": -122.31, "ticker": "KXHIGHTSEA",  "airport": "KSEA", "emoji": "☕" },
    { "name": "AUSTIN",  "lat": 30.19, "lon": -97.67,  "ticker": "KXHIGHAUS",   "airport": "KAUS", "emoji": "🎸" }
]

# RISK SETTINGS
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

# --- LEDGER SYSTEM ---
def get_city_meta(ticker):
    for city in CITIES:
        clean_ticker = city['ticker'].replace("KXHIGHT", "").replace("KXHIGH", "")
        if clean_ticker in ticker:
            return city
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
        if not file_exists:
            writer.writerow(["Date", "City", "Action", "Ticker", "Qty", "Price ($)", "PnL ($)", "City Bankroll ($)", "Forecast", "Gap"])
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
    requests.post(webhook, json={"embeds": [{"title": title, "color": color, "fields": fields, "footer": {"text": "Kalshi Bot V41 (Direct Price)"}, "timestamp": datetime.utcnow().isoformat()}]})

def get_target_date_str():
    est_now = datetime.now(timezone.utc) - timedelta(hours=5)
    return est_now.strftime("%y%b%d").upper()

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

def get_nws_hourly_forecast(lat, lon):
    try:
        headers = {'User-Agent': '(KalshiBot, contact@example.com)'}
        p_res = requests.get(f"https://api.weather.gov/points/{lat},{lon}", headers=headers).json()
        f_url = p_res['properties']['forecastHourly']
        grid = requests.get(f_url, headers=headers).json()
        periods = grid['properties']['periods']
        temps = []
        for i in range(min(18, len(periods))):
            temps.append(periods[i]['temperature'])
        if temps: return max(temps)
    except: return None
    return None

# --- TRADING ACTIONS ---
def execute_buy(client, market, qty, price, target_side, distance, safe_forecast):
    city_meta = get_city_meta(market['ticker'])
    try:
        resp = client.place_order(market['ticker'], "buy", target_side, qty, price)
        if resp.status_code == 201:
            current_bankroll = update_city_stats(city_meta['name'], 0)
            log_trade_csv(city_meta['name'], market['ticker'], safe_forecast, 0, distance, price, qty, f"BUY {target_side.upper()}", 0, current_bankroll)
            fields = [
                {"name": "City", "value": f"{city_meta['emoji']} {city_meta['name']}", "inline": True},
                {"name": "Contract", "value": f"`{market['ticker']}`", "inline": True},
                {"name": "Order", "value": f"Buy **{qty}x {target_side.upper()}** @ {price}¢", "inline": False},
                {"name": "Stats", "value": f"Forecast {safe_forecast:.1f}° (Dist {distance:.1f}°)", "inline": False},
                {"name": "City Bankroll", "value": f"${current_bankroll/100:.2f}", "inline": True}
            ]
            send_rich_discord_alert("🚀 BUY ORDER EXECUTED", 3447003, fields)
            print(f"   ✅ SUCCESS: Bought {qty}x {market['ticker']}")
        else:
            print(f"   ❌ FAILED: {resp.text}")
    except Exception as e: print(f"Buy Error: {e}")

def execute_sell(client, ticker, qty, sell_price, entry_price, reason):
    city_meta = get_city_meta(ticker)
    gross_pnl_cents = (sell_price - entry_price) * qty
    try:
        resp = client.place_order(ticker, "sell", "yes", qty, sell_price)
        if resp.status_code == 201:
            new_total = update_city_stats(city_meta['name'], gross_pnl_cents)
            log_trade_csv(city_meta['name'], ticker, "N/A", 0, 0, sell_price, qty, "SELL", gross_pnl_cents, new_total)
            color = 5763719 if gross_pnl_cents >= 0 else 15548997
            fields = [
                {"name": "City", "value": f"{city_meta['emoji']} {city_meta['name']}", "inline": True},
                {"name": "Reason", "value": reason, "inline": True},
                {"name": "Result", "value": f"Sold {qty}x @ {sell_price}¢ (Entry: {entry_price}¢)", "inline": False},
                {"name": "PnL", "value": f"**${gross_pnl_cents/100:.2f}**", "inline": True},
                {"name": "Bankroll", "value": f"${new_total/100:.2f}", "inline": True}
            ]
            send_rich_discord_alert(f"🤑 POSITION CLOSED", color, fields)
            print(f"   ✅ SOLD {ticker}")
    except Exception as e: print(f"Sell Error: {e}")

def check_daytime_profits(client):
    print("--- 💰 Checking for Jackpots ---")
    try:
        positions = client.get_positions()
        for pos in positions:
            if pos['position'] <= 0: continue
            ob = client.get_orderbook(pos['ticker'])
            if not ob: continue
            # Check YES bid to see if it's worth selling
            best_bid = ob['yes'][0][0] if ob['yes'] else 0
            if best_bid >= PROFIT_TAKE_PRICE:
                entry_price = pos.get('average_price', 0)
                execute_sell(client, pos['ticker'], pos['position'], best_bid, entry_price, "JACKPOT")
    except: pass

def liquidate_winners(client):
    print(f"--- 🌙 Night Shift ---")
    try:
        positions = client.get_positions()
        for pos in positions:
            if pos['position'] <= 0: continue
            ob = client.get_orderbook(pos['ticker'])
            if not ob: continue
            best_bid = ob['yes'][0][0] if ob['yes'] else 0
            entry_price = pos.get('average_price', 0)
            if best_bid > entry_price and best_bid > 5:
                execute_sell(client, pos['ticker'], pos['position'], best_bid, entry_price, "NIGHT CASHOUT")
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
            if not ob: continue
            current_bid = ob['yes'][0][0] if ob['yes'] else 0
            entry_price = pos.get('average_price', 0)
            
            should_sell = False
            reason = ""
            
            if diff > 1.0:
                is_profitable = current_bid > entry_price
                if is_profitable: should_sell = True; reason = "PROFIT PROTECT (Drifted)"
                else: should_sell = True; reason = "STOP LOSS (Drifted)"
                    
            if should_sell: execute_sell(client, pos['ticker'], pos['position'], current_bid, entry_price, reason)
    except: pass

def main():
    print("🚀 Bot Starting (V41 Direct Price)...")
    if os.getenv("TRADING_ENABLED", "TRUE").upper() == "FALSE": return
    current_est = (datetime.utcnow().hour - 5) % 24
    target_date_str = get_target_date_str()
    print(f"🕒 Time: {current_est}:00 EST | 🔒 Date: {target_date_str}")
    
    try:
        client = KalshiClient()
        track_portfolio_value(client)
        if client.get_balance() < MIN_BALANCE_CENTS: 
            print("❌ Low Balance"); return
        
        check_daytime_profits(client)
        if current_est >= CASHOUT_HOUR:
            liquidate_winners(client); return
        
        all_positions = client.get_positions()

    except Exception as e: print(f"❌ Login Error: {e}"); return

    print(f"--- Scanning {len(CITIES)} Cities ---")
    for city in CITIES:
        print(f"\n🔎 {city['name']}...")
        nws = get_nws_forecast(city['lat'], city['lon'])
        hourly_max = get_nws_hourly_forecast(city['lat'], city['lon'])
        
        nws_str = f"{nws}°" if nws else "N/A"
        hourly_str = f"{hourly_max}°" if hourly_max else "N/A"
        
        if not nws and not hourly_max: 
            print("   ⚠️ No Weather Data"); continue
        
        if nws and hourly_max: safe_forecast = (nws + hourly_max) / 2
        else: safe_forecast = nws or hourly_max
        
        print(f"   🎯 Forecast: {safe_forecast:.1f}° (Daily: {nws_str} | Hourly: {hourly_str})")
        
        manage_risk(client, city['ticker'], safe_forecast)
        
        has_active_position = False
        for p in all_positions:
            if p['position'] > 0 and city['ticker'] in p['ticker'] and target_date_str in p['ticker']:
                has_active_position = True
                print(f"   🔒 Active position found: {p['ticker']}.")
                break
        
        try: markets = client._req("GET", f"/markets?series_ticker={city['ticker']}&status=open").json().get("markets", [])
        except: print("   ⚠️ API Error fetching markets"); continue
        
        if not markets: continue

        for market in markets:
            ticker = market['ticker']
            if target_date_str not in ticker: continue
            
            try:
                matches = re.findall(r"(\d+(?:\.\d+)?)", ticker)
                if not matches: continue
                strike = float(matches[-1]) 
            except: continue
            
            diff = abs(safe_forecast - strike)
            target_side = "none"
            
            if diff <= 0.6: 
                if has_active_position:
                    print(f"   Skipping {strike}° [YES]: Already hold a position.")
                    continue
                target_side = "yes"
            elif diff >= 1.8:
                target_side = "no"
            else:
                print(f"   Skipping {strike}°: Coin Flip Zone (Diff {diff:.1f})")
                continue

            ob = client.get_orderbook(ticker)
            if not ob: continue
            
            # ✅ DIRECT ORDERBOOK ACCESS
            # Instead of 100 - NO Bid, we look for the actual Sellers (Asks)
            # yes_ask: Price to buy YES. no_ask: Price to buy NO.
            yes_ask = ob['no'][0][0] if ob['no'] else 0 # Best Seller for YES is inverse of No bidder? No.
            # Correction: Kalshi API orderbook 'yes' contains bidders. 'no' contains bidders for NO.
            # Best price to buy YES = (100 - best_bid_for_NO). 
            # If NO bidders are missing, we check the 'yes' side for sellers.
            
            price = 0
            if target_side == "yes":
                # Find the lowest price someone is willing to sell YES for
                # If there are NO bidders (ob['no']), we can buy at (100 - no_bid)
                no_bid = ob['no'][0][0] if ob['no'] else 0
                if no_bid > 0:
                    price = 100 - no_bid
                else:
                    print(f"   Skipping {strike}° [YES]: No Sellers (Empty Book)")
                    continue
            else: 
                # Find the lowest price someone is willing to sell NO for
                yes_bid = ob['yes'][0][0] if ob['yes'] else 0
                if yes_bid > 0:
                    price = 100 - yes_bid
                else:
                    print(f"   Skipping {strike}° [NO]: No Sellers (Empty Book)")
                    continue

            if price < MIN_PRICE or price > MAX_PRICE:
                print(f"   Skipping {strike}° [{target_side.upper()}]: Price {price}¢ is bad.")
                continue

            qty = LOW_CONF_COUNT
            if diff < 0.3: qty = MED_CONF_COUNT 
            if diff > 3.0: qty = HIGH_CONF_COUNT 
            
            print(f"   🚀 EXECUTE: Buying {qty}x {ticker} [{target_side}] @ {price}¢ (Diff {diff:.1f})")
            execute_buy(client, market, qty, price, target_side, diff, safe_forecast)

if __name__ == "__main__":
    main()

import os
import uuid
import requests
import csv
from datetime import datetime
import kalshi_python
from kalshi_python.models import *

# --- CONFIGURATION: CITY LIST ---
CITIES = [
    { "name": "NOLA", "lat": 29.99, "lon": -90.25, "ticker": "KXHIGHTNOLA" },
    { "name": "CHICAGO", "lat": 41.79, "lon": -87.75, "ticker": "KXHIGHTCHI" },
    { "name": "MIAMI", "lat": 25.80, "lon": -80.29, "ticker": "KXHIGHTMIA" },
    { "name": "SEATTLE", "lat": 47.45, "lon": -122.31, "ticker": "KXHIGHTSEA" },
    { "name": "AUSTIN", "lat": 30.19, "lon": -97.67, "ticker": "KXHIGHTAUS" }
]

# RISK & STRATEGY
MIN_BALANCE_CENTS = 500     
MAX_TOTAL_POS = 20          
PROFIT_TAKE_PRICE = 92      
FEE_BUFFER = 3
MIN_PRICE = 20
MAX_PRICE = 80

# CONFIDENCE SCALING
LOW_CONF_COUNT = 1
MED_CONF_COUNT = 3
HIGH_CONF_COUNT = 10

def send_discord_alert(message):
    webhook_url = os.getenv("DISCORD_WEBHOOK_URL")
    if not webhook_url: return
    try:
        requests.post(webhook_url, json={"content": message})
    except Exception as e:
        print(f"Discord Error: {e}")

def log_trade(ticker, forecast, strike, gap, price, qty, action):
    file_exists = os.path.isfile("trade_log.csv")
    try:
        with open("trade_log.csv", "a", newline="") as f:
            writer = csv.writer(f)
            if not file_exists:
                writer.writerow(["Date", "Ticker", "Forecast", "Strike", "Gap", "Price", "Qty", "Action"])
            writer.writerow([
                datetime.now().strftime("%Y-%m-%d %H:%M"),
                ticker, forecast, strike, f"{gap:.1f}", price, qty, action
            ])
    except Exception as e:
        print(f"CSV Log Error: {e}")

def get_open_meteo_forecast(lat, lon):
    url = f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}&daily=temperature_2m_max&temperature_unit=fahrenheit&timezone=auto"
    try:
        res = requests.get(url).json()
        return res['daily']['temperature_2m_max'][0]
    except Exception as e:
        print(f"⚠️ OpenMeteo Error: {e}")
        return None

def get_nws_forecast(lat, lon):
    headers = {'User-Agent': '(KalshiWeatherBot, contact@example.com)'}
    try:
        point_url = f"https://api.weather.gov/points/{lat},{lon}"
        point_res = requests.get(point_url, headers=headers).json()
        if 'status' in point_res and point_res['status'] >= 400:
            return None
            
        forecast_url = point_res['properties']['forecast']
        grid_res = requests.get(forecast_url, headers=headers).json()
        periods = grid_res['properties']['periods']
        
        for p in periods:
            if p['isDaytime']:
                return p['temperature']
        return None
    except Exception as e:
        print(f"⚠️ NWS Error: {e}")
        return None

def check_for_profit_taking(portfolio_api, market_api):
    print("--- Checking Profit Taking ---")
    try:
        positions = portfolio_api.get_positions().market_positions
        for pos in positions:
            if pos.position > 0:
                orderbook = market_api.get_market_orderbook(pos.ticker)
                if not orderbook.orderbook.yes: continue
                best_bid = orderbook.orderbook.yes[0][0]
                
                if best_bid >= PROFIT_TAKE_PRICE:
                    print(f"💰 SELLING {pos.ticker} @ {best_bid}¢")
                    portfolio_api.create_order(CreateOrderRequest(
                        ticker=pos.ticker, action="sell", side="yes",
                        count=pos.position, type="limit", yes_price=best_bid,
                        client_order_id=str(uuid.uuid4())
                    ))
                    log_trade(pos.ticker, "N/A", "N/A", 0, best_bid, pos.position, "SELL_PROFIT")
                    send_discord_alert(f"💰 **Profit Taken!** Sold {pos.position}x {pos.ticker} at **{best_bid}¢**")
    except Exception as e:
        print(f"Profit Taking Skip: {e}")

def main():
    print("🚀 Bot Starting...")

    # 0. Kill Switch
    if os.getenv("TRADING_ENABLED", "TRUE").upper() == "FALSE":
        print("🛑 Trading DISABLED via Env Var.")
        return

    # 1. Credentials
    api_key_id = os.getenv("KALSHI_KEY")
    private_key_pem = os.getenv("KALSHI_PRIVATE_KEY")
    
    if not api_key_id or not private_key_pem: 
        print("❌ CRITICAL: Missing API Keys in Secrets!")
        return

    # --- 🛠️ KEY REPAIR STATION 🛠️ ---
    # This automatically fixes keys broken by GitHub Secrets formatting
    if private_key_pem:
        # Replace literal "\n" characters with actual newlines
        private_key_pem = private_key_pem.replace('\\n', '\n')
        
        # Ensure headers have correct spacing
        if "-----BEGIN RSA PRIVATE KEY-----" in private_key_pem:
             private_key_pem = private_key_pem.replace("-----BEGIN RSA PRIVATE KEY----- ", "-----BEGIN RSA PRIVATE KEY-----\n")
             private_key_pem = private_key_pem.replace("-----BEGIN RSA PRIVATE KEY-----", "-----BEGIN RSA PRIVATE KEY-----\n")
             # Fix double newlines if we added too many
             private_key_pem = private_key_pem.replace("-----\n\n", "-----\n")

        if "-----END RSA PRIVATE KEY-----" in private_key_pem:
             private_key_pem = private_key_pem.replace(" -----END RSA PRIVATE KEY-----", "\n-----END RSA PRIVATE KEY-----")
             private_key_pem = private_key_pem.replace("-----END RSA PRIVATE KEY-----", "\n-----END RSA PRIVATE KEY-----")
             # Fix double newlines
             private_key_pem = private_key_pem.replace("\n\n-----", "\n-----")
    # ------------------------------------

    try:
        # Using the Elections URL which was resolving correctly for you
        config = kalshi_python.Configuration(host="https://api.elections.kalshi.com/trade-api/v2")
        config.api_key_id = api_key_id
        config.private_key_pem = private_key_pem
        
        api_client = kalshi_python.ApiClient(config)
        portfolio_api = kalshi_python.PortfolioApi(api_client)
        market_api = kalshi_python.MarketsApi(api_client)
    except Exception as e:
        print(f"❌ API Setup Error: {e}")
        return

    # 2. Risk Checks (Global)
    print("💳 Checking Balance...")
    try:
        balance_data = portfolio_api.get_balance()
        balance = balance_data.balance
        print(f"✅ Balance: {balance}¢")
        if balance < MIN_BALANCE_CENTS:
            print(f"🛑 Balance Low: {balance}¢. Stopping.")
            return
        check_for_profit_taking(portfolio_api, market_api)
    except Exception as e: 
        print(f"❌ CRITICAL ERROR in Risk Checks: {e}")
        print("💡 TIP: If this is still 401, verify your KALSHI_KEY is the UUID (e.g., 123-abc) and NOT the file name.")
        return

    # --- MAIN CITY LOOP ---
    print(f"--- Starting Scan of {len(CITIES)} Cities ---")
    
    for city in CITIES:
        print(f"\n🔎 Analyzing {city['name']} ({city['ticker']})...")
        
        om_temp = get_open_meteo_forecast(city['lat'], city['lon'])
        nws_temp = get_nws_forecast(city['lat'], city['lon'])
        
        print(f"   Forecasts: OM {om_temp}° | NWS {nws_temp}°")
        
        if not om_temp or not nws_temp:
            print("   Skipping (Data Missing)")
            continue

        safe_forecast = min(om_temp, nws_temp)
        
        try:
            markets_res = market_api.get_markets(series_ticker=city['ticker'], status="open")
        except Exception as e:
            print(f"   ⚠️ Market Fetch Error: {e}")
            continue

        if not markets_res.markets:
            print("   No active markets found.")
            continue

        for market in markets_res.markets:
            try:
                strike = float(market.ticker.split('-T')[-1])
            except ValueError: continue

            gap = safe_forecast - strike
            
            if gap >= 2.0:
                if gap >= 5.0: qty, label = HIGH_CONF_COUNT, "HIGH"
                elif gap >= 3.0: qty, label = MED_CONF_COUNT, "MED"
                else: qty, label = LOW_CONF_COUNT, "LOW"
                
                try:
                    pos_res = portfolio_api.get_positions()
                    curr_pos = next((p.position for p in pos_res.market_positions if p.ticker == market.ticker), 0)
                except: curr_pos = 0
                
                qty = min(qty, MAX_TOTAL_POS - curr_pos)
                if qty <= 0: continue

                try:
                    orderbook = market_api.get_market_orderbook(market.ticker)
                    if not orderbook.orderbook.no: continue
                    best_no_bid = orderbook.orderbook.no[0][0]
                    buy_yes_price = 100 - best_no_bid
                except: continue
                
                if buy_yes_price < MIN_PRICE or buy_yes_price > MAX_PRICE: continue

                target_price_limit = 75 - FEE_BUFFER 
                if buy_yes_price < target_price_limit:
                    print(f"   🚀 EXECUTE: Buying {qty}x {market.ticker} @ {buy_yes_price}¢")
                    try:
                        portfolio_api.create_order(CreateOrderRequest(
                            ticker=market.ticker, action="buy", side="yes",
                            count=qty, type="limit", yes_price=buy_yes_price,
                            client_order_id=str(uuid.uuid4())
                        ))
                        log_trade(market.ticker, safe_forecast, strike, gap, buy_yes_price, qty, "BUY")
                        msg = (f"**Kalshi Bot Trade ({city['name']})** 🌎\n"
                               f"Strategy: **{label}** (Gap {gap:.1f}°)\n"
                               f"Bought: **{qty}x {market.ticker}** @ {buy_yes_price}¢\n"
                               f"Forecasts: NWS {nws_temp}° / OM {om_temp}°")
                        send_discord_alert(msg)
                    except Exception as e:
                        print(f"   Order Fail: {e}")

if __name__ == "__main__":
    main()

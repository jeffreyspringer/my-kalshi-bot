import os
import uuid
import requests
import time
import kalshi_python
from kalshi_python.models import *

# --- CONFIGURATION ---
# UPDATED: Louis Armstrong New Orleans Intl (KMSY)
# This is the official settlement station for Kalshi NOLA markets.
NOLA_LAT, NOLA_LON = 29.99, -90.25
SERIES_TICKER = "KXHIGHTNOLA"

# RISK & STRATEGY
MIN_BALANCE_CENTS = 500     # Stop if balance < $5.00
MAX_TOTAL_POS = 20          # Max contracts to hold per market (Stop Loss)
PROFIT_TAKE_PRICE = 92      # Sell automatically if price hits 92¢

# CONFIDENCE SCALING (Forecast - Strike = Gap)
LOW_CONF_COUNT = 1          # Gap > 2.0°
MED_CONF_COUNT = 3          # Gap > 3.0°
HIGH_CONF_COUNT = 10        # Gap > 5.0°

def send_discord_alert(message):
    webhook_url = os.getenv("DISCORD_WEBHOOK_URL")
    if not webhook_url: return
    try:
        requests.post(webhook_url, json={"content": message})
    except Exception as e:
        print(f"Discord Error: {e}")

def get_open_meteo_forecast():
    """Source 1: Open-Meteo (Fast, simple)"""
    url = f"https://api.open-meteo.com/v1/forecast?latitude={NOLA_LAT}&longitude={NOLA_LON}&daily=temperature_2m_max&temperature_unit=fahrenheit&timezone=auto"
    try:
        res = requests.get(url).json()
        return res['daily']['temperature_2m_max'][0]
    except Exception as e:
        print(f"OpenMeteo Error: {e}")
        return None

def get_nws_forecast():
    """Source 2: National Weather Service (Official Settlement Source)"""
    headers = {'User-Agent': '(KalshiWeatherBot, contact@example.com)'}
    
    try:
        # Step 1: Get the Gridpoint for these NEW coordinates (KMSY)
        point_url = f"https://api.weather.gov/points/{NOLA_LAT},{NOLA_LON}"
        point_res = requests.get(point_url, headers=headers).json()
        forecast_url = point_res['properties']['forecast']
        
        # Step 2: Get the Forecast from that Gridpoint
        grid_res = requests.get(forecast_url, headers=headers).json()
        periods = grid_res['properties']['periods']
        
        for p in periods:
            if p['isDaytime']:
                return p['temperature']
        return None
    except Exception as e:
        print(f"NWS API Error: {e}")
        return None

def check_for_profit_taking(portfolio_api, market_api):
    """Scans portfolio and sells if we are winning big."""
    print("--- Checking Profit Taking ---")
    try:
        positions = portfolio_api.get_positions().market_positions
        for pos in positions:
            if pos.position > 0:
                orderbook = market_api.get_market_orderbook(pos.ticker)
                if not orderbook.orderbook.yes: continue
                
                best_bid = orderbook.orderbook.yes[0][0]
                
                if best_bid >= PROFIT_TAKE_PRICE:
                    print(f"💰 CASHING OUT {pos.ticker} @ {best_bid}¢")
                    market_api.create_order(CreateOrderRequest(
                        ticker=pos.ticker, action="sell", side="yes",
                        count=pos.position, type="limit", yes_price=best_bid,
                        client_order_id=str(uuid.uuid4())
                    ))
                    send_discord_alert(f"💰 **Profit Taken!** Sold {pos.position}x {pos.ticker} at **{best_bid}¢**")
    except Exception as e:
        print(f"Profit Taking Error: {e}")

def main():
    # 1. Credentials
    api_key_id = os.getenv("KALSHI_KEY")
    private_key_pem = os.getenv("KALSHI_PRIVATE_KEY")
    
    if not api_key_id or not private_key_pem:
        print("Missing Secrets.")
        return

    config = kalshi_python.Configuration(host="https://api.elections.kalshi.com/trade-api/v2")
    config.api_key_id = api_key_id
    config.private_key_pem = private_key_pem
    
    api_client = kalshi_python.ApiClient(config)
    portfolio_api = kalshi_python.PortfolioApi(api_client)
    market_api = kalshi_python.MarketApi(api_client)

    # 2. Risk Checks
    try:
        balance = portfolio_api.get_balance().balance
        if balance < MIN_BALANCE_CENTS:
            print(f"Balance Low: {balance}¢")
            return
        check_for_profit_taking(portfolio_api, market_api)
    except Exception as e:
        print(f"Init Error: {e}")
        return

    # 3. The "Ensemble" Forecast
    om_temp = get_open_meteo_forecast()
    nws_temp = get_nws_forecast()
    
    print(f"Forecasts (KMSY) -> OpenMeteo: {om_temp}°F | NWS: {nws_temp}°F")
    
    if not om_temp or not nws_temp:
        print("Weather source failed. Skipping.")
        return

    safe_forecast = min(om_temp, nws_temp)
    print(f"--- Trading based on Safe Forecast: {safe_forecast}°F ---")

    # 4. Market Scan
    markets_res = market_api.get_markets(series_ticker=SERIES_TICKER, status="open")
    if not markets_res.markets: return

    for market in markets_res.markets:
        try:
            strike = float(market.ticker.split('-T')[-1])
        except ValueError: continue

        gap = safe_forecast - strike
        
        if gap >= 2.0:
            if gap >= 5.0: qty, label = HIGH_CONF_COUNT, "HIGH"
            elif gap >= 3.0: qty, label = MED_CONF_COUNT, "MED"
            else: qty, label = LOW_CONF_COUNT, "LOW"
            
            pos_res = portfolio_api.get_positions()
            curr_pos = next((p.position for p in pos_res.market_positions if p.ticker == market.ticker), 0)
            
            qty = min(qty, MAX_TOTAL_POS - curr_pos)
            if qty <= 0: continue

            orderbook = market_api.get_market_orderbook(market.ticker)
            if not orderbook.orderbook.no: continue
                
            best_no_bid = orderbook.orderbook.no[0][0]
            buy_yes_price = 100 - best_no_bid
            
            if buy_yes_price < 75:
                print(f"Buying {qty}x {market.ticker} @ {buy_yes_price}¢")
                try:
                    market_api.create_order(CreateOrderRequest(
                        ticker=market.ticker, action="buy", side="yes",
                        count=qty, type="limit", yes_price=buy_yes_price,
                        client_order_id=str(uuid.uuid4())
                    ))
                    
                    msg = (f"**Kalshi Bot Trade (KMSY)** 🦅\n"
                           f"Strategy: **{label}** Confidence (Gap {gap:.1f}°)\n"
                           f"Bought: **{qty}x {market.ticker}** @ {buy_yes_price}¢\n"
                           f"Forecasts: NWS {nws_temp}° / OM {om_temp}°")
                    send_discord_alert(msg)
                except Exception as e:
                    print(f"Order Fail: {e}")
                    send_discord_alert(f"⚠️ Trade Failed: {e}")

if __name__ == "__main__":
    main()

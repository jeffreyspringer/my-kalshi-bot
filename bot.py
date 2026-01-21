import os
import uuid
import requests
import kalshi_python
from kalshi_python.models import *

# --- CONFIGURATION ---
NOLA_LAT, NOLA_LON = 30.05, -90.03
SERIES_TICKER = "KXHIGHTNOLA"
MIN_BALANCE_CENTS = 500  # Stop if balance < $5.00

# DYNAMIC SIZING SETTINGS
# We compare our forecast to the market strike (Forecast - Strike = Gap)
LOW_CONF_COUNT = 1   # Gap of 2.0 - 2.9 degrees
MED_CONF_COUNT = 3   # Gap of 3.0 - 4.9 degrees
HIGH_CONF_COUNT = 10  # Gap of 5.0+ degrees
MAX_TOTAL_POS = 20   # Never hold more than 20 contracts total for one market

def get_forecast():
    url = f"https://api.open-meteo.com/v1/forecast?latitude={NOLA_LAT}&longitude={NOLA_LON}&daily=temperature_2m_max&temperature_unit=fahrenheit&timezone=auto"
    try:
        response = requests.get(url).json()
        return response['daily']['temperature_2m_max'][0]
    except Exception: return None

def main():
    # 1. Setup
    api_key_id = os.getenv("KALSHI_KEY")
    private_key_pem = os.getenv("KALSHI_PRIVATE_KEY")
    config = kalshi_python.Configuration(host="https://api.elections.kalshi.com/trade-api/v2")
    config.api_key_id = api_key_id
    config.private_key_pem = private_key_pem
    api_client = kalshi_python.ApiClient(config)
    
    portfolio_api = kalshi_python.PortfolioApi(api_client)
    market_api = kalshi_python.MarketApi(api_client)

    # 2. Balance Check
    balance = portfolio_api.get_balance().balance
    if balance < MIN_BALANCE_CENTS: return

    # 3. Forecast Logic
    forecast = get_forecast()
    if not forecast: return
    print(f"NOLA Forecast: {forecast}°F")

    # 4. Market Selection
    markets_res = market_api.get_markets(series_ticker=SERIES_TICKER, status="open")
    for market in markets_res.markets:
        strike = float(market.ticker.split('-T')[-1])
        gap = forecast - strike
        
        # Only trade if we have at least a 2-degree "cushion"
        if gap >= 2.0:
            # Determine how many to buy based on confidence gap
            if gap >= 5.0:
                trade_qty = HIGH_CONF_COUNT
            elif gap >= 3.0:
                trade_qty = MED_CONF_COUNT
            else:
                trade_qty = LOW_CONF_COUNT
            
            # 5. Check existing position
            pos_res = portfolio_api.get_positions()
            current_pos = next((p.position for p in pos_res.market_positions if p.ticker == market.ticker), 0)
            
            if current_pos >= MAX_TOTAL_POS: continue
            
            # Ensure we don't exceed MAX_TOTAL_POS with this new order
            trade_qty = min(trade_qty, MAX_TOTAL_POS - current_pos)
            if trade_qty <= 0: continue

            # 6. Price Check & Execute
            orderbook = market_api.get_market_orderbook(market.ticker)
            if not orderbook.orderbook.no: continue
            yes_price = orderbook.orderbook.no[0][0]

            if yes_price < 70:
                print(f"Gap is {gap}°. Scaling up: buying {trade_qty} contracts at {yes_price}c")
                market_api.create_order(CreateOrderRequest(
                    ticker=market.ticker, action="buy", side="yes",
                    count=trade_qty, type="limit", yes_price=yes_price,
                    client_order_id=str(uuid.uuid4())
                ))

if __name__ == "__main__":
    main()

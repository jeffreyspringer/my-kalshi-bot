import os
import uuid
import requests
import kalshi_python
from kalshi_python.models import *

# --- CONFIGURATION ---
NOLA_LAT, NOLA_LON = 30.05, -90.03
SERIES_TICKER = "KXHIGHTNOLA" 

def get_forecast():
    url = f"https://api.open-meteo.com/v1/forecast?latitude={NOLA_LAT}&longitude={NOLA_LON}&daily=temperature_2m_max&temperature_unit=fahrenheit&timezone=auto"
    try:
        response = requests.get(url).json()
        return response['daily']['temperature_2m_max'][0]
    except Exception as e:
        print(f"Error fetching weather: {e}")
        return None

def main():
    # 1. Credentials
    api_key_id = os.getenv("KALSHI_KEY")
    private_key_pem = os.getenv("KALSHI_PRIVATE_KEY")
    
    config = kalshi_python.Configuration(host="https://api.elections.kalshi.com/trade-api/v2")
    config.api_key_id = api_key_id
    config.private_key_pem = private_key_pem
    
    api_client = kalshi_python.ApiClient(config)
    kalshi_api = kalshi_python.MarketApi(api_client)

    # 2. Get Data
    forecast = get_forecast()
    if not forecast: return
    print(f"--- NOLA BOT RUN: Forecast {forecast}°F ---")

    # 3. Find Market
    markets_res = kalshi_api.get_markets(series_ticker=SERIES_TICKER, status="open")
    if not markets_res.markets:
        print("No open markets found.")
        return

    for market in markets_res.markets:
        strike = float(market.ticker.split('-T')[-1])
        
        # If forecast is 3 degrees higher than strike, it's a strong 'YES' candidate
        if forecast >= (strike + 3):
            print(f"Target found: {market.ticker}")
            
            # Check price and "Spread"
            orderbook = kalshi_api.get_market_orderbook(market.ticker)
            
            # price to buy YES is the 'No' ask side
            if not orderbook.orderbook.no:
                print("No liquidity (nobody is selling). Skipping.")
                continue
                
            yes_price = orderbook.orderbook.no[0][0]
            
            # LOGIC: Buy if probability is high (forecast > strike) 
            # and price is relatively cheap (< 65 cents)
            if yes_price < 65:
                print(f"Price is {yes_price}c. Placing Buy Order.")
                kalshi_api.create_order(CreateOrderRequest(
                    ticker=market.ticker,
                    action="buy",
                    side="yes",
                    count=1,
                    type="limit",
                    yes_price=yes_price,
                    client_order_id=str(uuid.uuid4())
                ))
            else:
                print(f"Price too high ({yes_price}c). No edge.")

if __name__ == "__main__":
    main()

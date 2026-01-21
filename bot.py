import os
import uuid
import requests
import kalshi_python
from kalshi_python.models import *

# --- CONFIGURATION ---
NOLA_LAT, NOLA_LON = 30.05, -90.03
SERIES_TICKER = "KXHIGHTNOLA"
MIN_BALANCE_CENTS = 500  # Stop trading if balance is below $5.00
MAX_CONTRACTS_PER_MARKET = 5 # Don't own more than 5 of the same contract

def get_forecast():
    """Fetches forecasted high for NOLA Lakefront Airport."""
    url = f"https://api.open-meteo.com/v1/forecast?latitude={NOLA_LAT}&longitude={NOLA_LON}&daily=temperature_2m_max&temperature_unit=fahrenheit&timezone=auto"
    try:
        response = requests.get(url).json()
        return response['daily']['temperature_2m_max'][0]
    except Exception as e:
        print(f"Weather API Error: {e}")
        return None

def main():
    # 1. Credentials from GitHub Secrets
    api_key_id = os.getenv("KALSHI_KEY")
    private_key_pem = os.getenv("KALSHI_PRIVATE_KEY")
    
    config = kalshi_python.Configuration(host="https://api.elections.kalshi.com/trade-api/v2")
    config.api_key_id = api_key_id
    config.private_key_pem = private_key_pem
    
    api_client = kalshi_python.ApiClient(config)
    
    # We use both Portfolio and Market APIs
    portfolio_api = kalshi_python.PortfolioApi(api_client)
    market_api = kalshi_python.MarketApi(api_client)

    # 2. Safety Check: Balance
    try:
        balance_res = portfolio_api.get_balance()
        balance = balance_res.balance
        print(f"Current Balance: ${balance/100:.2f}")
        if balance < MIN_BALANCE_CENTS:
            print("Balance too low. Stopping.")
            return
    except Exception as e:
        print(f"Could not fetch balance: {e}")
        return

    # 3. Get Weather Data
    forecast = get_forecast()
    if not forecast: return
    print(f"Forecast for NOLA: {forecast}°F")

    # 4. Find Best Market
    markets_res = market_api.get_markets(series_ticker=SERIES_TICKER, status="open")
    if not markets_res.markets:
        print("No active markets.")
        return

    for market in markets_res.markets:
        strike = float(market.ticker.split('-T')[-1])
        
        # LOGIC: Forecast is at least 2 degrees above strike
        if forecast >= (strike + 2):
            ticker = market.ticker
            print(f"Found Edge on {ticker}")

            # 5. Check if we already own too much of this ticker
            positions_res = portfolio_api.get_positions()
            current_pos = 0
            for p in positions_res.market_positions:
                if p.ticker == ticker:
                    current_pos = p.position
                    break
            
            if current_pos >= MAX_CONTRACTS_PER_MARKET:
                print(f"Already holding {current_pos} contracts. Skipping.")
                continue

            # 6. Check Orderbook for Price
            orderbook = market_api.get_market_orderbook(ticker)
            if not orderbook.orderbook.no:
                print("No sellers available.")
                continue
                
            yes_price = orderbook.orderbook.no[0][0]
            
            # Final buy condition: Forecast is strong AND price is < 75 cents
            if yes_price < 75:
                print(f"Executing Buy at {yes_price}c...")
                market_api.create_order(CreateOrderRequest(
                    ticker=ticker,
                    action="buy",
                    side="yes",
                    count=1,
                    type="limit",
                    yes_price=yes_price,
                    client_order_id=str(uuid.uuid4())
                ))
            else:
                print(f"Price too high ({yes_price}c).")

if __name__ == "__main__":
    main()

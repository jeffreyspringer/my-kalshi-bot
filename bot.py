import os
import uuid
import requests
import kalshi_python
from kalshi_python.models import *

# --- CONFIGURATION ---
# New Orleans Lakefront Airport (KNEW) coordinates
NOLA_LAT, NOLA_LON = 30.05, -90.03
SERIES_TICKER = "KXHIGHTNOLA"  # Kalshi's series for NOLA high temps

def get_forecast():
    """Fetches tomorrow's high temp for NOLA (Lakefront Airport) from Open-Meteo."""
    url = f"https://api.open-meteo.com/v1/forecast?latitude={NOLA_LAT}&longitude={NOLA_LON}&daily=temperature_2m_max&temperature_unit=fahrenheit&timezone=auto"
    try:
        response = requests.get(url).json()
        high_temp = response['daily']['temperature_2m_max'][0]
        return high_temp
    except Exception as e:
        print(f"Error fetching weather: {e}")
        return None

def find_best_market(kalshi_api, forecast_temp):
    """Finds the specific market ticker for today's high temp."""
    # Get all active markets in the New Orleans High Temp series
    markets_res = kalshi_api.get_markets(series_ticker=SERIES_TICKER, status="open")
    
    if not markets_res.markets:
        print("No active NOLA weather markets found.")
        return None, None

    # Logic: Find a market where our forecast is safely above the strike price
    # Example strike: 'High will be above 80'
    for market in markets_res.markets:
        # Ticker format usually ends in Txx (e.g., T80 for 80 degrees)
        strike_temp = float(market.ticker.split('-T')[-1])
        
        # If forecast is 3+ degrees above the strike, we consider it a 'Yes' bet
        if forecast_temp >= (strike_temp + 2):
            return market.ticker, strike_temp
            
    return None, None

def main():
    # 1. Load Credentials from GitHub Secrets
    api_key_id = os.getenv("KALSHI_KEY")
    private_key_pem = os.getenv("KALSHI_PRIVATE_KEY")

    if not api_key_id or not private_key_pem:
        print("Missing API credentials. Check GitHub Secrets.")
        return

    # 2. Setup Kalshi Client
    config = kalshi_python.Configuration(host="https://api.elections.kalshi.com/trade-api/v2")
    config.api_key_id = api_key_id
    config.private_key_pem = private_key_pem
    
    api_client = kalshi_python.ApiClient(config)
    kalshi_api = kalshi_python.MarketApi(api_client)

    # 3. Get Weather & Market
    forecast = get_forecast()
    if forecast is None: return
    print(f"Current NOLA Forecasted High: {forecast}°F")

    ticker, strike = find_best_market(kalshi_api, forecast)
    
    if ticker:
        print(f"Evaluating Market: {ticker} (Strike: {strike}°F)")
        
        # Check current price
        orderbook = kalshi_api.get_market_orderbook(ticker)
        # Get the best price to buy 'Yes' (the lowest 'No' ask)
        # Kalshi prices are in cents (1-99)
        yes_price = orderbook.orderbook.no[0][0] if orderbook.orderbook.no else 99
        
        print(f"Current price for YES: {yes_price} cents")

        # 4. Strategy: Buy if forecast is high but price is < 70 cents
        if yes_price < 70:
            print("Edge detected. Placing order...")
            order_id = str(uuid.uuid4())
            try:
                order_res = kalshi_api.create_order(CreateOrderRequest(
                    ticker=ticker,
                    action="buy",
                    side="yes",
                    count=1, # Start with 1 contract to test
                    type="limit",
                    yes_price=yes_price, 
                    client_order_id=order_id
                ))
                print(f"Order Success! ID: {order_res.order.order_id}")
            except Exception as e:
                print(f"Order failed: {e}")
        else:
            print("Price too high, skipping trade.")
    else:
        print("No suitable market found for current forecast.")

if __name__ == "__main__":
    main()

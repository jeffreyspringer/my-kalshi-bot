import os
import uuid
import requests
import kalshi_python
from kalshi_python.models import *

# --- CONFIGURATION ---
# New Orleans Lakefront Airport (KNEW)
NOLA_LAT, NOLA_LON = 30.05, -90.03
SERIES_TICKER = "KXHIGHTNOLA"

# RISK MANAGEMENT
MIN_BALANCE_CENTS = 500   # Stop trading if balance < $5.00
MAX_TOTAL_POS = 20        # Max contracts to hold per market (Stop-loss/Risk limit)

# CONFIDENCE SCALING (Forecast - Strike = Gap)
LOW_CONF_COUNT = 1        # Gap > 2.0°
MED_CONF_COUNT = 3        # Gap > 3.0°
HIGH_CONF_COUNT = 10      # Gap > 5.0°

def send_discord_alert(message):
    """Sends a notification to your Discord channel."""
    webhook_url = os.getenv("DISCORD_WEBHOOK_URL")
    if not webhook_url:
        print("No Discord Webhook found. Skipping alert.")
        return

    payload = {"content": message}
    try:
        requests.post(webhook_url, json=payload)
    except Exception as e:
        print(f"Failed to send Discord alert: {e}")

def get_forecast():
    """Fetches forecasted high for NOLA Lakefront Airport."""
    url = f"https://api.open-meteo.com/v1/forecast?latitude={NOLA_LAT}&longitude={NOLA_LON}&daily=temperature_2m_max&temperature_unit=fahrenheit&timezone=auto"
    try:
        response = requests.get(url).json()
        temp = response['daily']['temperature_2m_max'][0]
        return temp
    except Exception as e:
        print(f"Weather API Error: {e}")
        return None

def main():
    # 1. Credentials from GitHub Secrets
    api_key_id = os.getenv("KALSHI_KEY")
    private_key_pem = os.getenv("KALSHI_PRIVATE_KEY")
    
    if not api_key_id or not private_key_pem:
        print("Error: Missing Kalshi Credentials in GitHub Secrets.")
        return

    config = kalshi_python.Configuration(host="https://api.elections.kalshi.com/trade-api/v2")
    config.api_key_id = api_key_id
    config.private_key_pem = private_key_pem
    
    api_client = kalshi_python.ApiClient(config)
    portfolio_api = kalshi_python.PortfolioApi(api_client)
    market_api = kalshi_python.MarketApi(api_client)

    # 2. Safety Check: Balance
    try:
        balance_res = portfolio_api.get_balance()
        balance = balance_res.balance
        if balance < MIN_BALANCE_CENTS:
            print(f"Balance too low ({balance}¢). Stopping.")
            return
    except Exception as e:
        print(f"Could not fetch balance: {e}")
        return

    # 3. Get Forecast
    forecast = get_forecast()
    if not forecast: return
    print(f"--- NOLA Forecast: {forecast}°F ---")

    # 4. Find Active Markets
    markets_res = market_api.get_markets(series_ticker=SERIES_TICKER, status="open")
    if not markets_res.markets:
        print("No active markets found.")
        return

    for market in markets_res.markets:
        # Extract strike temp from ticker (e.g. "KXHIGHTNOLA-26JAN21-T80" -> 80.0)
        try:
            strike = float(market.ticker.split('-T')[-1])
        except ValueError:
            continue # Skip if ticker format is weird

        gap = forecast - strike
        
        # LOGIC: Only trade if we have at least a 2-degree "cushion"
        if gap >= 2.0:
            # Determine Size based on Confidence
            if gap >= 5.0:
                trade_qty, label = HIGH_CONF_COUNT, "HIGH"
            elif gap >= 3.0:
                trade_qty, label = MED_CONF_COUNT, "MEDIUM"
            else:
                trade_qty, label = LOW_CONF_COUNT, "LOW"
            
            # 5. Portfolio Check (Don't overbuy)
            pos_res = portfolio_api.get_positions()
            current_pos = next((p.position for p in pos_res.market_positions if p.ticker == market.ticker), 0)
            
            if current_pos >= MAX_TOTAL_POS:
                print(f"Max position reached for {market.ticker}. Skipping.")
                continue
            
            # Reduce buy amount if we are near the limit
            trade_qty = min(trade_qty, MAX_TOTAL_POS - current_pos)
            if trade_qty <= 0: continue

            # 6. Pricing Logic (The "Mirror" Fix)
            orderbook = market_api.get_market_orderbook(market.ticker)
            
            # We need someone buying "No" to sell us "Yes"
            if not orderbook.orderbook.no:
                print(f"No liquidity on {market.ticker} (No sellers).")
                continue
                
            best_no_bid = orderbook.orderbook.no[0][0]
            buy_yes_price = 100 - best_no_bid
            
            # Final Check: Is the price good? (< 75 cents)
            if buy_yes_price < 75:
                print(f"Edge Found! Buying {trade_qty}x {market.ticker} @ {buy_yes_price}¢")
                
                try:
                    # EXECUTE TRADE
                    market_api.create_order(CreateOrderRequest(
                        ticker=market.ticker,
                        action="buy",
                        side="yes",
                        count=trade_qty,
                        type="limit",
                        yes_price=buy_yes_price,
                        client_order_id=str(uuid.uuid4())
                    ))
                    
                    # SEND DISCORD ALERT
                    msg = (f"**Kalshi Bot Alert** 🚨\n"
                           f"Bought **{trade_qty}x** {market.ticker}\n"
                           f"Confidence: **{label}** (Gap: {gap:.1f}°)\n"
                           f"Price: **{buy_yes_price}¢** | Forecast: **{forecast}°F**")
                    send_discord_alert(msg)
                    
                except Exception as e:
                    print(f"Order failed: {e}")
                    send_discord_alert(f"⚠️ Bot tried to buy {market.ticker} but failed: {e}")
            else:
                print(f"Price too high ({buy_yes_price}¢) for {market.ticker}. Gap: {gap}°")

if __name__ == "__main__":
    main()

import os

# Instead of hardcoding paths, read from the GitHub Environment
api_key_id = os.getenv("KALSHI_KEY")
private_key_contents = os.getenv("KALSHI_PRIVATE_KEY")

# Now use these to configure the Kalshi API
config = kalshi_python.Configuration(host="https://api.elections.kalshi.com/trade-api/v2")
config.api_key_id = api_key_id
config.private_key_pem = private_key_contents

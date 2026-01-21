import requests
import os

# --- INSTRUCTIONS ---
# If running locally on your computer, paste your Discord Webhook URL below:
# WEBHOOK_URL = "https://discord.com/api/webhooks/..."

# If running on GitHub Actions, leave this alone (it reads the Secret):
WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL")

def send_test_message():
    if not WEBHOOK_URL:
        print("❌ Error: No Discord Webhook URL found.")
        print("If running locally, uncomment the line 'WEBHOOK_URL = ...' and paste your link.")
        return

    payload = {
        "content": "✅ **Success!** Your Kalshi Bot is correctly connected to Discord.",
        "username": "Kalshi Weather Bot"
    }

    try:
        response = requests.post(WEBHOOK_URL, json=payload)
        
        if 200 <= response.status_code < 300:
            print("✅ Message sent successfully! Check your Discord channel.")
        else:
            print(f"⚠️ Failed with status code: {response.status_code}")
            print(f"Response: {response.text}")
            
    except Exception as e:
        print(f"❌ Connection error: {e}")

if __name__ == "__main__":
    send_test_message()

import os
import base64
import requests
import time
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.serialization import load_pem_private_key

def main():
    print("🕵️ STARTING RAW KEY DIAGNOSTIC...")
    
    # 1. Load Keys
    key_id = os.getenv("KALSHI_KEY", "").strip()
    private_key_str = os.getenv("KALSHI_PRIVATE_KEY", "").strip()
    
    # Key Repair (Same as bot.py)
    if "\\n" in private_key_str: private_key_str = private_key_str.replace('\\n', '\n')
    if "-----BEGIN" not in private_key_str: 
        private_key_str = "-----BEGIN RSA PRIVATE KEY-----\n" + private_key_str + "\n-----END RSA PRIVATE KEY-----"

    print(f"🔑 Key ID: {key_id[:4]}...{key_id[-4:]}")
    
    try:
        private_key = load_pem_private_key(private_key_str.encode(), password=None)
        print("✅ Private Key Loaded Successfully.")
    except Exception as e:
        print(f"❌ Private Key Load Failed: {e}")
        return

    # 2. Define Request Details
    host = "https://api.elections.kalshi.com"
    path = "/trade-api/v2/portfolio/balance"
    method = "GET"
    timestamp = str(int(time.time() * 1000)) # Milliseconds

    # 3. Create Signature (RSA-PSS)
    # Docs: timestamp + method + path (no query params)
    msg = f"{timestamp}{method}{path}".encode('utf-8')
    
    try:
        signature = private_key.sign(
            msg,
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.MAX_LENGTH
            ),
            hashes.SHA256()
        )
        sig_b64 = base64.b64encode(signature).decode('utf-8')
        print("✅ Signature Generated.")
    except Exception as e:
        print(f"❌ Signing Failed: {e}")
        return

    # 4. Send Request
    headers = {
        "KALSHI-API-KEY": key_id,
        "KALSHI-API-SIGNATURE": sig_b64,
        "KALSHI-API-TIMESTAMP": timestamp,
        "Content-Type": "application/json"
    }
    
    print(f"🚀 Sending Request to {host}{path}...")
    response = requests.get(f"{host}{path}", headers=headers)
    
    print(f"\n📡 RESPONSE CODE: {response.status_code}")
    print(f"📜 RESPONSE BODY: {response.text}")

    if response.status_code == 200:
        print("\n🎉 SUCCESS! The keys are working. The issue was the Python Library.")
    elif response.status_code == 401:
        print("\n💀 FAILURE: 401 Unauthorized.")
        print("👉 DIAGNOSIS: These keys are strictly invalid for this environment.")
        print("   1. Are these 'Demo' keys trying to work on 'Prod'?")
        print("   2. Was the Key ID copied from the wrong file?")

if __name__ == "__main__":
    main()

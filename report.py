import os
import json
import csv
import requests
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from datetime import datetime

# --- CONFIGURATION ---
STATS_FILE = "city_stats.json"
PORTFOLIO_FILE = "portfolio_history.csv"

# ✅ CITIES (Just for Names/Emojis)
CITIES = [
    { "name": "NOLA",    "emoji": "🎷" },
    { "name": "CHICAGO", "emoji": "🍕" },
    { "name": "MIAMI",   "emoji": "🌴" },
    { "name": "SEATTLE", "emoji": "☕" },
    { "name": "AUSTIN",  "emoji": "🎸" }
]

def generate_trend_graph():
    dates = []
    values = []
    
    if not os.path.exists(PORTFOLIO_FILE): return None
    
    try:
        with open(PORTFOLIO_FILE, 'r') as f:
            reader = csv.reader(f)
            next(reader, None) # Skip header
            for row in reader:
                dt = datetime.strptime(row[0], "%Y-%m-%d %H:%M")
                dates.append(dt)
                values.append(float(row[1]))
        
        if not values: return None

        fig, ax = plt.subplots(figsize=(10, 5))
        ax.plot_date(dates, values, linestyle='-', marker='o', color='#00d2be', linewidth=2, markersize=6)
        
        ax.set_title('Portfolio Value Trend', color='white', fontsize=14, pad=15)
        ax.set_ylabel('Value ($)', color='white', fontsize=12)
        ax.xaxis.set_major_formatter(mdates.DateFormatter('%b %d'))
        fig.autofmt_xdate()
        
        ax.set_facecolor('#2f3136')
        fig.patch.set_facecolor('#2f3136')
        ax.tick_params(axis='x', colors='white')
        ax.tick_params(axis='y', colors='white')
        ax.grid(color='#40444b', linestyle='--', alpha=0.5)
        
        ax.spines['top'].set_visible(False)
        ax.spines['right'].set_visible(False)
        ax.spines['bottom'].set_color('white')
        ax.spines['left'].set_color('white')
        
        filename = "chart.png"
        plt.savefig(filename, bbox_inches='tight', dpi=100)
        plt.close()
        return filename
    except Exception as e:
        print(f"Graph Error: {e}")
        return None

def send_daily_report():
    print("--- 📊 Generating CEO Report ---")
    
    webhook = os.getenv("DISCORD_REPORT_WEBHOOK_URL")
    if not webhook: 
        print("   ⚠️ No Report Webhook found.")
        return

    city_stats = {}
    if os.path.exists(STATS_FILE):
        with open(STATS_FILE, 'r') as f:
            city_stats = json.load(f)
            
    stats_text = ""
    for city in CITIES:
        pnl = city_stats.get(city['name'], 0)
        emoji_city = city['emoji']
        
        if pnl > 0:
            status_dot = "🟢"
            pnl_fmt = f"+${pnl/100:.2f}"
        elif pnl < 0:
            status_dot = "🔴"
            pnl_fmt = f"-${abs(pnl)/100:.2f}"
        else:
            status_dot = "⚪"
            pnl_fmt = "$0.00"
            
        stats_text += f"{status_dot} {emoji_city} **{city['name']}:** {pnl_fmt}\n"
        
    if not stats_text: stats_text = "No trades recorded yet."

    # Get latest portfolio value
    current_balance = 0
    if os.path.exists(PORTFOLIO_FILE):
        with open(PORTFOLIO_FILE, 'r') as f:
            for line in f: pass
            last_line = line.strip().split(',')
            try: current_balance = float(last_line[1])
            except: pass

    chart_file = generate_trend_graph()
    
    embed = {
        "title": "📊 Daily CEO Report",
        "description": f"**Account Value:** ${current_balance:.2f}\n\n**🌍 City Performance (All-Time):**\n{stats_text}",
        "color": 3447003, # Blue
        "image": {"url": "attachment://chart.png"},
        "timestamp": datetime.utcnow().isoformat()
    }

    files = {}
    if chart_file:
        files["file"] = (chart_file, open(chart_file, "rb"))
        
    requests.post(webhook, data={"payload_json": json.dumps({"embeds": [embed]})}, files=files)
    print("✅ CEO Report Sent.")

if __name__ == "__main__":
    send_daily_report()

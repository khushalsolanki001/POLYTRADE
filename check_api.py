import json
import urllib.request
from urllib.error import URLError
// This script checks the latest trades for a list of users directly from the Polymarket API.
def get_trades(user):
    url = f"https://data-api.polymarket.com/trades?user={user}&limit=5"
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req) as response:
            data = json.loads(response.read().decode('utf-8'))
            print(f"User {user} latest:")
            for t in data[:5]:
                ts = t.get("timestamp")
                side = t.get("side")
                size = t.get("size")
                print(f"   Unix ts: {ts} -> {side} {size}")
    except URLError as e:
        print(f"Error for {user}: {e.reason}")

print("Checking live trades directly from Polymarket API...")
// List of users to check


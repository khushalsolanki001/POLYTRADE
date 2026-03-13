"""Live check: show exactly what API returns vs DB cursor."""
import asyncio, aiohttp, sqlite3, datetime

async def main():
    conn = sqlite3.connect("polytrack.db")
    conn.row_factory = sqlite3.Row
    wallets = conn.execute(
        "SELECT nickname, wallet_address, last_timestamp FROM watched_wallets ORDER BY id"
    ).fetchall()
    conn.close()

    now_ts = int(datetime.datetime.now(datetime.timezone.utc).timestamp())
    print(f"Current time: {datetime.datetime.fromtimestamp(now_ts, tz=datetime.timezone.utc)}")
    print()

    async with aiohttp.ClientSession(headers={"User-Agent": "PolyTrack/1.0"}) as session:
        for w in wallets:
            addr = w["wallet_address"]
            last_ts = w["last_timestamp"]
            nick = w["nickname"]

            params = {"user": addr, "limit": "20", "sortBy": "TIMESTAMP", "sortDirection": "DESC"}
            async with session.get("https://data-api.polymarket.com/trades", params=params) as resp:
                trades = await resp.json(content_type=None)

            new_trades = [t for t in trades if t.get("timestamp", 0) > last_ts]
            cursor_dt = datetime.datetime.fromtimestamp(last_ts, tz=datetime.timezone.utc)

            print(f"[{nick}]")
            print(f"  cursor : {last_ts} ({cursor_dt.strftime('%H:%M UTC')})")
            print(f"  newest : {trades[0].get('timestamp') if trades else 'N/A'} ({datetime.datetime.fromtimestamp(trades[0].get('timestamp',0), tz=datetime.timezone.utc).strftime('%H:%M UTC') if trades else 'N/A'})")
            print(f"  NEW trades (ts > cursor): {len(new_trades)}")
            for t in new_trades:
                ts = t.get("timestamp", 0)
                dt_s = datetime.datetime.fromtimestamp(ts, tz=datetime.timezone.utc).strftime("%H:%M:%S UTC")
                print(f"    -> {ts} ({dt_s}) {t.get('side')} size={t.get('size')} price={t.get('price')}")
            print()

asyncio.run(main())

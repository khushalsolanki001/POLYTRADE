"""
reset_cursors.py  — One-time script to sync DB cursors to the latest
                     trade timestamps so the bot won't re-alert on missed
                     historical trades when it restarts.

Run ONCE before starting the bot:
    python reset_cursors.py
"""
import asyncio
import sqlite3
import datetime
import sys

sys.path.insert(0, ".")
import api


async def reset():
    conn = sqlite3.connect("polytrack.db")
    conn.row_factory = sqlite3.Row
    wallets = conn.execute(
        "SELECT id, nickname, wallet_address, last_timestamp FROM watched_wallets"
    ).fetchall()

    print(f"Resetting cursors for {len(wallets)} wallet(s)...\n")

    for w in wallets:
        wid   = w["id"]
        nick  = w["nickname"] or w["wallet_address"][:10]
        addr  = w["wallet_address"]
        old_ts = w["last_timestamp"]

        trades = await api.fetch_trades(addr, limit=5)
        if not trades:
            print(f"  [{nick}]  No trades returned — cursor unchanged ({old_ts})")
            continue

        newest_ts = max(api.parse_trade_timestamp(t) for t in trades)

        if newest_ts > old_ts:
            conn.execute(
                "UPDATE watched_wallets SET last_timestamp = ? WHERE id = ?",
                (newest_ts, wid),
            )
            dt = datetime.datetime.fromtimestamp(newest_ts, tz=datetime.timezone.utc)
            old_dt = datetime.datetime.fromtimestamp(old_ts, tz=datetime.timezone.utc)
            print(f"  [{nick}]  {old_ts} ({old_dt.strftime('%Y-%m-%d %H:%M UTC')}) -> {newest_ts} ({dt.strftime('%Y-%m-%d %H:%M UTC')})")
        else:
            print(f"  [{nick}]  Already up-to-date ({old_ts})")

    conn.commit()
    conn.close()
    await api.close_session()
    print("\nDone. Start the bot now: python bot.py")


asyncio.run(reset())

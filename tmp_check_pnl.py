import sqlite3
import os

DB_PATH = "polytrack.db"

def check():
    if not os.path.exists(DB_PATH):
        print(f"File {DB_PATH} DOES NOT EXIST")
        return
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    try:
        row = con.execute("SELECT SUM(pnl) as total_pnl FROM paper_trade_history").fetchone()
        print(f"Total PnL in history: {row['total_pnl']}")
        
        row_count = con.execute("SELECT COUNT(*) as cnt FROM paper_trade_history").fetchone()
        print(f"Total trades in history: {row_count['cnt']}")
        
        wins = con.execute("SELECT COUNT(*) as cnt FROM paper_trade_history WHERE pnl > 0").fetchone()
        losses = con.execute("SELECT COUNT(*) as cnt FROM paper_trade_history WHERE pnl <= 0").fetchone()
        print(f"Wins: {wins['cnt']}, Losses: {losses['cnt']}")
        
        # Recent pnl (last 10 trades)
        recent_pnl = con.execute("SELECT SUM(pnl) as pnl FROM (SELECT pnl FROM paper_trade_history ORDER BY closed_at DESC LIMIT 10)").fetchone()
        print(f"PnL of last 10 trades: {recent_pnl['pnl']}")
        
    except Exception as e:
        print(f"Error: {e}")
    finally:
        con.close()

if __name__ == "__main__":
    check()

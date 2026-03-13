"""
db.py — SQLite persistence layer for PolyTrack Bot
===================================================
All database access goes through this module.  Tables are created
automatically on first run; no migration tooling needed.
"""

import sqlite3
import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)

# ── DB path comes from .env or defaults to project root ──────────────────────
DB_PATH: str = os.getenv("DB_PATH", "polytrack.db")


# ─────────────────────────────────────────────────────────────────────────────
#  Connection helper
# ─────────────────────────────────────────────────────────────────────────────

def _connect() -> sqlite3.Connection:
    """Return a new SQLite connection with WAL mode and row_factory set."""
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row          # rows behave like dicts
    conn.execute("PRAGMA journal_mode=WAL") # safe for concurrent readers
    conn.execute("PRAGMA foreign_keys=ON")  # enforce FK constraints
    return conn


# ─────────────────────────────────────────────────────────────────────────────
#  Schema initialisation
# ─────────────────────────────────────────────────────────────────────────────

def init_db() -> None:
    """Create tables if they don't exist yet.  Called once at bot startup."""
    conn = _connect()
    try:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                user_id    INTEGER PRIMARY KEY,
                username   TEXT,
                chat_id    INTEGER NOT NULL,
                created_at TEXT    NOT NULL DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS watched_wallets (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id           INTEGER NOT NULL REFERENCES users(user_id)
                                          ON DELETE CASCADE,
                wallet_address    TEXT    NOT NULL,
                nickname          TEXT,
                min_usd_threshold REAL    NOT NULL DEFAULT 0.0,
                only_buys         INTEGER NOT NULL DEFAULT 0,  -- bool: 0/1
                last_timestamp    INTEGER NOT NULL DEFAULT 0,  -- epoch seconds
                created_at        TEXT    NOT NULL DEFAULT (datetime('now')),
                UNIQUE (user_id, wallet_address)
            );
        """)
        conn.commit()
        logger.info("✅ Database initialised at '%s'", DB_PATH)
    finally:
        conn.close()


# ─────────────────────────────────────────────────────────────────────────────
#  User helpers
# ─────────────────────────────────────────────────────────────────────────────

def upsert_user(user_id: int, username: Optional[str], chat_id: int) -> None:
    """Insert or update a Telegram user record."""
    with _connect() as conn:
        conn.execute("""
            INSERT INTO users (user_id, username, chat_id)
            VALUES (?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                username = excluded.username,
                chat_id  = excluded.chat_id
        """, (user_id, username, chat_id))


# ─────────────────────────────────────────────────────────────────────────────
#  Wallet helpers
# ─────────────────────────────────────────────────────────────────────────────

def add_wallet(
    user_id: int,
    wallet_address: str,
    nickname: Optional[str],
    min_usd: float,
    only_buys: bool,
) -> bool:
    """
    Add a wallet to a user's watch list.
    Returns True on success, False if the wallet is already tracked.
    """
    try:
        with _connect() as conn:
            conn.execute("""
                INSERT INTO watched_wallets
                    (user_id, wallet_address, nickname, min_usd_threshold, only_buys)
                VALUES (?, ?, ?, ?, ?)
            """, (user_id, wallet_address.lower(), nickname, min_usd, int(only_buys)))
        return True
    except sqlite3.IntegrityError:
        return False  # UNIQUE constraint → already watching


def get_wallets_for_user(user_id: int) -> list[sqlite3.Row]:
    """Return all wallets being watched by a specific user."""
    with _connect() as conn:
        return conn.execute(
            "SELECT * FROM watched_wallets WHERE user_id = ? ORDER BY created_at DESC",
            (user_id,),
        ).fetchall()


def remove_wallet(wallet_id: int, user_id: int) -> bool:
    """
    Delete a wallet row.  user_id is included as a safety check so users
    cannot delete each other's entries.
    Returns True if a row was actually deleted.
    """
    with _connect() as conn:
        cur = conn.execute(
            "DELETE FROM watched_wallets WHERE id = ? AND user_id = ?",
            (wallet_id, user_id),
        )
        return cur.rowcount > 0


def get_all_wallets() -> list[sqlite3.Row]:
    """
    Return every watched wallet across all users, joined with the owner's
    chat_id so the polling job knows where to send alerts.
    """
    with _connect() as conn:
        return conn.execute("""
            SELECT
                ww.*,
                u.chat_id
            FROM watched_wallets ww
            JOIN users u ON u.user_id = ww.user_id
            ORDER BY ww.id
        """).fetchall()


def update_last_timestamp(wallet_id: int, timestamp: int) -> None:
    """Persist the latest seen trade timestamp so we don't re-alert."""
    with _connect() as conn:
        conn.execute(
            "UPDATE watched_wallets SET last_timestamp = ? WHERE id = ?",
            (timestamp, wallet_id),
        )


def count_wallets_for_user(user_id: int) -> int:
    """How many wallets is this user currently watching?"""
    with _connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) FROM watched_wallets WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        return row[0] if row else 0

"""
bot.py — PolyTrack Telegram Bot — Entry Point
==============================================

╔══════════════════════════════════════════════════════════════════════════════╗
║              WINDOWS DEVELOPER SETUP INSTRUCTIONS                          ║
╠══════════════════════════════════════════════════════════════════════════════╣
║                                                                              ║
║  1. Install Python 3.10+ from https://www.python.org/downloads/             ║
║     ✅ Check "Add Python to PATH" during installation                       ║
║                                                                              ║
║  2. Open Command Prompt or PowerShell in this project folder                ║
║     (Right-click in Explorer → "Open in Terminal")                          ║
║                                                                              ║
║  3. Create virtual environment:                                             ║
║       python -m venv venv                                                   ║
║                                                                              ║
║  4. Activate it:                                                            ║
║       venv\\Scripts\\activate         ← CMD                                  ║
║       .\\venv\\Scripts\\Activate.ps1  ← PowerShell                           ║
║     You'll see (venv) in your prompt when active.                           ║
║                                                                              ║
║  5. Install dependencies:                                                   ║
║       pip install -r requirements.txt                                       ║
║                                                                              ║
║  6. Get your BOT_TOKEN:                                                     ║
║     a) Open Telegram → search for @BotFather                                ║
║     b) Send /newbot → follow prompts → copy the token                       ║
║                                                                              ║
║  7. Create your .env file:                                                  ║
║       copy .env.example .env       ← CMD                                    ║
║       cp .env.example .env         ← PowerShell / Git Bash                  ║
║     Then open .env and paste your token after BOT_TOKEN=                   ║
║                                                                              ║
║  8. Run locally:                                                            ║
║       python bot.py                                                         ║
║     The bot will start polling. Open Telegram and send /start               ║
║                                                                              ║
║  ─────────────────────────────────── VPS DEPLOY (Linux) ─────────────────  ║
║                                                                              ║
║  1. SSH into your VPS:                                                      ║
║       ssh user@your-server-ip                                               ║
║                                                                              ║
║  2. Copy project files (from your local machine):                           ║
║       scp -r . user@your-server-ip:/home/user/polytrack/                   ║
║     OR clone from GitHub:                                                   ║
║       git clone https://github.com/you/polytrack.git                       ║
║                                                                              ║
║  3. On VPS — create venv & install:                                         ║
║       cd polytrack                                                           ║
║       python3 -m venv venv                                                  ║
║       source venv/bin/activate                                              ║
║       pip install -r requirements.txt                                       ║
║                                                                              ║
║  4. Create .env on VPS:                                                     ║
║       nano .env                                                             ║
║     Add: BOT_TOKEN=your_token                                               ║
║                                                                              ║
║  5. Create systemd service (/etc/systemd/system/polytrack.service):         ║
║                                                                              ║
║     [Unit]                                                                  ║
║     Description=PolyTrack Telegram Bot                                      ║
║     After=network.target                                                    ║
║                                                                              ║
║     [Service]                                                               ║
║     Type=simple                                                             ║
║     User=ubuntu                                                             ║
║     WorkingDirectory=/home/ubuntu/polytrack                                 ║
║     ExecStart=/home/ubuntu/polytrack/venv/bin/python bot.py                 ║
║     Restart=always                                                          ║
║     RestartSec=10                                                           ║
║     StandardOutput=journal                                                  ║
║     StandardError=journal                                                   ║
║                                                                              ║
║     [Install]                                                               ║
║     WantedBy=multi-user.target                                              ║
║                                                                              ║
║  6. Enable & start:                                                         ║
║       sudo systemctl daemon-reload                                           ║
║       sudo systemctl enable polytrack                                       ║
║       sudo systemctl start  polytrack                                       ║
║       sudo journalctl -fu polytrack   ← live logs                           ║
║                                                                              ║
╚══════════════════════════════════════════════════════════════════════════════╝
"""

import asyncio
import logging
import os
import sys
from datetime import datetime, timezone

# Force UTF-8 output on Windows (avoids cp1252 UnicodeEncodeError for emoji in logs)
if sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if sys.stderr.encoding.lower() != "utf-8":
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


from dotenv import load_dotenv
from telegram import Bot
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    filters,
)
from telegram.constants import ParseMode

import db
import api
import scanner
import agent
from handlers import (
    cmd_start,
    cmd_help,
    cmd_my_wallets,
    cmd_history,
    callback_history,
    cmd_remove_wallet,
    callback_remove_wallet,
    handle_menu_text,
    build_add_wallet_conversation,
    format_trade_alert,
    cmd_paper_buy,
    cmd_paper_sell,
    cmd_sellall,
    cmd_portfolio,
    cmd_quick_trade,
    callback_quick_trade,
    cmd_agent_status,
    cmd_agent_toggle,
)

# ─────────────────────────────────────────────────────────────────────────────
#  Environment & logging setup
# ─────────────────────────────────────────────────────────────────────────────

load_dotenv()  # reads .env from project root

LOG_LEVEL     = os.getenv("LOG_LEVEL", "INFO").upper()
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", "10"))   # default 10 s — Polymarket API itself has ~2-4 min indexing lag
BOT_TOKEN     = os.getenv("BOT_TOKEN", "")

# Console + rotating file logging
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("polytrack.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
#  Background polling job
# ─────────────────────────────────────────────────────────────────────────────

async def poll_trades(context) -> None:
    """
    Called by JobQueue every POLL_INTERVAL seconds.

    For each watched wallet (across all users):
      1. Fetch recent trades from Polymarket Data API.
      2. If last_timestamp == 0 (first run): silently set cursor to newest
         trade so we don't spam historical alerts on startup.
      3. For genuinely NEW trades: apply filters, send alert.
      4. Persist updated last_timestamp so we never re-alert.
    """
    try:
        await _poll_trades_inner(context)
    except Exception as exc:
        logger.error("💥 Unhandled crash in poll_trades: %s", exc, exc_info=True)


async def _poll_trades_inner(context) -> None:
    """Inner polling logic — wrapped by poll_trades for crash safety."""
    import time as _time

    bot: Bot = context.bot
    wallets   = db.get_all_wallets()

    if not wallets:
        logger.debug("Poll cycle: no wallets configured.")
        return

    logger.info("🔄 Poll cycle — %d wallet(s) to check", len(wallets))

    for row in wallets:
        wallet_id = row["id"]
        address   = row["wallet_address"]
        chat_id   = row["chat_id"]
        nickname  = row["nickname"]
        min_usd   = row["min_usd_threshold"]
        only_buys = bool(row["only_buys"])
        last_ts   = row["last_timestamp"]

        label = nickname or f"{address[:8]}…"
        logger.info("  Checking: %s (last_ts=%d)", label, last_ts)

        try:
            trades = await api.fetch_trades(address, limit=50)
        except Exception as exc:
            logger.error("API error for %s: %s", label, exc)
            continue

        if not trades:
            logger.info("  No trades returned for %s", label)
            continue

        logger.info("  Got %d trades for %s", len(trades), label)

        # ── FIRST RUN: Initialize cursor silently ───────────────────────────
        # last_ts == 0 means this wallet was just added and has never been
        # polled. We set the cursor to the newest trade so we only alert on
        # truly NEW trades going forward, not dump all history.
        if last_ts == 0:
            newest_ts = max(
                (api.parse_trade_timestamp(t) for t in trades), default=0
            )
            init_ts = newest_ts if newest_ts > 0 else int(_time.time())
            db.update_last_timestamp(wallet_id, init_ts)
            logger.info(
                "  📍 First poll for %s — cursor initialised to %d (no alerts)",
                label, init_ts,
            )
            continue  # Don't send alerts for existing trades

        # ── Process new trades (oldest-first so alerts arrive in order) ─────
        new_max_ts  = last_ts
        alerts_sent = 0

        for trade in reversed(trades):
            ts         = api.parse_trade_timestamp(trade)
            trade_type = api.parse_trade_type(trade)
            size       = api.parse_trade_size(trade)
            price      = api.parse_trade_price(trade)
            usd_value  = api.parse_trade_usd_value(trade)
            outcome    = api.parse_trade_outcome(trade)

            logger.debug(
                "    Trade ts=%d last_ts=%d type=%s size=%.2f price=%.3f usd=%.2f",
                ts, last_ts, trade_type, size, price, usd_value,
            )

            # ── Skip already-seen trades ────────────────────────────────────
            if ts <= last_ts:
                logger.debug("    ↩ Skipped (already seen, ts=%d)", ts)
                continue

            # ── Apply user filters ──────────────────────────────────────────
            if only_buys and trade_type != "BUY":
                logger.info("  ⏭ Filtered (only_buys=True, got %s)", trade_type)
                new_max_ts = max(new_max_ts, ts)
                continue

            if usd_value < min_usd:
                logger.info(
                    "  ⏭ Filtered (usd_value=%.2f < min_usd=%.2f)",
                    usd_value, min_usd,
                )
                new_max_ts = max(new_max_ts, ts)
                continue

            # ── Get market title (from trade directly — no extra HTTP call) ──
            market_title = api.get_trade_title(trade)

            # ── Build alert ──────────────────────────────────────────────────
            poly_url = f"https://polymarket.com/profile/{address}?tab=activity"

            msg = format_trade_alert(
                trade_type     = trade_type,
                size           = size,
                price          = price,
                usd_value      = usd_value,
                outcome        = outcome,
                market_title   = market_title,
                wallet_address = address,
                nickname       = nickname,
                timestamp      = ts,
                polymarket_url = poly_url,
            )

            # ── Send alert ───────────────────────────────────────────────────
            try:
                await bot.send_message(
                    chat_id                  = chat_id,
                    text                     = msg,
                    parse_mode               = ParseMode.MARKDOWN_V2,
                    disable_web_page_preview = True,
                )
                alerts_sent += 1
                logger.info(
                    "  🔔 Alert sent → chat=%s wallet=%s %s $%.2f",
                    chat_id, label, trade_type, usd_value,
                )
            except Exception as send_exc:
                logger.error(
                    "  ❌ Failed to send alert to chat=%s: %s",
                    chat_id, send_exc,
                )
                # Still advance cursor so we don't retry this broken message

            new_max_ts = max(new_max_ts, ts)

        # ── Persist updated cursor ───────────────────────────────────────────
        if new_max_ts > last_ts:
            db.update_last_timestamp(wallet_id, new_max_ts)
            logger.info(
                "  💾 Cursor updated for %s: %d → %d (%d alert(s) sent)",
                label, last_ts, new_max_ts, alerts_sent,
            )
        else:
            logger.info("  ✅ No new trades for %s", label)

    logger.info("🔄 Poll cycle complete.")


# ─────────────────────────────────────────────────────────────────────────────
#  Application bootstrap
# ─────────────────────────────────────────────────────────────────────────────

async def _notify_startup(app) -> None:
    """Send a startup notification to every tracked user."""
    wallets = db.get_all_wallets()
    notified = set()
    for row in wallets:
        cid = row["chat_id"]
        if cid in notified:
            continue
        notified.add(cid)
        try:
            await app.bot.send_message(
                chat_id=cid,
                text="🟢 *PolyTrack is online\\!*\nWallet monitoring has resumed\\.",
                parse_mode="MarkdownV2",
            )
        except Exception as exc:
            logger.warning("Could not send startup ping to %s: %s", cid, exc)


def main() -> None:
    if not BOT_TOKEN:
        logger.critical(
            "❌ BOT_TOKEN is not set!\n"
            "   Copy .env.example → .env and fill in your token from @BotFather."
        )
        raise SystemExit(1)

    logger.info("🚀 PolyTrack Bot starting up…")

    # Initialise SQLite schema (idempotent)
    db.init_db()

    async def _post_init(app):
        from telegram import BotCommand
        commands = [
            BotCommand("start", "Show main menu"),
            BotCommand("help", "Show help message"),
            BotCommand("my_wallets", "List your tracked wallets"),
            BotCommand("history", "Show last 5 trades for a wallet"),
            BotCommand("remove_wallet", "Stop tracking a wallet"),
            BotCommand("paper_buy", "Buy paper shares (e.g. /paper_buy Up 10)"),
            BotCommand("paper_sell", "Sell paper shares (e.g. /paper_sell Up 10)"),
            BotCommand("sellall", "Sell ALL paper positions at once"),
            BotCommand("portfolio", "View your paper trading portfolio"),
            BotCommand("quick_trade", "Open Up/Down buy/sell buttons"),
            BotCommand("agent_status", "View the auto-trading agent status"),
            BotCommand("agent_toggle", "Pause or resume the auto-trading agent"),
        ]
        try:
            await app.bot.set_my_commands(commands)
            logger.info("Bot commands updated successfully.")
        except Exception as e:
            logger.warning("Could not set bot commands: %s", e)
            
        await _notify_startup(app)
        asyncio.create_task(scanner.run_block_scanner(app))
        asyncio.create_task(agent.start(app))
        logger.info("🤖 PolyAgent background tasks scheduled.")

    # Build the Application
    app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .post_init(_post_init)
        .build()
    )

    # ── Register handlers (order matters for dispatcher) ───────────────────

    # 1. Multi-step conversation for adding a wallet (highest priority)
    app.add_handler(build_add_wallet_conversation())

    # 2. Simple command handlers
    app.add_handler(CommandHandler("start",          cmd_start))
    app.add_handler(CommandHandler("help",           cmd_help))
    app.add_handler(CommandHandler("my_wallets",     cmd_my_wallets))
    app.add_handler(CommandHandler("history",        cmd_history))
    app.add_handler(CommandHandler("remove_wallet",  cmd_remove_wallet))
    app.add_handler(CommandHandler("paper_buy",      cmd_paper_buy))
    app.add_handler(CommandHandler("paper_sell",     cmd_paper_sell))
    app.add_handler(CommandHandler("sellall",        cmd_sellall))
    app.add_handler(CommandHandler("portfolio",      cmd_portfolio))
    app.add_handler(CommandHandler("quick_trade",    cmd_quick_trade))
    app.add_handler(CommandHandler("agent_status",   cmd_agent_status))
    app.add_handler(CommandHandler("agent_toggle",   cmd_agent_toggle))

    # 3. Inline button callbacks
    app.add_handler(CallbackQueryHandler(callback_remove_wallet, pattern=r"^remove:"))
    app.add_handler(CallbackQueryHandler(callback_history,       pattern=r"^hist:"))
    app.add_handler(CallbackQueryHandler(callback_quick_trade,   pattern=r"^qbuy:"))
    app.add_handler(CallbackQueryHandler(callback_quick_trade,   pattern=r"^qsell:"))

    # 4. Reply keyboard button text routing (catch-all text messages)
    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            handle_menu_text,
        )
    )

    # ── Background polling job ─────────────────────────────────────────────
    # We remove the old REST API polling job and use the Web3 loop instead.

    # ── Global error handler (logs all unhandled exceptions) ──────────────
    async def _error_handler(update, context) -> None:
        logger.error(
            "Unhandled exception (update=%s): %s",
            update, context.error, exc_info=context.error,
        )
    app.add_error_handler(_error_handler)

    logger.info(
        "✅ Bot is running. Polling interval: %ds. Press Ctrl+C to stop.",
        POLL_INTERVAL,
    )

    # Start polling (blocks until stopped)
    app.run_polling(
        allowed_updates = ["message", "callback_query"],
        drop_pending_updates = True,   # ignore messages sent while bot was offline
    )

    # Cleanup after stop
    asyncio.get_event_loop().run_until_complete(api.close_session())
    logger.info("👋 Bot shut down gracefully.")


if __name__ == "__main__":
    main()

"""
handlers.py — Telegram command & conversation handlers for PolyTrack Bot
=========================================================================
Defines every handler that the ApplicationBuilder registers:
  • /start        → main menu
  • /help
  • /my_wallets
  • /history      → last 5 trades for a wallet (for testing / manual check)
  • /remove_wallet
  • ConversationHandler for adding a wallet (multi-step wizard)
  • Inline-button callbacks (remove wallet, history picker)
"""

import re
import logging
import time as _time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)
import aiohttp
import json
import re
from telegram.ext import (
    ContextTypes,
    ConversationHandler,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
)
from telegram.constants import ParseMode

import db
import api

logger = logging.getLogger(__name__)

# ─── Conversation states ──────────────────────────────────────────────────────
(
    STATE_WALLET,
    STATE_NICKNAME,
    STATE_MIN_USD,
    STATE_ONLY_BUYS,
) = range(4)

# ─── Wallet address regex (0x + 40-42 hex chars) ─────────────────────────────
WALLET_RE = re.compile(r"^0x[0-9a-fA-F]{40,42}$")

# ─── Max wallets per user (prevent abuse) ────────────────────────────────────
MAX_WALLETS = 10

# ─── All menu button labels (used in fallback matching) ──────────────────────
MENU_BUTTONS = frozenset(
    ["\u2795 Add Wallet", "\U0001f4cb My Wallets", "\U0001f5d1\ufe0f Remove Wallet", "\U0001f550 History", "\U0001f4bc Portfolio", "\u26a1 Quick Trade", "\u2753 Help"]
)


# ─────────────────────────────────────────────────────────────────────────────
#  Shared UI helpers
# ─────────────────────────────────────────────────────────────────────────────

def _main_menu_keyboard() -> ReplyKeyboardMarkup:
    """Persistent bottom keyboard shown after /start."""
    return ReplyKeyboardMarkup(
        [
            ["\u2795 Add Wallet",  "\U0001f4cb My Wallets"],
            ["\U0001f550 History", "\U0001f4bc Portfolio"],
            ["\u26a1 Quick Trade", "\U0001f5d1\ufe0f Remove Wallet"],
            ["\u2753 Help"],
        ],
        resize_keyboard=True,
        input_field_placeholder="Choose an option\u2026",
    )


def _cancel_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [["\u274c Cancel"]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


def _esc(text: str) -> str:
    """
    Escape special MarkdownV2 characters.
    NOTE: We intentionally do NOT use ~ in any message templates (it triggers
    strikethrough formatting). Use the \u2248 character instead for 'approximately'.
    """
    special = r"\_*[]()~`>#+-=|{}.!"
    return "".join(f"\\{c}" if c in special else c for c in str(text))

def _esc_code(text: str) -> str:
    """Escape special MarkdownV2 characters for use INSIDE code blocks."""
    return str(text).replace('\\', '\\\\').replace('`', '\\`')


def _build_trade_line(
    i: int,
    emoji: str,
    t_type: str,
    outcome_str: str,
    size_str: str,
    price: float,
    usd_value: float,
    dt_str: str,
    market_title: str | None,
) -> str:
    """
    Build one trade line for history display.
    Uses \u2248 (U+2248) instead of ~ to show 'approximately', avoiding
    Telegram's strikethrough parser which treats ~ as a formatting marker.
    """
    title_line = ""
    if market_title:
        truncated = market_title[:55] + ("\u2026" if len(market_title) > 55 else "")
        title_line = f"\n    \u2022 {_esc(truncated)}"

    return (
        f"*{i}\\.* {emoji} *{_esc(t_type)}*{outcome_str} \\| `{size_str}` sh @ `${price:.3f}`{title_line}\n"
        f"    💵 \u2248`${usd_value:,.2f}` \\| ⏱ `{_esc_code(dt_str)}`"
    )


# ─────────────────────────────────────────────────────────────────────────────
#  /start
# ─────────────────────────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    db.upsert_user(user.id, user.username, update.effective_chat.id)

    await update.message.reply_text(
        f"\U0001f44b *Welcome to PolyTrack, {_esc(user.first_name)}\\!*\n\n"
        "I'm your personal Polymarket trade monitor\\. "
        "Add any public wallet address and I'll ping you the moment a trade lands\\.\n\n"
        "\U0001f50d *What I can do:*\n"
        "\u2022 Track multiple wallets simultaneously\n"
        "\u2022 Filter by minimum trade size \\(USD\\)\n"
        "\u2022 Alert only on BUY trades if you prefer\n"
        "\u2022 Show the last 5 trades of any wallet on demand\n"
        "\u2022 Send rich, real\\-time notifications\n\n"
        "\U0001f4b8 *Paper Trading Phase 2:*\n"
        "You have `$100` virtual USD to play with\\! Try these commands:\n"
        "`/paper_buy <url> Yes 10$` \u2014 Buy $10 of 'Yes'\n"
        "`/paper_sell <url> Yes 10` \u2014 Sell 10 shares of 'Yes'\n"
        "`/portfolio` \u2014 View your active paper trades\n\n"
        "Use the menu below to get started\\!",
        parse_mode=ParseMode.MARKDOWN_V2,
        reply_markup=_main_menu_keyboard(),
    )


# ─────────────────────────────────────────────────────────────────────────────
#  /help
# ─────────────────────────────────────────────────────────────────────────────

HELP_TEXT = (
    "\U0001f916 *PolyTrack Bot \u2014 Help*\n\n"
    "*Commands:*\n"
    "  /start \u2014 Show main menu\n"
    "  /add\\_wallet \u2014 Add a wallet to track\n"
    "  /my\\_wallets \u2014 List your tracked wallets\n"
    "  /history \\[address\\] \u2014 Show last 5 trades\n"
    "  /remove\\_wallet \u2014 Stop tracking a wallet\n"
    "  /help \u2014 This message\n\n"
    "*How it works:*\n"
    "Every 20 seconds I query the official Polymarket Data API for new trades "
    "on each wallet you're watching\\. "
    "When a trade matches your filters, I send you an alert\\.\n\n"
    "*Which wallet address to use?*\n"
    "Use your *Polymarket proxy wallet* address \u2014 the `0x\u2026` address shown "
    "in your Polymarket portfolio URL or profile page\\. "
    "This is *not* your MetaMask/EOA address; it's the smart contract wallet "
    "Polymarket creates for you on first login\\.\n\n"
    "*Testing:*\n"
    "Use \U0001f550 *History* \\(or `/history 0x\u2026`\\) to instantly see the last "
    "5 trades of any wallet without waiting for a poll cycle\\.\n\n"
    "*Privacy:*\n"
    "Only *public* on\\-chain wallet addresses are used\\. "
    "I never ask for private keys or seed phrases\\.\n\n"
    f"Each user can track up to *{MAX_WALLETS} wallets*\\."
)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        HELP_TEXT,
        parse_mode=ParseMode.MARKDOWN_V2,
        reply_markup=_main_menu_keyboard(),
    )


# ─────────────────────────────────────────────────────────────────────────────
#  /my_wallets
# ─────────────────────────────────────────────────────────────────────────────

async def cmd_my_wallets(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    rows = db.get_wallets_for_user(user.id)

    if not rows:
        await update.message.reply_text(
            "\U0001f4ed You're not tracking any wallets yet\\.\n"
            "Tap *\u2795 Add Wallet* to get started\\!",
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=_main_menu_keyboard(),
        )
        return

    lines = ["\U0001f4cb *Your Tracked Wallets*\n"]
    for row in rows:
        addr     = row["wallet_address"]
        nick     = row["nickname"] or "\u2014"
        min_usd  = row["min_usd_threshold"]
        only_buy = "\u2705 Yes" if row["only_buys"] else "\u274c No"
        short    = f"`{addr[:6]}\u2026{addr[-4:]}`"
        lines.append(
            f"*{_esc(nick)}* \\({short}\\)\n"
            f"  \U0001f4b5 Min USD: `${min_usd:.0f}` \\| Buys only: {only_buy}\n"
            f"  \U0001f194 ID: `{row['id']}`\n"
        )

    lines.append("_Tap \U0001f5d1\ufe0f Remove Wallet to stop tracking one\\._")

    await update.message.reply_text(
        "\n".join(lines),
        parse_mode=ParseMode.MARKDOWN_V2,
        reply_markup=_main_menu_keyboard(),
    )


# ─────────────────────────────────────────────────────────────────────────────
#  /history — last 5 trades for a wallet (great for testing)
# ─────────────────────────────────────────────────────────────────────────────

async def cmd_history(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /history [wallet_address]

    If a wallet address is provided → fetch directly.
    If the user has watched wallets → show an inline picker.
    If only one wallet → fetch directly.
    """
    user = update.effective_user

    # ── Argument provided: /history 0x... ──────────────────────────────────
    if context.args:
        wallet = context.args[0].strip()
        if not WALLET_RE.match(wallet):
            await update.message.reply_text(
                "\u274c Invalid wallet address\\.\n"
                "Must start with `0x` followed by 40\\-42 hex characters\\.\n\n"
                "Example: `/history 0xAbCd1234`",
                parse_mode=ParseMode.MARKDOWN_V2,
                reply_markup=_main_menu_keyboard(),
            )
            return
        await _send_history(update.message, wallet, None)
        return

    # ── No argument — check user's watched wallets ──────────────────────────
    rows = db.get_wallets_for_user(user.id)

    if not rows:
        await update.message.reply_text(
            "\U0001f4ed You have no tracked wallets yet\\.\n\n"
            "You can also check any wallet directly:\n"
            "`/history 0xYourWalletAddress`",
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=_main_menu_keyboard(),
        )
        return

    if len(rows) == 1:
        row = rows[0]
        await _send_history(update.message, row["wallet_address"], row["nickname"])
        return

    # Multiple wallets — let the user pick via inline keyboard
    buttons = []
    for row in rows:
        label    = row["nickname"] or f"{row['wallet_address'][:8]}\u2026"
        nick_val = row["nickname"] or ""
        buttons.append([
            InlineKeyboardButton(
                f"\U0001f4ca {label}",
                callback_data=f"hist:{row['wallet_address']}:{nick_val}",
            )
        ])

    await update.message.reply_text(
        "\U0001f550 *Trade History*\n\nWhich wallet would you like to check?",
        parse_mode=ParseMode.MARKDOWN_V2,
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def _send_history(message, wallet: str, nickname: str | None) -> None:
    """
    Fetch the last 5 trades for *wallet* and reply with a formatted list.
    Falls back to plain text if MarkdownV2 parsing fails.
    """
    short = f"`{wallet[:6]}\u2026{wallet[-4:]}`"
    sent = await message.reply_text(
        f"\U0001f50d Fetching last 5 trades for {short}\\.\\.\\.",
        parse_mode=ParseMode.MARKDOWN_V2,
    )

    trades = await api.fetch_trades(wallet, limit=5)

    if not trades:
        await sent.edit_text(
            f"\U0001f4ed No trades found for {short}\\.\n\n"
            "_The wallet may have no activity, or the Polymarket API is "
            "temporarily unavailable\\. Try again in a moment\\._",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return

    wallet_disp = nickname or f"{wallet[:6]}\u2026{wallet[-4:]}"
    count       = min(len(trades), 5)
    lines       = [f"\U0001f550 *Last {count} Trade{'s' if count > 1 else ''} \u2014 {_esc(wallet_disp)}*\n"]

    for i, trade in enumerate(trades[:5], 1):
        t_type    = api.parse_trade_type(trade)
        price     = api.parse_trade_price(trade)
        usd_value = api.parse_trade_usd_value(trade)
        size      = api.parse_trade_size(trade)
        outcome   = api.parse_trade_outcome(trade)
        ts        = api.parse_trade_timestamp(trade)

        emoji       = "\U0001f4b0" if t_type == "BUY" else "\U0001f4c9"
        dt_str      = (
            datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%b %d, %H:%M UTC")
            if ts else "Unknown time"
        )
        outcome_str = f" {_esc(outcome)}" if outcome else ""
        size_str    = f"{size:,.0f}" if size >= 1 else f"{size:.4f}"

        market_title = api.get_trade_title(trade)

        lines.append(_build_trade_line(
            i, emoji, t_type, outcome_str, size_str,
            price, usd_value, dt_str, market_title,
        ))

    poly_url = f"https://polymarket.com/profile/{wallet}?tab=activity"
    lines.append(f"\n[\U0001f517 View full activity on Polymarket]({poly_url})")

    text = "\n".join(lines)
    try:
        await sent.edit_text(
            text,
            parse_mode=ParseMode.MARKDOWN_V2,
            disable_web_page_preview=True,
        )
    except Exception as exc:
        logger.warning("MarkdownV2 failed, sending plain text: %s", exc)
        plain = text.replace("*", "").replace("`", "").replace("\\", "")
        await sent.edit_text(plain, disable_web_page_preview=True)


async def callback_history(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle the inline wallet picker for /history."""
    query = update.callback_query
    await query.answer()

    # callback_data format: "hist:{wallet}:{nickname}"
    parts    = query.data.split(":", 2)
    wallet   = parts[1] if len(parts) > 1 else ""
    nickname = parts[2] if len(parts) > 2 and parts[2] else None

    if not wallet:
        await query.edit_message_text("\u26a0\ufe0f Invalid selection\\.", parse_mode=ParseMode.MARKDOWN_V2)
        return

    short = f"`{wallet[:6]}\u2026{wallet[-4:]}`"
    await query.edit_message_text(
        f"\U0001f50d Fetching last 5 trades for {short}\\.\\.\\.",
        parse_mode=ParseMode.MARKDOWN_V2,
    )

    trades = await api.fetch_trades(wallet, limit=5)

    if not trades:
        await query.edit_message_text(
            f"\U0001f4ed No trades found for {short}\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return

    wallet_disp = nickname or f"{wallet[:6]}\u2026{wallet[-4:]}"
    count       = min(len(trades), 5)
    lines       = [f"\U0001f550 *Last {count} Trade{'s' if count > 1 else ''} \u2014 {_esc(wallet_disp)}*\n"]

    for i, trade in enumerate(trades[:5], 1):
        t_type    = api.parse_trade_type(trade)
        price     = api.parse_trade_price(trade)
        usd_value = api.parse_trade_usd_value(trade)
        size      = api.parse_trade_size(trade)
        outcome   = api.parse_trade_outcome(trade)
        ts        = api.parse_trade_timestamp(trade)

        emoji       = "\U0001f4b0" if t_type == "BUY" else "\U0001f4c9"
        dt_str      = (
            datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%b %d, %H:%M UTC")
            if ts else "Unknown time"
        )
        outcome_str = f" {_esc(outcome)}" if outcome else ""
        size_str    = f"{size:,.0f}" if size >= 1 else f"{size:.4f}"

        market_title = api.get_trade_title(trade)

        lines.append(_build_trade_line(
            i, emoji, t_type, outcome_str, size_str,
            price, usd_value, dt_str, market_title,
        ))

    poly_url = f"https://polymarket.com/profile/{wallet}?tab=activity"
    lines.append(f"\n[\U0001f517 View full activity on Polymarket]({poly_url})")

    text = "\n".join(lines)
    try:
        await query.edit_message_text(
            text,
            parse_mode=ParseMode.MARKDOWN_V2,
            disable_web_page_preview=True,
        )
    except Exception as exc:
        logger.warning("MarkdownV2 failed in callback_history, falling back: %s", exc)
        plain = text.replace("*", "").replace("`", "").replace("\\", "")
        await query.edit_message_text(plain, disable_web_page_preview=True)


# ─────────────────────────────────────────────────────────────────────────────
#  /remove_wallet — inline keyboard approach
# ─────────────────────────────────────────────────────────────────────────────

async def cmd_remove_wallet(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    rows = db.get_wallets_for_user(user.id)

    if not rows:
        await update.message.reply_text(
            "\U0001f4ed You have no wallets to remove\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=_main_menu_keyboard(),
        )
        return

    buttons = []
    for row in rows:
        addr  = row["wallet_address"]
        label = row["nickname"] or f"{addr[:8]}\u2026"
        buttons.append([
            InlineKeyboardButton(
                f"\U0001f5d1\ufe0f {label}",
                callback_data=f"remove:{row['id']}",
            )
        ])
    buttons.append([InlineKeyboardButton("\u274c Cancel", callback_data="remove:cancel")])

    await update.message.reply_text(
        "\U0001f5d1\ufe0f *Remove a Wallet*\n\nChoose which wallet to stop tracking:",
        parse_mode=ParseMode.MARKDOWN_V2,
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def callback_remove_wallet(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query   = update.callback_query
    await query.answer()
    user    = query.from_user
    payload = query.data  # "remove:<id>" or "remove:cancel"

    if payload == "remove:cancel":
        await query.edit_message_text("\u2705 No changes made\\.", parse_mode=ParseMode.MARKDOWN_V2)
        return

    try:
        wallet_id = int(payload.split(":")[1])
    except (IndexError, ValueError):
        await query.edit_message_text("\u26a0\ufe0f Invalid action\\.", parse_mode=ParseMode.MARKDOWN_V2)
        return

    removed = db.remove_wallet(wallet_id, user.id)
    if removed:
        await query.edit_message_text(
            "\u2705 Wallet removed\\. You'll no longer receive alerts for it\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
    else:
        await query.edit_message_text(
            "\u26a0\ufe0f Wallet not found \\(maybe already removed\\?\\)\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
        )


# ─────────────────────────────────────────────────────────────────────────────
#  Add-wallet ConversationHandler steps
# ─────────────────────────────────────────────────────────────────────────────

async def conv_start_add(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Entry point: /add_wallet or Add Wallet button."""
    user = update.effective_user

    # Always register the user first — handles DB resets and users who skip /start
    db.upsert_user(user.id, user.username, update.effective_chat.id)

    count = db.count_wallets_for_user(user.id)

    if count >= MAX_WALLETS:
        await update.message.reply_text(
            f"\u26a0\ufe0f You've reached the limit of *{MAX_WALLETS} wallets*\\.\n"
            "Please remove one before adding another\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=_main_menu_keyboard(),
        )
        return ConversationHandler.END

    context.user_data.clear()
    await update.message.reply_text(
        "\u2795 *Add Wallet \u2014 Step 1 of 4*\n\n"
        "Send me the *Polymarket proxy wallet* address to track\\.\n"
        "_This is the `0x\u2026` address in your Polymarket profile URL \u2014 "
        "not your MetaMask EOA\\._\n\n"
        "Example: `/history 0xb76d3d56\u2026`\n\n"
        "Tap *\u274c Cancel* at any time to abort\\.",
        parse_mode=ParseMode.MARKDOWN_V2,
        reply_markup=_cancel_keyboard(),
    )
    return STATE_WALLET


async def conv_receive_wallet(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()

    # If user tapped a menu button while conversation was active → cancel & re-route
    if text in MENU_BUTTONS or text == "\u274c Cancel":
        await _cancel(update, context)
        if text in MENU_BUTTONS and text != "\u274c Cancel":
            await handle_menu_text(update, context)
        return ConversationHandler.END

    if not WALLET_RE.match(text):
        await update.message.reply_text(
            "\u274c That doesn't look like a valid wallet address\\.\n"
            "It must start with `0x` followed by 40\\-42 hex characters\\.\n\n"
            "Try again, or tap \u274c Cancel:",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return STATE_WALLET

    # Check for duplicate
    existing = [r["wallet_address"] for r in db.get_wallets_for_user(update.effective_user.id)]
    if text.lower() in existing:
        await update.message.reply_text(
            "\u26a0\ufe0f You're already tracking that wallet\\!\n"
            "Send a different address, or tap \u274c Cancel:",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return STATE_WALLET

    context.user_data["wallet"] = text.lower()

    await update.message.reply_text(
        "\u2705 *Step 2 of 4 \u2014 Nickname*\n\n"
        "Give this wallet a friendly nickname \\(e\\.g\\. `Whale 1`\\)\\.\n"
        "Or send /skip to use the short address\\.",
        parse_mode=ParseMode.MARKDOWN_V2,
        reply_markup=_cancel_keyboard(),
    )
    return STATE_NICKNAME


async def conv_receive_nickname(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()

    if text in MENU_BUTTONS or text == "\u274c Cancel":
        await _cancel(update, context)
        if text in MENU_BUTTONS and text != "\u274c Cancel":
            await handle_menu_text(update, context)
        return ConversationHandler.END

    context.user_data["nickname"] = None if text.lower() in ("/skip", "skip") else text[:32]

    await update.message.reply_text(
        "\u2705 *Step 3 of 4 \u2014 Minimum Trade Size*\n\n"
        "Only alert me when the trade value is at least how many USD?\n"
        "_Send a number like `100` or `0` for all trades\\._\n"
        "Or send /skip for no minimum\\.",
        parse_mode=ParseMode.MARKDOWN_V2,
        reply_markup=_cancel_keyboard(),
    )
    return STATE_MIN_USD


async def conv_receive_min_usd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()

    if text in MENU_BUTTONS or text == "\u274c Cancel":
        await _cancel(update, context)
        if text in MENU_BUTTONS and text != "\u274c Cancel":
            await handle_menu_text(update, context)
        return ConversationHandler.END

    if text.lower() in ("/skip", "skip"):
        context.user_data["min_usd"] = 0.0
    else:
        try:
            val = float(text.replace("$", "").replace(",", ""))
            if val < 0:
                raise ValueError
            context.user_data["min_usd"] = val
        except ValueError:
            await update.message.reply_text(
                "\u274c Please enter a valid positive number \\(e\\.g\\. `50` or `0`\\):",
                parse_mode=ParseMode.MARKDOWN_V2,
            )
            return STATE_MIN_USD

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("\u2705 BUY trades only", callback_data="onlybuys:yes"),
            InlineKeyboardButton("\U0001f4ca All trades",   callback_data="onlybuys:no"),
        ]
    ])
    await update.message.reply_text(
        "\u2705 *Step 4 of 4 \u2014 Trade Filter*\n\n"
        "Should I alert you only on *BUY* trades, or all trades \\(SELL included\\)?",
        parse_mode=ParseMode.MARKDOWN_V2,
        reply_markup=keyboard,
    )
    return STATE_ONLY_BUYS


async def conv_receive_only_buys(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    only_buys = query.data == "onlybuys:yes"
    user      = query.from_user
    wallet    = context.user_data.get("wallet", "")
    nick      = context.user_data.get("nickname")
    min_usd   = context.user_data.get("min_usd", 0.0)

    # Safety net: ensure user row exists (handles DB resets / first add without /start)
    db.upsert_user(user.id, user.username, query.message.chat_id)

    success = db.add_wallet(user.id, wallet, nick, min_usd, only_buys)

    nick_display = _esc(nick) if nick else f"`{wallet[:6]}\u2026{wallet[-4:]}`"
    filter_text  = "BUY trades only" if only_buys else "all trades"
    min_usd_text = f"${min_usd:,.0f}" if min_usd else "no minimum"

    if success:
        await query.edit_message_text(
            f"\U0001f389 *Wallet added successfully\\!*\n\n"
            f"\U0001f4cd *Name:*    {nick_display}\n"
            f"\U0001f4b5 *Min size:* `{_esc(min_usd_text)}`\n"
            f"\U0001f50d *Filter:*  {_esc(filter_text)}\n\n"
            "_I'll send alerts within 20 seconds of a new trade\\._",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
    else:
        await query.edit_message_text(
            "\u26a0\ufe0f Could not add wallet \u2014 it may already be tracked\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
        )

    # Restore main keyboard
    await context.bot.send_message(
        chat_id      = query.message.chat_id,
        text         = "Use the menu below to manage your wallets\\.",
        parse_mode   = ParseMode.MARKDOWN_V2,
        reply_markup = _main_menu_keyboard(),
    )
    context.user_data.clear()
    return ConversationHandler.END


async def _cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    await update.message.reply_text(
        "\u274c Cancelled\\. No changes were made\\.",
        parse_mode=ParseMode.MARKDOWN_V2,
        reply_markup=_main_menu_keyboard(),
    )
    return ConversationHandler.END


# ─────────────────────────────────────────────────────────────────────────────
#  Reply keyboard menu text routing
# ─────────────────────────────────────────────────────────────────────────────

async def handle_menu_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Route taps on the persistent reply keyboard to the right handler."""
    text = update.message.text

    if text == "\u2795 Add Wallet":
        await conv_start_add(update, context)
    elif text == "\U0001f4cb My Wallets":
        await cmd_my_wallets(update, context)
    elif text == "\U0001f5d1\ufe0f Remove Wallet":
        await cmd_remove_wallet(update, context)
    elif text == "\U0001f550 History":
        await cmd_history(update, context)
    elif text == "\U0001f4bc Portfolio":
        await cmd_portfolio(update, context)
    elif text == "\u26a1 Quick Trade":
        await cmd_quick_trade(update, context)
    elif text == "\u2753 Help":
        await cmd_help(update, context)
    else:
        await update.message.reply_text(
            "\U0001f914 I didn't understand that\\. Use the menu below or /help\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=_main_menu_keyboard(),
        )


# ─────────────────────────────────────────────────────────────────────────────
#  Alert formatting (called from the background polling job in bot.py)
# ─────────────────────────────────────────────────────────────────────────────

def format_trade_alert(
    trade_type: str,
    size: float,
    price: float,
    usd_value: float,
    outcome: str,
    market_title: str | None,
    wallet_address: str,
    nickname: str | None,
    timestamp: int,
    polymarket_url: str,
) -> str:
    """
    Build the MarkdownV2 alert message sent to the user on new trades.
    Uses \u2248 instead of ~ to show 'approximately' — avoids strikethrough parser.
    """
    emoji       = "\U0001f7e2" if trade_type == "BUY" else "\U0001f534"
    wallet_disp = nickname or f"{wallet_address[:6]}\u2026{wallet_address[-4:]}"
    dt_str      = (
        datetime.fromtimestamp(timestamp, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        if timestamp else "Unknown time"
    )
    # Do not _esc these as they are inside `` backticks
    size_str    = f"{size:,.0f}" if size >= 1 else f"{size:.4f}"
    
    outcome_str = f" `{_esc_code(outcome)}`" if outcome else ""
    market_str  = _esc(market_title) if market_title else "Unknown Market"

    # Show how long ago the trade happened vs now (Polymarket API indexing lag)
    import time as _time
    now_ts     = int(_time.time())
    age_secs   = max(0, now_ts - timestamp) if timestamp else 0
    if age_secs < 60:
        age_str = f"{age_secs}s ago"
    elif age_secs < 3600:
        age_str = f"{age_secs // 60}m {age_secs % 60}s ago"
    else:
        age_str = f"{age_secs // 3600}h ago"

    return "\n".join([
        "🚨 *NEW POLYMARKET TRADE* 🚨\n",
        f"👤 *Wallet:* ` {_esc_code(wallet_disp)} `",
        f"      ↳ ` {wallet_address[:6]}\u2026{wallet_address[-4:]} `\n",
        f"📊 *Market:* *{market_str}*",
        f"🎯 *Action:* {emoji} *{_esc(trade_type)}*{outcome_str}",
        f"💰 *Size:* `{size_str}` shares",
        f"💲 *Price:* `${price:.3f}`",
        f"💵 *Value:* \u2248`${usd_value:,.2f}`\n",
        f"⏱ *Time:* `{_esc_code(dt_str)}` _\\({_esc(age_str)}\\)_",
        f"\n🔗 [\U0001f517 View Activity]({polymarket_url})",
    ])


# ─────────────────────────────────────────────────────────────────────────────
#  ConversationHandler factory (imported by bot.py)
# ─────────────────────────────────────────────────────────────────────────────

def build_add_wallet_conversation() -> ConversationHandler:
    """Build and return the multi-step add-wallet ConversationHandler."""

    # Any menu button tap while in a conversation gracefully cancels it
    menu_fallback = MessageHandler(
        filters.Regex(
            r"^(\u2795 Add Wallet|\U0001f4cb My Wallets|\U0001f5d1\ufe0f Remove Wallet"
            r"|\U0001f550 History|\U0001f4bc Portfolio|\u26a1 Quick Trade|\u2753 Help|\u274c Cancel)$"
        ),
        _cancel,
    )

    return ConversationHandler(
        entry_points=[
            CommandHandler("add_wallet", conv_start_add),
            MessageHandler(filters.Regex(r"^\u2795 Add Wallet$"), conv_start_add),
        ],
        states={
            STATE_WALLET: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, conv_receive_wallet),
            ],
            STATE_NICKNAME: [
                CommandHandler("skip", conv_receive_nickname),
                MessageHandler(filters.TEXT & ~filters.COMMAND, conv_receive_nickname),
            ],
            STATE_MIN_USD: [
                CommandHandler("skip", conv_receive_min_usd),
                MessageHandler(filters.TEXT & ~filters.COMMAND, conv_receive_min_usd),
            ],
            STATE_ONLY_BUYS: [
                CallbackQueryHandler(conv_receive_only_buys, pattern=r"^onlybuys:"),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", _cancel),
            menu_fallback,
        ],
        allow_reentry=True,
        per_message=False,
        name="add_wallet_conv",
    )


# ─────────────────────────────────────────────────────────────────────────────
#  Paper Trading Telegram Commands
# ─────────────────────────────────────────────────────────────────────────────

GAMMA_API_URL = "https://gamma-api.polymarket.com/events?slug={slug}"
CLOB_PRICE_URL = "https://clob.polymarket.com/price"
TARGET_EVENT_SLUG_BASE = "btc-updown-5m"
TARGET_EVENT_URL_BASE = f"https://polymarket.com/event/{TARGET_EVENT_SLUG_BASE}"
MARKET_TZ = ZoneInfo("America/New_York")
USER_TZ = ZoneInfo("Asia/Kolkata")
WINDOW_SECONDS = 300  # 5 minutes


def _parse_iso_datetime(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        val = str(raw).strip()
        if val.endswith("Z"):
            val = val[:-1] + "+00:00"
        dt = datetime.fromisoformat(val)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def _market_window_utc(market: dict) -> tuple[datetime | None, datetime | None]:
    start_utc = _parse_iso_datetime(
        market.get("startDate")
        or market.get("startTime")
        or market.get("start_date")
    )
    end_utc = _parse_iso_datetime(
        market.get("endDate")
        or market.get("endTime")
        or market.get("closeDate")
        or market.get("closedTime")
    )
    return start_utc, end_utc


def _pick_market_for_now(markets: list[dict]) -> tuple[dict | None, str]:
    """Pick active market for current time; fallback to nearest upcoming, then latest."""
    now_utc = datetime.now(timezone.utc)
    active: list[tuple[datetime | None, datetime | None, dict]] = []
    upcoming: list[tuple[datetime | None, datetime | None, dict]] = []
    recent: list[tuple[datetime | None, datetime | None, dict]] = []

    for m in markets:
        if m.get("closed") is True or m.get("active") is False:
            continue

        start_utc, end_utc = _market_window_utc(m)

        if start_utc and end_utc and start_utc <= now_utc < end_utc:
            active.append((start_utc, end_utc, m))
            continue

        if start_utc and start_utc > now_utc:
            upcoming.append((start_utc, end_utc, m))
            continue

        if end_utc and end_utc <= now_utc:
            recent.append((start_utc, end_utc, m))

    if active:
        active.sort(key=lambda x: x[1] or datetime.max.replace(tzinfo=timezone.utc))
        return active[0][2], "active"

    if upcoming:
        upcoming.sort(key=lambda x: x[0] or datetime.max.replace(tzinfo=timezone.utc))
        return upcoming[0][2], "upcoming"

    if recent:
        recent.sort(key=lambda x: x[1] or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
        return recent[0][2], "recent"

    return (markets[0], "fallback") if markets else (None, "none")


def _format_market_window_text(market: dict) -> str:
    """Format window using API's startDate/endDate (may be unreliable)."""
    start_utc, end_utc = _market_window_utc(market)
    if not start_utc or not end_utc:
        return "Window: Unknown"

    start_et = start_utc.astimezone(MARKET_TZ).strftime("%I:%M %p").lstrip("0")
    end_et = end_utc.astimezone(MARKET_TZ).strftime("%I:%M %p").lstrip("0")
    start_ist = start_utc.astimezone(USER_TZ).strftime("%I:%M %p").lstrip("0")
    end_ist = end_utc.astimezone(USER_TZ).strftime("%I:%M %p").lstrip("0")
    return f"Window ET: {start_et} to {end_et} | IST: {start_ist} to {end_ist}"


def _format_window_from_slug(slug: str) -> str:
    """
    Compute the 5-minute window times directly from the slug timestamp.
    Much more reliable than the Gamma API's startDate/endDate fields.

    e.g. slug 'btc-updown-5m-1773438900' -> window 5:55 PM to 6:00 PM ET
    """
    try:
        ts = int(slug.split("-")[-1])
    except (ValueError, IndexError):
        return "Window: Unknown"

    start_utc = datetime.fromtimestamp(ts, tz=timezone.utc)
    end_utc = datetime.fromtimestamp(ts + WINDOW_SECONDS, tz=timezone.utc)

    start_et = start_utc.astimezone(MARKET_TZ).strftime("%I:%M %p").lstrip("0")
    end_et = end_utc.astimezone(MARKET_TZ).strftime("%I:%M %p").lstrip("0")
    start_ist = start_utc.astimezone(USER_TZ).strftime("%I:%M %p").lstrip("0")
    end_ist = end_utc.astimezone(USER_TZ).strftime("%I:%M %p").lstrip("0")
    return f"Window: {start_et} - {end_et} ET | {start_ist} - {end_ist} IST"


def _resolve_slug_from_args(args: list[str]) -> tuple[str | None, list[str]]:
    """
    Check if the first arg is a URL, a timestamp suffix, or just an outcome.
    Returns (slug_override_or_None, remaining_args).

    Accepted formats:
      /paper_buy Up 100                                     → auto-detect
      /paper_buy 1773434700 Up 100                          → specific timestamp
      /paper_buy https://polymarket.com/event/btc-updown-5m-1773434700 Up 100
    """
    if not args:
        return None, args

    first = args[0]

    # Full URL
    if first.startswith("http"):
        m = re.search(r"polymarket\.com/event/([^/?#]+)", first)
        if m:
            slug = m.group(1)
            if slug.startswith(TARGET_EVENT_SLUG_BASE):
                return slug, args[1:]
            else:
                return None, []  # signal unsupported
        return None, args[1:]

    # Pure numeric timestamp → build slug
    if first.isdigit() and len(first) >= 9:
        slug = f"{TARGET_EVENT_SLUG_BASE}-{first}"
        return slug, args[1:]

    # Otherwise it's probably the outcome directly
    return None, args


def _extract_buy_args(args: list[str]) -> tuple[str | None, str | None, float | None, str | None]:
    """
    Parse buy arguments. Returns (slug_override, outcome, amount_usd, error).

    Supports:
      /paper_buy Up 100
      /paper_buy 1773434700 Up 100
      /paper_buy https://polymarket.com/event/btc-updown-5m-1773434700 Up 100
    """
    if len(args) < 2:
        return None, None, None, None

    slug_override, remaining = _resolve_slug_from_args(args)

    if not remaining or len(remaining) < 2:
        # If we consumed the URL/timestamp but not enough args left
        if slug_override and len(remaining) == 0:
            return None, None, None, None  # just not enough args
        if len(remaining) < 2:
            return None, None, None, None

    outcome = remaining[0]
    amount_str = remaining[1]

    try:
        amount_usd = float(amount_str.replace("$", "").replace(",", ""))
        if amount_usd <= 0:
            raise ValueError
    except Exception:
        return None, None, None, "Invalid amount. Use a positive number like 10 or 100."

    return slug_override, outcome, amount_usd, None


def _extract_sell_args(args: list[str]) -> tuple[str | None, str | None, float | None, str | None]:
    """
    Parse sell arguments. Returns (slug_override, outcome, shares, error).

    Supports:
      /paper_sell Up 100
      /paper_sell 1773434700 Up 100
      /paper_sell https://polymarket.com/event/btc-updown-5m-1773434700 Up 100
    """
    if len(args) < 2:
        return None, None, None, None

    slug_override, remaining = _resolve_slug_from_args(args)

    if not remaining or len(remaining) < 2:
        return None, None, None, None

    outcome = remaining[0]
    shares_str = remaining[1].replace("$", "")

    try:
        shares = float(shares_str)
        if shares <= 0:
            raise ValueError
    except Exception:
        return None, None, None, "Invalid shares. Use a positive number."

    return slug_override, outcome, shares, None


def _quick_trade_keyboard(show_sell: bool = False) -> InlineKeyboardMarkup:
    amounts = [1, 10, 25, 50, 100]
    rows = [
        [
            InlineKeyboardButton(f"🟢 Up ${amt}", callback_data=f"qbuy:Up:{amt}"),
            InlineKeyboardButton(f"🔴 Down ${amt}", callback_data=f"qbuy:Down:{amt}"),
        ]
        for amt in amounts
    ]
    if show_sell:
        rows.append([
            InlineKeyboardButton("📤 Sell ALL Up", callback_data="qsell:Up:all"),
            InlineKeyboardButton("📤 Sell ALL Down", callback_data="qsell:Down:all"),
        ])
    rows.append([
        InlineKeyboardButton("💣 Sell ALL Positions", callback_data="qsell:all:all"),
    ])
    rows.append([InlineKeyboardButton("🔄 Refresh", callback_data="qbuy:refresh")])
    return InlineKeyboardMarkup(rows)


def _compute_current_5m_timestamp() -> int:
    """
    Compute the Unix timestamp for the CURRENT 5-minute block.
    Each block starts at a Unix timestamp that is an exact multiple of 300.
    """
    now = int(_time.time())
    return (now // 300) * 300


def _compute_5m_slug(ts: int) -> str:
    """Build the Polymarket event slug for a specific 5-minute window."""
    return f"{TARGET_EVENT_SLUG_BASE}-{ts}"


def _format_5m_et_label(ts: int) -> str:
    """
    Convert a 5m window timestamp to Polymarket's ET label.
    Polymarket labels by the END of the window: timestamp + 300.
    Returns e.g. '5:40 PM ET'.
    """
    end_utc = datetime.fromtimestamp(ts + WINDOW_SECONDS, tz=timezone.utc)
    end_et = end_utc.astimezone(MARKET_TZ)
    return end_et.strftime("%I:%M %p ET").lstrip("0")


def _is_market_tradeable(market: dict) -> bool:
    """Check if a market is still open for trading (not yet resolved)."""
    if market.get("closed") is True:
        return False
    if market.get("active") is False:
        return False
    # Check end date — if it has passed, market is resolved
    end_utc = _parse_iso_datetime(
        market.get("endDate") or market.get("endTime")
        or market.get("closeDate") or market.get("closedTime")
    )
    if end_utc and datetime.now(timezone.utc) > end_utc:
        return False
    return True


async def _get_target_market(slug_override: str | None = None) -> tuple[dict | None, str | None]:
    """
    Fetch the target BTC 5m market.

    - If slug_override is given, use that exact slug.
    - Otherwise, try multiple time windows to find the currently ACTIVE one:
        1. Current window (ts)      — the window currently being measured.
        2. Previous window (ts-300) — just resolved, fallback.
        3. Next window  (ts + 300)  — only if current doesn't exist yet.
    """
    if slug_override:
        url = f"https://polymarket.com/event/{slug_override}"
        event = await _get_market_data(url)
        if event and event.get("markets"):
            market = event["markets"][0]
            return market, "specific"
        return None, "Could not load BTC 5m market data."

    ts = _compute_current_5m_timestamp()
    # Try windows in priority order: current → previous → next
    candidates = [
        (ts,                  "auto"),
        (ts - WINDOW_SECONDS, "auto-prev"),
        (ts + WINDOW_SECONDS, "auto-next"),
    ]

    for candidate_ts, label in candidates:
        slug = _compute_5m_slug(candidate_ts)
        url = f"https://polymarket.com/event/{slug}"
        logger.info("Trying BTC 5m slug: %s (%s)", slug, label)
        event = await _get_market_data(url)
        if event and event.get("markets"):
            market = event["markets"][0]
            if _is_market_tradeable(market):
                logger.info("Found tradeable market: %s (mode=%s)", slug, label)
                return market, label
            else:
                logger.info("Market %s exists but is closed/resolved, skipping", slug)

    # Last resort: return any found market even if resolved
    for candidate_ts, label in candidates:
        slug = _compute_5m_slug(candidate_ts)
        url = f"https://polymarket.com/event/{slug}"
        event = await _get_market_data(url)
        if event and event.get("markets"):
            logger.warning("Using resolved market %s as fallback", slug)
            return event["markets"][0], f"{label}-resolved"

    return None, "Could not load BTC 5m market data."


async def cmd_quick_trade(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    db.init_paper_user(user_id)
    has_positions = bool(db.get_all_paper_positions(user_id))

    market, mode = await _get_target_market()
    if not market:
        await update.message.reply_text("Could not load quick trade market right now.")
        return

    title = market.get("question") or market.get("title") or "BTC Up/Down 5m"
    window_text = _format_market_window_text(market)
    balance = db.get_paper_balance(user_id)
    await update.message.reply_text(
        "⚡ *Quick Trade Buttons*\n"
        f"Market: *{_esc(str(title))}*\n"
        f"{_esc(window_text)}\n"
        f"Mode: `{_esc_code(mode)}`\n"
        f"💵 Balance: `${_esc_code(f'{balance:.2f}')}`\n\n"
        "Tap a button to BUY or SELL instantly\\."
        ,
        parse_mode="MarkdownV2",
        reply_markup=_quick_trade_keyboard(show_sell=has_positions),
    )


async def callback_quick_trade(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    payload = query.data or ""
    user_id = query.from_user.id

    # ── Refresh button ──
    if payload == "qbuy:refresh":
        db.init_paper_user(user_id)
        has_positions = bool(db.get_all_paper_positions(user_id))
        market, mode = await _get_target_market()
        if not market:
            await query.edit_message_text("Could not refresh market data right now.")
            return
        title = market.get("question") or market.get("title") or "BTC Up/Down 5m"
        window_text = _format_market_window_text(market)
        balance = db.get_paper_balance(user_id)
        await query.edit_message_text(
            "⚡ *Quick Trade Buttons*\n"
            f"Market: *{_esc(str(title))}*\n"
            f"{_esc(window_text)}\n"
            f"Mode: `{_esc_code(mode)}`\n"
            f"💵 Balance: `${_esc_code(f'{balance:.2f}')}`\n\n"
            "Tap a button to BUY or SELL instantly\\."
            ,
            parse_mode="MarkdownV2",
            reply_markup=_quick_trade_keyboard(show_sell=has_positions),
        )
        return

    # ── Sell button (qsell:outcome:amount_or_all) ──
    if payload.startswith("qsell:"):
        parts = payload.split(":")
        if len(parts) != 3:
            await query.edit_message_text("Invalid sell action.")
            return
        outcome_key = parts[1]  # "Up", "Down", or "all"
        amount_key = parts[2]   # "all" or a number

        if outcome_key == "all":
            # Sell ALL positions
            ok, message = await _paper_sellall_core(user_id)
        else:
            # Sell all shares of a specific outcome
            ok, message = await _paper_sell_core(user_id, outcome_key, None)  # None = sell all
        await query.edit_message_text(message, parse_mode="MarkdownV2")
        return

    # ── Buy button (qbuy:outcome:amount) ──
    parts = payload.split(":")
    if len(parts) != 3 or parts[0] != "qbuy":
        await query.edit_message_text("Invalid quick trade action.")
        return

    outcome = parts[1]
    try:
        amount_usd = float(parts[2])
    except Exception:
        await query.edit_message_text("Invalid amount in quick trade action.")
        return

    try:
        ok, message = await _paper_buy_core(user_id, outcome, amount_usd)
        try:
            await query.edit_message_text(message, parse_mode="MarkdownV2")
        except Exception as fmt_err:
            logger.warning("MarkdownV2 failed in quick_trade buy: %s", fmt_err)
            plain = message.replace("*", "").replace("`", "").replace("\\", "")
            await query.edit_message_text(plain)
    except Exception as exc:
        logger.error("Error in quick_trade buy: %s", exc, exc_info=True)
        await query.edit_message_text(f"\u274c Error: {exc}")

async def _get_market_data(url: str) -> dict | None:
    """Fetch event+market metadata from Gamma API using the URL slug."""
    match = re.search(r'polymarket\.com/event/([^/?#]+)', url)
    if not match: return None
    slug = match.group(1)
    async with aiohttp.ClientSession() as session:
        async with session.get(GAMMA_API_URL.format(slug=slug)) as resp:
            if resp.status != 200: return None
            data = await resp.json()
            if not data or not isinstance(data, list): return None
            return data[0]

async def _get_clob_price(session: aiohttp.ClientSession, token_id: str, side: str) -> float | None:
    """
    Fetch real-time price from CLOB order book.
    
    IMPORTANT — CLOB API direction is counter-intuitive:
      side='buy'  = what market makers are BIDDING (= price you receive when SELLING)
      side='sell' = what market makers are ASKING  (= price you PAY when BUYING)
    
    So call as:
      _get_clob_price(session, token_id, 'sell') → your buy/ask price
      _get_clob_price(session, token_id, 'buy')  → your sell/bid price
    """
    try:
        params = {"token_id": token_id, "side": side}
        async with session.get(CLOB_PRICE_URL, params=params) as r:
            if r.status == 200:
                d = await r.json()
                p = float(d.get("price", 0))
                return p if 0 < p < 1 else None
    except Exception:
        pass
    return None

async def _get_clob_midpoint(session: aiohttp.ClientSession, token_id: str) -> float | None:
    """Fetch the mid-market price (between bid and ask). Best for portfolio valuation."""
    try:
        async with session.get("https://clob.polymarket.com/midpoint", params={"token_id": token_id}) as r:
            if r.status == 200:
                d = await r.json()
                p = float(d.get("mid", 0))
                return p if 0 < p < 1 else None
    except Exception:
        pass
    return None

def _find_outcome_index(outcomes: list[str], target: str) -> int | None:
    target_lower = target.lower()
    for i, o in enumerate(outcomes):
        if o.lower() == target_lower: return i
    return None

async def cmd_paper_buy(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    slug_override, outcome, amount_usd, err = _extract_buy_args(context.args)
    if err:
        await update.message.reply_text(err)
        return
    if outcome is None or amount_usd is None:
        await update.message.reply_text(
            "Usage:\n"
            "`/paper_buy Up 100`\n"
            "`/paper_buy 1773434700 Up 100` \\(timestamp suffix\\)\n"
            "or `/paper_buy https://polymarket.com/event/btc-updown-5m-1773434700 Up 100`\n\n"
            "\U0001f4a1 You can just use the number after `5m\\-` in the URL\\!\n"
            "Also try `/quick_trade` for instant buttons\\.",
            parse_mode="MarkdownV2",
            reply_markup=_quick_trade_keyboard(),
        )
        return

    try:
        ok, message = await _paper_buy_core(update.effective_user.id, outcome, amount_usd, slug_override)
        try:
            await update.message.reply_text(message, parse_mode="MarkdownV2")
        except Exception as fmt_err:
            logger.warning("MarkdownV2 failed in paper_buy: %s", fmt_err)
            plain = message.replace("*", "").replace("`", "").replace("\\", "")
            await update.message.reply_text(plain)
    except Exception as exc:
        logger.error("Error in paper_buy: %s", exc, exc_info=True)
        await update.message.reply_text(f"\u274c Error executing buy: {exc}")


async def _paper_buy_core(user_id: int, outcome: str, amount_usd: float, slug_override: str | None = None) -> tuple[bool, str]:
    db.init_paper_user(user_id)

    market, mode = await _get_target_market(slug_override)
    if not market:
        return False, "Could not find a valid BTC 5m market right now\\."

    outcomes = json.loads(market.get("outcomes", "[]"))
    token_ids = json.loads(market.get("clobTokenIds", "[]"))
    gamma_prices = json.loads(market.get("outcomePrices", "[]"))

    idx = _find_outcome_index(outcomes, outcome)
    if idx is None:
        return False, f"Outcome not found\\. Available: {_esc(', '.join(outcomes))}"

    # Get real-time CLOB ASK price (what buyer pays)
    actual_outcome = outcomes[idx]
    price = None
    price_source = "gamma"
    if token_ids and idx < len(token_ids):
        async with aiohttp.ClientSession() as session:
            price = await _get_clob_price(session, token_ids[idx], "sell")
            if price is not None:
                price_source = "clob"
    
    # Fallback to Gamma API price
    if price is None:
        gamma_price = float(gamma_prices[idx]) if gamma_prices and idx < len(gamma_prices) else 0
        if 0 < gamma_price < 1:
            price = gamma_price
            price_source = "gamma"
        else:
            return False, "Could not fetch a valid price\\. Market may be closed or paused\\."
    else:
        price_source = "clob"

    shares_to_buy = amount_usd / price
    balance = db.get_paper_balance(user_id)
    if balance < amount_usd:
        return False, f"Insufficient funds\\. You have ${balance:.2f}\\."

    db.update_paper_balance(user_id, balance - amount_usd)
    slug = market.get("slug", "unknown")
    title = market.get("question", "Unknown")
    actual_outcome = outcomes[idx]
    
    existing = db.get_paper_position(user_id, slug, actual_outcome)
    if existing:
        old_shares = existing["shares"]
        old_avg = existing["avg_price"]
        total_cost = (old_shares * old_avg) + amount_usd
        new_shares = old_shares + shares_to_buy
        new_avg = total_cost / new_shares
    else:
        old_shares = 0.0
        old_avg = 0.0
        new_shares, new_avg = shares_to_buy, price
        
    db.upsert_paper_position(user_id, slug, title, actual_outcome, new_shares, new_avg)

    new_balance = balance - amount_usd
    total_value = new_shares * price  # current value at buy price
    source_tag = "CLOB live" if price_source == 'clob' else "Gamma est"
    window_text = _format_window_from_slug(slug)

    # Get the ET label (e.g. "5:40 PM ET") from the slug timestamp
    slug_parts = slug.split("-")
    try:
        slug_ts = int(slug_parts[-1])
        et_label = _format_5m_et_label(slug_ts)
    except (ValueError, IndexError):
        et_label = "Unknown"

    # Build current time strings
    now_utc = datetime.now(timezone.utc)
    now_ist = now_utc.astimezone(USER_TZ).strftime("%I:%M:%S %p IST").lstrip("0")
    now_et = now_utc.astimezone(MARKET_TZ).strftime("%I:%M:%S %p ET").lstrip("0")

    return True, (
        f"\u2705 *Paper Buy Executed\\!*\n\n"
        f"\U0001f4ca *Market:* {_esc(et_label)} window\n"
        f"\U0001f4dd *Name:* `{_esc_code(title)}`\n"
        f"{_esc(window_text)}\n"
        f"\U0001f3af *Outcome:* *{_esc(actual_outcome)}*\n\n"
        f"\U0001f4b0 *This Order:*\n"
        f"  Shares bought: `{_esc_code(f'{shares_to_buy:.2f}')}`\n"
        f"  Buy price: `${_esc_code(f'{price:.4f}')}` \\({_esc(source_tag)}\\)\n"
        f"  Cost: `${_esc_code(f'{amount_usd:.2f}')}`\n\n"
        f"\U0001f4c8 *Position Summary:*\n"
        f"  Total shares: `{_esc_code(f'{new_shares:.2f}')}`\n"
        f"  Avg price: `${_esc_code(f'{new_avg:.4f}')}`\n"
        f"  Position value: `${_esc_code(f'{total_value:.2f}')}`\n\n"
        f"\U0001f4b5 *Balance:* `${_esc_code(f'{new_balance:.2f}')}`\n"
        f"\u23f0 *Time:* `{_esc_code(now_et)}` / `{_esc_code(now_ist)}`\n"
        f"\U0001f517 *Slug:* `{_esc_code(slug)}`"
    )

async def _paper_sell_core(user_id: int, outcome: str, shares_to_sell: float | None = None, slug_override: str | None = None) -> tuple[bool, str]:
    """
    Core sell logic. If shares_to_sell is None, sell ALL shares of that outcome.
    """
    db.init_paper_user(user_id)

    market, mode = await _get_target_market(slug_override)
    if not market:
        return False, "Could not find a valid BTC 5m market right now\\."

    outcomes = json.loads(market.get("outcomes", "[]"))
    token_ids = json.loads(market.get("clobTokenIds", "[]"))
    gamma_prices = json.loads(market.get("outcomePrices", "[]"))
    
    idx = _find_outcome_index(outcomes, outcome)
    if idx is None:
        return False, f"Outcome not found\\. Available: {_esc(', '.join(outcomes))}"
    
    actual_outcome = outcomes[idx]
    slug = market.get("slug", "unknown")

    # Get real-time CLOB BID price (what seller receives)
    price = None
    price_source = "gamma"
    if token_ids and idx < len(token_ids):
        async with aiohttp.ClientSession() as session:
            price = await _get_clob_price(session, token_ids[idx], "buy")
            if price is not None:
                price_source = "clob"
    
    if price is None:
        gamma_price = float(gamma_prices[idx]) if gamma_prices and idx < len(gamma_prices) else 0
        if 0 < gamma_price < 1:
            price = gamma_price
        else:
            price = 0
    
    position = db.get_paper_position(user_id, slug, actual_outcome)
    if not position or position["shares"] <= 0:
        return False, f"You don't own any shares of {_esc(actual_outcome)}\\."

    # If shares_to_sell is None, sell all
    if shares_to_sell is None:
        shares_to_sell = position["shares"]

    if shares_to_sell > position["shares"]:
        return False, f"You only have {position['shares']:.2f} shares\\."
        
    proceeds = shares_to_sell * price
    balance = db.get_paper_balance(user_id)
    db.update_paper_balance(user_id, balance + proceeds)
    
    rem_shares = position["shares"] - shares_to_sell
    if rem_shares < 0.0001:
        db.remove_paper_position(position["id"])
    else:
        db.upsert_paper_position(user_id, slug, position["market_title"], actual_outcome, rem_shares, position["avg_price"])

    proceeds_str = f"{proceeds:.2f}"
    balance_str = f"{(balance + proceeds):.2f}"
    price_str = f"{price:.4f}"
    shares_str_fmt = f"{shares_to_sell:.2f}"
    source_note = " \\(CLOB live\\)" if price_source == "clob" else " \\(Gamma est\\)"
    window_text = _format_window_from_slug(slug)
    event_url = f"{TARGET_EVENT_URL_BASE}-{slug.split('-')[-1]}" if slug != "unknown" else TARGET_EVENT_URL_BASE
    
    pnl = (price - position["avg_price"]) * shares_to_sell
    pnl_sign = "+" if pnl >= 0 else "-"
    pnl_str = _esc_code(f"{pnl_sign}${abs(pnl):.2f}")
    
    return True, (
        f"\u2705 *Paper Sell Executed\\!*\n"
        f"Event: *{_esc('BTC Up/Down 5m')}*\n"
        f"URL: {_esc(event_url)}\n"
        f"Market: `{_esc_code(position['market_title'])}`\n"
        f"{_esc(window_text)}\n"
        f"Selection: `{_esc_code(mode)}`\n"
        f"Outcome: *{_esc(actual_outcome)}*\n"
        f"Shares: `{_esc_code(shares_str_fmt)}` @ `${_esc_code(price_str)}`{source_note}\n"
        f"Proceeds: `${_esc_code(proceeds_str)}` \\| PnL: `{pnl_str}`\n"
        f"Virtual Balance: `${_esc_code(balance_str)}`"
    )


async def cmd_paper_sell(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    slug_override, outcome, shares_to_sell, err = _extract_sell_args(context.args)
    if err:
        await update.message.reply_text(err)
        return
    if outcome is None or shares_to_sell is None:
        await update.message.reply_text(
            "Usage:\n"
            "`/paper_sell Up 100`\n"
            "`/paper_sell 1773434700 Up 100` \\(timestamp suffix\\)\n\n"
            "💡 You can just use the number after `5m\\-` in the URL\\!\n"
            "Also try `/sellall` to sell everything at once\\.",
            parse_mode="MarkdownV2",
        )
        return

    user_id = update.effective_user.id
    try:
        ok, message = await _paper_sell_core(user_id, outcome, shares_to_sell, slug_override)
        try:
            await update.message.reply_text(message, parse_mode="MarkdownV2")
        except Exception as fmt_err:
            logger.warning("MarkdownV2 failed in paper_sell: %s", fmt_err)
            plain = message.replace("*", "").replace("`", "").replace("\\", "")
            await update.message.reply_text(plain)
    except Exception as exc:
        logger.error("Error in paper_sell: %s", exc, exc_info=True)
        await update.message.reply_text(f"\u274c Error executing sell: {exc}")


async def _paper_sellall_core(user_id: int) -> tuple[bool, str]:
    """
    Sell ALL paper positions at current market prices.
    Frees all positions and returns cash to balance.
    """
    db.init_paper_user(user_id)
    positions = db.get_all_paper_positions(user_id)

    if not positions:
        return False, "You have no open positions to sell\\."

    total_proceeds = 0.0
    total_pnl = 0.0
    sold_lines = []
    balance = db.get_paper_balance(user_id)

    async with aiohttp.ClientSession() as session:
        for p in positions:
            slug = p["market_slug"]
            outcome = p["outcome"]
            shares = p["shares"]
            avg_price = p["avg_price"]

            # Try to get live BID price from CLOB
            price = None
            price_source = "gamma"
            try:
                api_url = GAMMA_API_URL.format(slug=slug)
                async with session.get(api_url) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        if data and isinstance(data, list) and data[0].get("markets"):
                            market = data[0]["markets"][0]
                            outcomes_list = json.loads(market.get("outcomes", "[]"))
                            token_ids = json.loads(market.get("clobTokenIds", "[]"))
                            gamma_prices_list = json.loads(market.get("outcomePrices", "[]"))
                            idx = _find_outcome_index(outcomes_list, outcome)
                            if idx is not None:
                                # Try CLOB BID price first
                                if token_ids and idx < len(token_ids):
                                    price = await _get_clob_price(session, token_ids[idx], "buy")
                                    if price is not None:
                                        price_source = "clob"
                                # Fallback to Gamma
                                if price is None and gamma_prices_list and idx < len(gamma_prices_list):
                                    gp = float(gamma_prices_list[idx])
                                    if 0 < gp < 1:
                                        price = gp
            except Exception:
                pass

            if price is None:
                price = avg_price  # worst case fallback: sell at your buy price
                price_source = "fallback"

            proceeds = shares * price
            pnl = (price - avg_price) * shares
            total_proceeds += proceeds
            total_pnl += pnl

            # Remove position from DB
            db.remove_paper_position(p["id"])

            pnl_sign = "+" if pnl >= 0 else "-"
            title_short = (p["market_title"] or "Unknown")[:35]
            sold_lines.append(
                f"  • {_esc(outcome)} in `{_esc_code(title_short)}` — "
                f"`{_esc_code(f'{shares:.2f}')}` sh @ `${_esc_code(f'{price:.4f}')}` "
                f"→ `{_esc_code(f'{pnl_sign}${abs(pnl):.2f}')}`"
            )

    # Update balance
    new_balance = balance + total_proceeds
    db.update_paper_balance(user_id, new_balance)

    total_pnl_sign = "+" if total_pnl >= 0 else "-"
    msg_lines = [
        f"💣 *All Positions Sold\\!*\n",
        f"📦 *Positions closed:* {len(positions)}",
    ] + sold_lines + [
        f"\n💰 *Total Proceeds:* `${_esc_code(f'{total_proceeds:.2f}')}`",
        f"📊 *Total PnL:* `{_esc_code(f'{total_pnl_sign}${abs(total_pnl):.2f}')}`",
        f"💵 *New Balance:* `${_esc_code(f'{new_balance:.2f}')}`",
    ]

    return True, "\n".join(msg_lines)


async def cmd_sellall(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Sell all paper positions at current market prices."""
    user_id = update.effective_user.id
    try:
        ok, message = await _paper_sellall_core(user_id)
        try:
            await update.message.reply_text(message, parse_mode="MarkdownV2", reply_markup=_main_menu_keyboard())
        except Exception as fmt_err:
            logger.warning("MarkdownV2 failed in sellall: %s", fmt_err)
            plain = message.replace("*", "").replace("`", "").replace("\\", "")
            await update.message.reply_text(plain, reply_markup=_main_menu_keyboard())
    except Exception as exc:
        logger.error("Error in sellall: %s", exc, exc_info=True)
        await update.message.reply_text(f"\u274c Error: {exc}", reply_markup=_main_menu_keyboard())

async def cmd_portfolio(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    db.init_paper_user(user_id)
    
    balance = db.get_paper_balance(user_id)
    positions = db.get_all_paper_positions(user_id)
    
    if not positions:
        return await update.message.reply_text(f"📜 *Virtual Portfolio*\n\n💵 Cash: `${_esc_code(f'{balance:.2f}')}`\n\nYou have no active positions\\.", parse_mode="MarkdownV2", reply_markup=_main_menu_keyboard())
        
    sent = await update.message.reply_text("Fetching live prices\\.\\.\\.", parse_mode="MarkdownV2")
    
    lines = [f"\U0001fDDC *VIRTUAL PORTFOLIO*\n", f"\U0001f4b5 Cash: `${_esc_code(f'{balance:.2f}')}`\n"]
    total_val = balance
    
    async with aiohttp.ClientSession() as session:
        for p in positions:
            curr_val = p['shares'] * p['avg_price'] # fallback
            price_str = "UNKNOWN"
            pnl_str = "$0.00 (+0.00%)"
            
            try:
                # Fetch metadata+token IDs from Gamma, then midpoint from CLOB
                async with session.get(GAMMA_API_URL.format(slug=p['market_slug'])) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        if data and isinstance(data, list) and data[0].get("markets"):
                            market = data[0]["markets"][0]
                            outcomes = json.loads(market.get("outcomes", "[]"))
                            token_ids = json.loads(market.get("clobTokenIds", "[]"))
                            gamma_prices_list = json.loads(market.get("outcomePrices", "[]"))
                            idx = _find_outcome_index(outcomes, p['outcome'])
                            if idx is not None:
                                # Use CLOB midpoint for fairest portfolio valuation
                                curr_price = None
                                if token_ids and idx < len(token_ids):
                                    curr_price = await _get_clob_midpoint(session, token_ids[idx])
                                # Fallback to Gamma cache price
                                if curr_price is None and gamma_prices_list and idx < len(gamma_prices_list):
                                    gp = float(gamma_prices_list[idx])
                                    if 0 < gp < 1:
                                        curr_price = gp
                                if curr_price is not None:
                                    curr_val = curr_price * p['shares']
                                    cost = p['avg_price'] * p['shares']
                                    pnl = curr_val - cost
                                    pnl_pct = (pnl/cost)*100 if cost > 0 else 0
                                    price_str = f"${curr_price:.4f}"
                                    pnl_sign = "+" if pnl >= 0 else "-"
                                    pnl_str = f"{pnl_sign}${abs(pnl):.2f} ({pnl_sign}{abs(pnl_pct):.1f}%)"
            except Exception: pass
            
            total_val += curr_val
            
            title = p['market_title'][:40] + ("..." if len(p['market_title']) > 40 else "")
            lines.append(f"\U0001f539 *{_esc(title)}*")
            lines.append(f"  {_esc(p['outcome'])} \\| `{_esc_code(f'{p['shares']:.2f}')}` sh")
            lines.append(f"  Avg: `${_esc_code(f'{p['avg_price']:.4f}')}` \\| Cur: `{_esc_code(price_str)}`")
            lines.append(f"  Val: `${_esc_code(f'{curr_val:.2f}')}` \\| PnL: `{_esc_code(pnl_str)}`\n")
            
    total_pnl = total_val - 100.0
    pnl_sign = "+" if total_pnl >= 0 else "-"
    lines.append(f"\U0001f3e6 *Total Value:* `${_esc_code(f'{total_val:.2f}')}`")
    lines.append(f"\U0001f4c8 *All\\-Time PnL:* `{_esc_code(f'{pnl_sign}${abs(total_pnl):.2f}')}`")
    
    try:
        await sent.edit_text("\n".join(lines), parse_mode="MarkdownV2")
    except Exception as e:
        logger.error(f"Error formatting portfolio view: {e}")
        await sent.edit_text("Error formatting portfolio view\\.", parse_mode="MarkdownV2")

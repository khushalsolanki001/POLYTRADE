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
from datetime import datetime, timezone

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
    ["\u2795 Add Wallet", "\U0001f4cb My Wallets", "\U0001f5d1\ufe0f Remove Wallet", "\U0001f550 History", "\U0001f4bc Portfolio", "\u2753 Help"]
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
            ["\U0001f5d1\ufe0f Remove Wallet", "\u2753 Help"],
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
        "You have `$10,000` virtual USD to play with\\! Try these commands:\n"
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
            r"|\U0001f550 History|\u2753 Help|\u274c Cancel)$"
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
    if len(context.args) < 3:
        await update.message.reply_text("Usage: `/paper_buy <url> <outcome> <amount_usd>`\nExample: `/paper_buy https://polymarket.com/event/slug Yes 100`", parse_mode="MarkdownV2")
        return
    url, outcome, amount_str = context.args[0], context.args[1], context.args[2].replace('$', '')
    
    try: amount_usd = float(amount_str)
    except: return await update.message.reply_text("Invalid amount.")
    
    user_id = update.effective_user.id
    db.init_paper_user(user_id)
    
    event = await _get_market_data(url)
    if not event or not event.get("markets"):
        return await update.message.reply_text("Market not found.")
    market = event["markets"][0]
    outcomes = json.loads(market.get("outcomes", "[]"))
    token_ids = json.loads(market.get("clobTokenIds", "[]"))
    gamma_prices = json.loads(market.get("outcomePrices", "[]"))
    
    idx = _find_outcome_index(outcomes, outcome)
    if idx is None:
        return await update.message.reply_text(f"Outcome not found\. Available: {_esc(', '.join(outcomes))}", parse_mode="MarkdownV2")
    
    # Get real-time CLOB ASK price (what buyer pays)
    # CLOB API: side='sell' = what sellers are asking = your cost to buy
    actual_outcome = outcomes[idx]
    price = None
    price_source = "gamma"
    if token_ids and idx < len(token_ids):
        async with aiohttp.ClientSession() as session:
            price = await _get_clob_price(session, token_ids[idx], "sell")  # 'sell' = ask = what YOU pay
            if price is not None:
                price_source = "clob"
    
    # Fallback to Gamma API price if CLOB unavailable
    if price is None:
        gamma_price = float(gamma_prices[idx]) if gamma_prices and idx < len(gamma_prices) else 0
        if 0 < gamma_price < 1:
            price = gamma_price
            price_source = "gamma"
        else:
            return await update.message.reply_text("Could not fetch a valid price for this market\. It may be closed or paused\.", parse_mode="MarkdownV2")
    else:
        price_source = "clob"
        
    shares_to_buy = amount_usd / price
    balance = db.get_paper_balance(user_id)
    if balance < amount_usd:
        return await update.message.reply_text(f"Insufficient funds. You have ${balance:.2f}.")
        
    db.update_paper_balance(user_id, balance - amount_usd)
    slug = market.get("slug", "unknown")
    title = market.get("question", "Unknown")
    actual_outcome = outcomes[idx]
    
    existing = db.get_paper_position(user_id, slug, actual_outcome)
    if existing:
        total_cost = (existing["shares"] * existing["avg_price"]) + amount_usd
        new_shares = existing["shares"] + shares_to_buy
        new_avg = total_cost / new_shares
    else:
        new_shares, new_avg = shares_to_buy, price
        
    db.upsert_paper_position(user_id, slug, title, actual_outcome, new_shares, new_avg)
    amount_usd_str = f"{amount_usd:.2f}"
    balance_str = f"{(balance - amount_usd):.2f}"
    price_str = f"{price:.4f}"
    shares_str = f"{shares_to_buy:.2f}"
    source_note = " \\(CLOB live\\)" if price_source == 'clob' else " \\(Gamma est\\)"
    
    await update.message.reply_text(
        f"\u2705 *Paper Buy Executed\\!*\n"
        f"Market: `{_esc_code(title)}`\n"
        f"Outcome: *{_esc(actual_outcome)}*\n"
        f"Shares: `{_esc_code(shares_str)}` @ `${_esc_code(price_str)}`{source_note}\n"
        f"Cost: `${_esc_code(amount_usd_str)}`\n"
        f"Virtual Balance: `${_esc_code(balance_str)}`",
        parse_mode="MarkdownV2"
    )

async def cmd_paper_sell(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if len(context.args) < 3:
        await update.message.reply_text("Usage: `/paper_sell <url> <outcome> <shares>`\nExample: `/paper_sell https://polymarket.com/event/slug Yes 100`", parse_mode="MarkdownV2")
        return
    url, outcome, shares_str = context.args[0], context.args[1], context.args[2].replace('$', '')
    
    try: shares_to_sell = float(shares_str)
    except: return await update.message.reply_text("Invalid amount.")
    
    user_id = update.effective_user.id
    db.init_paper_user(user_id)
    
    event = await _get_market_data(url)
    if not event or not event.get("markets"):
        return await update.message.reply_text("Market not found\.")
    market = event["markets"][0]
    outcomes = json.loads(market.get("outcomes", "[]"))
    token_ids = json.loads(market.get("clobTokenIds", "[]"))
    gamma_prices = json.loads(market.get("outcomePrices", "[]"))
    
    idx = _find_outcome_index(outcomes, outcome)
    if idx is None:
        return await update.message.reply_text(f"Outcome not found\. Available: {_esc(', '.join(outcomes))}", parse_mode="MarkdownV2")
    
    actual_outcome = outcomes[idx]
    slug = market.get("slug", "unknown")

    # Get real-time CLOB BID price (what seller receives)
    # CLOB API: side='buy' = what buyers are bidding = what you receive when you sell
    price = None
    price_source = "gamma"
    if token_ids and idx < len(token_ids):
        async with aiohttp.ClientSession() as session:
            price = await _get_clob_price(session, token_ids[idx], "buy")  # 'buy' = bid = what YOU receive
            if price is not None:
                price_source = "clob"
    
    if price is None:
        gamma_price = float(gamma_prices[idx]) if gamma_prices and idx < len(gamma_prices) else 0
        if 0 < gamma_price < 1:
            price = gamma_price
        else:
            price = 0  # resolved market — treat as 0 or 1
    
    position = db.get_paper_position(user_id, slug, actual_outcome)
    if not position or position["shares"] <= 0:
        return await update.message.reply_text("You don't own any shares of this outcome.")
    if shares_to_sell > position["shares"]:
        return await update.message.reply_text(f"You only have {position['shares']:.2f} shares.")
        
    proceeds = shares_to_sell * price
    balance = db.get_paper_balance(user_id)
    db.update_paper_balance(user_id, balance + proceeds)
    
    rem_shares = position["shares"] - shares_to_sell
    if rem_shares < 0.0001: db.remove_paper_position(position["id"])
    else: db.upsert_paper_position(user_id, slug, position["market_title"], actual_outcome, rem_shares, position["avg_price"])
    proceeds_str = f"{proceeds:.2f}"
    balance_str = f"{(balance + proceeds):.2f}"
    price_str = f"{price:.4f}"
    shares_str_fmt = f"{shares_to_sell:.2f}"
    avg_price_str = f"{position['avg_price']:.4f}"
    source_note = " \\(CLOB live\\)" if price_source == "clob" else " \\(Gamma est\\)"
    
    pnl = (price - position["avg_price"]) * shares_to_sell
    pnl_sign = "+" if pnl >= 0 else "-"
    pnl_str = _esc_code(f"{pnl_sign}${abs(pnl):.2f}")
    
    await update.message.reply_text(
        f"\u2705 *Paper Sell Executed\\!*\n"
        f"Market: `{_esc_code(position['market_title'])}`\n"
        f"Outcome: *{_esc(actual_outcome)}*\n"
        f"Shares: `{_esc_code(shares_str_fmt)}` @ `${_esc_code(price_str)}`{source_note}\n"
        f"Proceeds: `${_esc_code(proceeds_str)}` \\| PnL: `{pnl_str}`\n"
        f"Virtual Balance: `${_esc_code(balance_str)}`",
        parse_mode="MarkdownV2"
    )

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
            
    total_pnl = total_val - 10000.0
    pnl_sign = "+" if total_pnl >= 0 else "-"
    lines.append(f"\U0001f3e6 *Total Value:* `${_esc_code(f'{total_val:.2f}')}`")
    lines.append(f"\U0001f4c8 *All\\-Time PnL:* `{_esc_code(f'{pnl_sign}${abs(total_pnl):.2f}')}`")
    
    try:
        await sent.edit_text("\n".join(lines), parse_mode="MarkdownV2")
    except Exception as e:
        logger.error(f"Error formatting portfolio view: {e}")
        await sent.edit_text("Error formatting portfolio view\\.", parse_mode="MarkdownV2")

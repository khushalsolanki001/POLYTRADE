"""
desktop_app.py — PolyTrade Mini Desktop App
============================================
A sleek, floating Windows mini-app for Polymarket paper trading.
Features:
  • Live price chart with candlestick-style price history
  • Buy / Sell panel with outcome selection
  • Portfolio view with live unrealized PnL
  • Trade history log
  • Auto-refreshing live prices
  • Connects to existing db.py + Polymarket Gamma API
"""

import sys
import os
import json
import re
import threading
import time
import asyncio
import requests
from datetime import datetime, timedelta
from collections import deque

import customtkinter as ctk
from customtkinter import CTkFont
import matplotlib
matplotlib.use("TkAgg")
import matplotlib.pyplot as plt
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib.figure import Figure
import numpy as np

# ── Make sure imports work from project root ──────────────────────────────────
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import db

# ── App theme ─────────────────────────────────────────────────────────────────
ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")

GAMMA_API       = "https://gamma-api.polymarket.com/events?slug={slug}"
GAMMA_MARKET_SEARCH = "https://gamma-api.polymarket.com/markets?active=true&limit=100&question={q}"

DESKTOP_USER_ID = 9999  # Dedicated ID for the desktop app user

# ── Color Palette ─────────────────────────────────────────────────────────────
BG_DARK    = "#0d1117"
BG_CARD    = "#161b22"
BG_PANEL   = "#21262d"
ACCENT     = "#58a6ff"
GREEN      = "#3fb950"
RED        = "#f85149"
GOLD       = "#e3b341"
TEXT_MAIN  = "#e6edf3"
TEXT_DIM   = "#8b949e"
BORDER     = "#30363d"

CHART_BG   = "#0d1117"
CHART_LINE = "#58a6ff"
CHART_FILL = "#1c2a3a"

# ─────────────────────────────────────────────────────────────────────────────
#  Polymarket API helpers (sync, using requests for simplicity in threads)
# ─────────────────────────────────────────────────────────────────────────────

def fetch_market_by_slug(slug: str) -> dict | None:
    """Fetch event+market data for a given slug."""
    try:
        r = requests.get(GAMMA_API.format(slug=slug), timeout=10)
        if r.status_code == 200:
            data = r.json()
            if data and isinstance(data, list):
                return data[0]
    except Exception:
        pass
    return None

def search_markets(query: str) -> list[dict]:
    """Search active markets by keyword."""
    try:
        url = f"https://gamma-api.polymarket.com/markets?active=true&limit=30"
        r = requests.get(url, timeout=10)
        if r.status_code == 200:
            markets = r.json()
            q_lower = query.lower()
            return [m for m in markets if q_lower in m.get("question","").lower()][:10]
    except Exception:
        pass
    return []

def extract_slug(url: str) -> str | None:
    match = re.search(r'polymarket\.com/event/([^/?#]+)', url)
    return match.group(1) if match else None

def get_event_data(url_or_slug: str) -> dict | None:
    """Accept a full URL or just a slug."""
    slug = extract_slug(url_or_slug) or url_or_slug.strip()
    return fetch_market_by_slug(slug)


# ─────────────────────────────────────────────────────────────────────────────
#  Price history simulation (for chart)
#  We store rolling price snapshots per market+outcome
# ─────────────────────────────────────────────────────────────────────────────

class PriceHistory:
    def __init__(self, maxlen=60):
        self._data: dict[str, deque] = {}

    def add(self, key: str, price: float, maxlen=60):
        if key not in self._data:
            self._data[key] = deque(maxlen=maxlen)
        self._data[key].append((time.time(), price))

    def get(self, key: str) -> list[tuple]:
        return list(self._data.get(key, []))

price_history = PriceHistory()


# ─────────────────────────────────────────────────────────────────────────────
#  Main App Window
# ─────────────────────────────────────────────────────────────────────────────

class PolyTradeApp(ctk.CTk):
    def __init__(self):
        super().__init__()

        db.init_db()
        db.init_paper_user(DESKTOP_USER_ID, starting_balance=1000.0)

        self.title("⬡ PolyTrade  —  Paper Trading")
        self.geometry("960x680")
        self.minsize(860, 600)
        self.configure(fg_color=BG_DARK)
        self.resizable(True, True)

        # State
        self._current_market  = None   # dict: full event data from API
        self._current_markets = []     # list of sub-markets inside event
        self._current_outcomes = []    # e.g. ["Yes","No"] or ["Up","Down"]
        self._current_prices   = []    # parallel list of floats
        self._selected_outcome = ctk.StringVar(value="")
        self._refresh_thread   = None
        self._stop_refresh     = threading.Event()

        self._build_ui()
        self._start_auto_refresh()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ── UI Building ──────────────────────────────────────────────────────────

    def _build_ui(self):
        # ── Header bar ────────────────────────────────────────────────────
        hdr = ctk.CTkFrame(self, fg_color=BG_CARD, corner_radius=0, height=52)
        hdr.pack(fill="x", side="top")
        hdr.pack_propagate(False)

        logo = ctk.CTkLabel(hdr, text="⬡ PolyTrade",
                            font=CTkFont(family="Segoe UI", size=18, weight="bold"),
                            text_color=ACCENT)
        logo.pack(side="left", padx=16, pady=12)

        subtitle = ctk.CTkLabel(hdr, text="Paper Trading · Polymarket",
                                font=CTkFont(family="Segoe UI", size=11),
                                text_color=TEXT_DIM)
        subtitle.pack(side="left", padx=4, pady=12)

        self._balance_lbl = ctk.CTkLabel(hdr, text="💵 $1,000.00",
                                          font=CTkFont(family="Segoe UI", size=13, weight="bold"),
                                          text_color=GREEN)
        self._balance_lbl.pack(side="right", padx=16, pady=12)

        bal_title = ctk.CTkLabel(hdr, text="Virtual Balance:",
                                  font=CTkFont(family="Segoe UI", size=11),
                                  text_color=TEXT_DIM)
        bal_title.pack(side="right", padx=0, pady=12)

        # ── Separator ─────────────────────────────────────────────────────
        sep = ctk.CTkFrame(self, fg_color=BORDER, height=1, corner_radius=0)
        sep.pack(fill="x")

        # ── Tab view (main area) ───────────────────────────────────────────
        self._tabs = ctk.CTkTabview(self,
                                     fg_color=BG_DARK,
                                     segmented_button_fg_color=BG_CARD,
                                     segmented_button_selected_color=ACCENT,
                                     segmented_button_selected_hover_color="#4a9aff",
                                     segmented_button_unselected_color=BG_CARD,
                                     border_color=BORDER,
                                     border_width=1)
        self._tabs.pack(fill="both", expand=True, padx=10, pady=(8,10))

        self._tabs.add("📈  Trade")
        self._tabs.add("📁  Portfolio")
        self._tabs.add("📜  History")
        self._tabs.add("🔍  Search")

        self._build_trade_tab(self._tabs.tab("📈  Trade"))
        self._build_portfolio_tab(self._tabs.tab("📁  Portfolio"))
        self._build_history_tab(self._tabs.tab("📜  History"))
        self._build_search_tab(self._tabs.tab("🔍  Search"))

    # ── Trade Tab ────────────────────────────────────────────────────────────

    def _build_trade_tab(self, parent):
        parent.configure(fg_color=BG_DARK)

        # Left: input panel
        left = ctk.CTkFrame(parent, fg_color=BG_CARD, corner_radius=12, border_color=BORDER, border_width=1)
        left.pack(side="left", fill="y", padx=(0,6), pady=0)
        left.pack_propagate(False)
        left.configure(width=300)

        # Market URL entry
        _section_label(left, "MARKET URL OR SLUG")
        self._url_entry = ctk.CTkEntry(left, placeholder_text="polymarket.com/event/...",
                                        height=36, corner_radius=8,
                                        fg_color=BG_PANEL, border_color=BORDER,
                                        text_color=TEXT_MAIN,
                                        placeholder_text_color=TEXT_DIM)
        self._url_entry.pack(fill="x", padx=12, pady=(2,8))

        load_btn = ctk.CTkButton(left, text="⟳  Load Market",
                                  height=36, corner_radius=8,
                                  fg_color=ACCENT, hover_color="#4a9aff",
                                  font=CTkFont(size=12, weight="bold"),
                                  command=self._load_market)
        load_btn.pack(fill="x", padx=12, pady=(0,12))

        # Market info display
        _section_label(left, "MARKET INFO")
        self._market_title_lbl = ctk.CTkLabel(left, text="No market loaded",
                                               wraplength=260, justify="left",
                                               font=CTkFont(size=11),
                                               text_color=TEXT_DIM)
        self._market_title_lbl.pack(fill="x", padx=12, pady=(2,6))

        # Outcome selector
        _section_label(left, "SELECT OUTCOME")
        self._outcome_frame = ctk.CTkFrame(left, fg_color="transparent")
        self._outcome_frame.pack(fill="x", padx=12, pady=(2,8))

        # Amount
        _section_label(left, "AMOUNT (USD $)")
        self._amount_entry = ctk.CTkEntry(left, placeholder_text="e.g.  25",
                                           height=36, corner_radius=8,
                                           fg_color=BG_PANEL, border_color=BORDER,
                                           text_color=TEXT_MAIN,
                                           placeholder_text_color=TEXT_DIM)
        self._amount_entry.pack(fill="x", padx=12, pady=(2,8))

        # Buy / Sell buttons
        btn_row = ctk.CTkFrame(left, fg_color="transparent")
        btn_row.pack(fill="x", padx=12, pady=(0,8))

        buy_btn = ctk.CTkButton(btn_row, text="🟢 BUY",
                                 height=40, corner_radius=8,
                                 fg_color="#1a3a1a", hover_color="#2a5a2a",
                                 border_color=GREEN, border_width=1,
                                 text_color=GREEN,
                                 font=CTkFont(size=13, weight="bold"),
                                 command=self._do_buy)
        buy_btn.pack(side="left", expand=True, fill="x", padx=(0,4))

        sell_btn = ctk.CTkButton(btn_row, text="🔴 SELL",
                                  height=40, corner_radius=8,
                                  fg_color="#3a1a1a", hover_color="#5a2a2a",
                                  border_color=RED, border_width=1,
                                  text_color=RED,
                                  font=CTkFont(size=13, weight="bold"),
                                  command=self._do_sell)
        sell_btn.pack(side="left", expand=True, fill="x", padx=(4,0))

        # Status message
        self._status_lbl = ctk.CTkLabel(left, text="",
                                         wraplength=270, justify="center",
                                         font=CTkFont(size=11),
                                         text_color=GREEN)
        self._status_lbl.pack(fill="x", padx=12, pady=4)

        # Prices display
        _section_label(left, "LIVE PRICES")
        self._prices_frame = ctk.CTkScrollableFrame(left, fg_color=BG_PANEL, corner_radius=8,
                                                     border_color=BORDER, border_width=1,
                                                     height=100)
        self._prices_frame.pack(fill="x", padx=12, pady=(2,8))
        self._price_labels: dict[str, ctk.CTkLabel] = {}

        # Right: chart
        right = ctk.CTkFrame(parent, fg_color=BG_DARK)
        right.pack(side="left", fill="both", expand=True)

        chart_card = ctk.CTkFrame(right, fg_color=BG_CARD, corner_radius=12,
                                   border_color=BORDER, border_width=1)
        chart_card.pack(fill="both", expand=True, pady=(0,0))

        self._chart_title = ctk.CTkLabel(chart_card, text="Price History",
                                          font=CTkFont(size=12, weight="bold"),
                                          text_color=TEXT_DIM)
        self._chart_title.pack(anchor="nw", padx=16, pady=(10,0))

        self._fig = Figure(figsize=(5, 3.6), dpi=100, facecolor=CHART_BG)
        self._ax  = self._fig.add_subplot(111)
        self._ax.set_facecolor(CHART_BG)
        self._style_axis(self._ax)
        self._fig.tight_layout(pad=1.5)

        self._canvas = FigureCanvasTkAgg(self._fig, master=chart_card)
        self._canvas.get_tk_widget().pack(fill="both", expand=True, padx=6, pady=6)

        self._draw_empty_chart()

    # ── Portfolio Tab ─────────────────────────────────────────────────────────

    def _build_portfolio_tab(self, parent):
        parent.configure(fg_color=BG_DARK)

        top_bar = ctk.CTkFrame(parent, fg_color="transparent")
        top_bar.pack(fill="x", pady=(0,8))

        ctk.CTkLabel(top_bar, text="Open Positions",
                      font=CTkFont(size=14, weight="bold"), text_color=TEXT_MAIN
                      ).pack(side="left")

        refresh_btn = ctk.CTkButton(top_bar, text="⟳ Refresh",
                                     width=90, height=30, corner_radius=6,
                                     fg_color=BG_PANEL, hover_color=BG_CARD,
                                     border_color=BORDER, border_width=1,
                                     text_color=ACCENT,
                                     font=CTkFont(size=11),
                                     command=self._refresh_portfolio)
        refresh_btn.pack(side="right")

        # Summary row
        self._port_summary = ctk.CTkFrame(parent, fg_color=BG_CARD, corner_radius=10,
                                           border_color=BORDER, border_width=1, height=60)
        self._port_summary.pack(fill="x", pady=(0,8))
        self._port_summary.pack_propagate(False)

        self._port_cash_lbl  = _stat_label(self._port_summary, "Cash", "$0.00", GREEN)
        self._port_value_lbl = _stat_label(self._port_summary, "Positions", "$0.00", ACCENT)
        self._port_total_lbl = _stat_label(self._port_summary, "Total", "$0.00", GOLD)
        self._port_pnl_lbl   = _stat_label(self._port_summary, "Unrealized PnL", "$0.00", GREEN)

        # Positions list
        self._port_scroll = ctk.CTkScrollableFrame(parent, fg_color=BG_CARD,
                                                    corner_radius=10,
                                                    border_color=BORDER, border_width=1)
        self._port_scroll.pack(fill="both", expand=True)

        self._port_rows: list[ctk.CTkFrame] = []
        self._refresh_portfolio()

    # ── History Tab ───────────────────────────────────────────────────────────

    def _build_history_tab(self, parent):
        parent.configure(fg_color=BG_DARK)

        ctk.CTkLabel(parent, text="Closed Trades",
                      font=CTkFont(size=14, weight="bold"), text_color=TEXT_MAIN
                      ).pack(anchor="w", pady=(0,8))

        # Stats row
        stats_row = ctk.CTkFrame(parent, fg_color=BG_CARD, corner_radius=10,
                                  border_color=BORDER, border_width=1, height=60)
        stats_row.pack(fill="x", pady=(0,8))
        stats_row.pack_propagate(False)

        self._hist_trades_lbl  = _stat_label(stats_row, "Total Trades", "0", ACCENT)
        self._hist_wins_lbl    = _stat_label(stats_row, "Wins", "0", GREEN)
        self._hist_losses_lbl  = _stat_label(stats_row, "Losses", "0", RED)
        self._hist_total_pnl   = _stat_label(stats_row, "Total PnL", "$0.00", GOLD)

        # History scroll
        self._hist_scroll = ctk.CTkScrollableFrame(parent, fg_color=BG_CARD,
                                                    corner_radius=10,
                                                    border_color=BORDER, border_width=1)
        self._hist_scroll.pack(fill="both", expand=True)

        self._load_history()

    # ── Search Tab ────────────────────────────────────────────────────────────

    def _build_search_tab(self, parent):
        parent.configure(fg_color=BG_DARK)

        ctk.CTkLabel(parent, text="Find Markets",
                      font=CTkFont(size=14, weight="bold"), text_color=TEXT_MAIN
                      ).pack(anchor="w", pady=(0,8))

        search_row = ctk.CTkFrame(parent, fg_color="transparent")
        search_row.pack(fill="x", pady=(0,8))

        self._search_entry = ctk.CTkEntry(search_row, placeholder_text="Search markets (e.g. bitcoin, trump...)",
                                           height=38, corner_radius=8,
                                           fg_color=BG_PANEL, border_color=BORDER,
                                           text_color=TEXT_MAIN,
                                           placeholder_text_color=TEXT_DIM)
        self._search_entry.pack(side="left", fill="x", expand=True, padx=(0,8))
        self._search_entry.bind("<Return>", lambda e: self._do_search())

        ctk.CTkButton(search_row, text="Search", width=80, height=38,
                       corner_radius=8, fg_color=ACCENT, hover_color="#4a9aff",
                       font=CTkFont(size=12, weight="bold"),
                       command=self._do_search).pack(side="left")

        self._search_status = ctk.CTkLabel(parent, text="Enter a keyword above to find active Polymarket markets.",
                                            font=CTkFont(size=11), text_color=TEXT_DIM)
        self._search_status.pack(anchor="w", pady=(0,4))

        self._search_scroll = ctk.CTkScrollableFrame(parent, fg_color=BG_CARD,
                                                      corner_radius=10,
                                                      border_color=BORDER, border_width=1)
        self._search_scroll.pack(fill="both", expand=True)

    # ─────────────────────────────────────────────────────────────────────────
    #  Actions
    # ─────────────────────────────────────────────────────────────────────────

    def _load_market(self):
        url = self._url_entry.get().strip()
        if not url:
            self._set_status("Please enter a market URL or slug.", RED)
            return
        self._set_status("Loading market…", TEXT_DIM)
        threading.Thread(target=self._load_market_thread, args=(url,), daemon=True).start()

    def _load_market_thread(self, url: str):
        event = get_event_data(url)
        if not event:
            self.after(0, lambda: self._set_status("❌ Market not found. Check the URL.", RED))
            return

        markets = event.get("markets", [])
        if not markets:
            self.after(0, lambda: self._set_status("❌ No sub-markets in this event.", RED))
            return

        self.after(0, lambda: self._apply_market(event, markets))

    def _apply_market(self, event: dict, markets: list):
        self._current_market  = event
        self._current_markets = markets
        market = markets[0]

        try:
            outcomes = json.loads(market.get("outcomes", "[]"))
            prices   = [float(p) for p in json.loads(market.get("outcomePrices", "[]"))]
        except Exception:
            outcomes, prices = [], []

        self._current_outcomes = outcomes
        self._current_prices   = prices

        title = market.get("question", event.get("title", "Unknown"))
        self._market_title_lbl.configure(text=title, text_color=TEXT_MAIN)
        self._chart_title.configure(text=title[:60] + ("…" if len(title) > 60 else ""))

        # Rebuild outcome buttons
        for w in self._outcome_frame.winfo_children():
            w.destroy()
        self._price_labels.clear()

        if outcomes:
            self._selected_outcome.set(outcomes[0])

        for i, (o, p) in enumerate(zip(outcomes, prices)):
            color = GREEN if o.lower() in ("yes","up","higher") else RED
            rb = ctk.CTkRadioButton(self._outcome_frame,
                                     text=f"{o}  ({p:.3f})",
                                     variable=self._selected_outcome,
                                     value=o,
                                     text_color=color,
                                     fg_color=color,
                                     hover_color=color,
                                     font=CTkFont(size=12, weight="bold"))
            rb.grid(row=0, column=i, padx=6, pady=4, sticky="w")

            # Price labels in prices section
            key = f"{market.get('slug','?')}|{o}"
            price_history.add(key, p)

        # Rebuild price labels
        for w in self._prices_frame.winfo_children():
            w.destroy()

        for o, p in zip(outcomes, prices):
            color = GREEN if o.lower() in ("yes","up","higher") else RED
            row = ctk.CTkFrame(self._prices_frame, fg_color="transparent")
            row.pack(fill="x", pady=2)
            ctk.CTkLabel(row, text=o, width=60, anchor="w",
                          font=CTkFont(size=11, weight="bold"), text_color=color
                          ).pack(side="left")
            lbl = ctk.CTkLabel(row, text=f"${p:.4f}", anchor="e",
                                font=CTkFont(size=11), text_color=TEXT_MAIN)
            lbl.pack(side="right")
            key = f"{market.get('slug','?')}|{o}"
            self._price_labels[key] = lbl

        self._redraw_chart()
        self._set_status(f"✅ Loaded: {len(outcomes)} outcomes", GREEN)
        self._update_balance_display()

    def _do_buy(self):
        if not self._current_markets:
            self._set_status("Load a market first.", RED)
            return
        outcome = self._selected_outcome.get()
        amount_str = self._amount_entry.get().replace("$","").strip()
        try:
            amount = float(amount_str)
            assert amount > 0
        except Exception:
            self._set_status("Enter a valid USD amount.", RED)
            return

        market = self._current_markets[0]
        outcomes = self._current_outcomes
        prices   = self._current_prices

        idx = next((i for i,o in enumerate(outcomes) if o == outcome), None)
        if idx is None:
            self._set_status("Select an outcome first.", RED)
            return

        price = prices[idx]
        if price <= 0 or price >= 1:
            self._set_status(f"Invalid price: {price}", RED)
            return

        balance = db.get_paper_balance(DESKTOP_USER_ID)
        if balance < amount:
            self._set_status(f"Insufficient balance (${balance:.2f})", RED)
            return

        shares = amount / price
        db.update_paper_balance(DESKTOP_USER_ID, balance - amount)

        slug  = market.get("slug","unknown")
        title = market.get("question","Unknown")
        existing = db.get_paper_position(DESKTOP_USER_ID, slug, outcome)
        if existing:
            total_cost = (existing["shares"] * existing["avg_price"]) + amount
            new_shares = existing["shares"] + shares
            new_avg    = total_cost / new_shares
        else:
            new_shares, new_avg = shares, price

        db.upsert_paper_position(DESKTOP_USER_ID, slug, title, outcome, new_shares, new_avg)

        self._set_status(
            f"✅ Bought {shares:.2f} shares of {outcome}\n@ ${price:.4f}  |  Cost ${amount:.2f}", GREEN)
        self._update_balance_display()
        self._refresh_portfolio()

    def _do_sell(self):
        if not self._current_markets:
            self._set_status("Load a market first.", RED)
            return
        outcome = self._selected_outcome.get()
        amount_str = self._amount_entry.get().replace("$","").strip()

        market = self._current_markets[0]
        outcomes = self._current_outcomes
        prices   = self._current_prices

        idx = next((i for i,o in enumerate(outcomes) if o == outcome), None)
        if idx is None:
            self._set_status("Select an outcome first.", RED)
            return

        price = prices[idx]
        slug  = market.get("slug","unknown")
        pos   = db.get_paper_position(DESKTOP_USER_ID, slug, outcome)

        if not pos or pos["shares"] <= 0:
            self._set_status(f"You have no {outcome} position.", RED)
            return

        # Sell USD worth or all shares
        try:
            sell_usd = float(amount_str)
            shares_to_sell = min(sell_usd / price, pos["shares"])
        except Exception:
            shares_to_sell = pos["shares"]  # sell all if blank

        if shares_to_sell <= 0:
            self._set_status("Nothing to sell.", RED)
            return

        proceeds = shares_to_sell * price
        pnl      = (price - pos["avg_price"]) * shares_to_sell
        balance  = db.get_paper_balance(DESKTOP_USER_ID)
        db.update_paper_balance(DESKTOP_USER_ID, balance + proceeds)

        remaining = pos["shares"] - shares_to_sell
        if remaining < 0.0001:
            db.remove_paper_position(pos["id"])
            db.add_trade_history(DESKTOP_USER_ID, slug, outcome,
                                  pos["avg_price"], price, shares_to_sell, pnl)
        else:
            db.upsert_paper_position(DESKTOP_USER_ID, slug, pos["market_title"],
                                      outcome, remaining, pos["avg_price"])

        pnl_str = f"+${pnl:.2f}" if pnl >= 0 else f"-${abs(pnl):.2f}"
        color   = GREEN if pnl >= 0 else RED
        self._set_status(
            f"✅ Sold {shares_to_sell:.2f} shares @ ${price:.4f}\n"
            f"Proceeds ${proceeds:.2f}  |  PnL {pnl_str}", color)
        self._update_balance_display()
        self._refresh_portfolio()
        self._load_history()

    def _do_search(self):
        q = self._search_entry.get().strip()
        if not q:
            return
        self._search_status.configure(text="Searching…", text_color=TEXT_DIM)
        for w in self._search_scroll.winfo_children():
            w.destroy()
        threading.Thread(target=self._search_thread, args=(q,), daemon=True).start()

    def _search_thread(self, q: str):
        results = search_markets(q)
        self.after(0, lambda: self._show_search_results(results))

    def _show_search_results(self, results: list[dict]):
        for w in self._search_scroll.winfo_children():
            w.destroy()

        if not results:
            self._search_status.configure(text="No active markets found.", text_color=RED)
            return

        self._search_status.configure(text=f"Found {len(results)} markets:", text_color=TEXT_DIM)

        for m in results:
            slug  = m.get("slug","")
            title = m.get("question", "Unknown")
            try:
                prices   = json.loads(m.get("outcomePrices","[]"))
                outcomes = json.loads(m.get("outcomes","[]"))
                price_str = "  ".join(f"{o}: {float(p):.3f}" for o,p in zip(outcomes,prices))
            except Exception:
                price_str = ""

            card = ctk.CTkFrame(self._search_scroll, fg_color=BG_PANEL, corner_radius=8,
                                 border_color=BORDER, border_width=1)
            card.pack(fill="x", pady=4)

            ctk.CTkLabel(card, text=title, wraplength=520, justify="left",
                          font=CTkFont(size=11, weight="bold"), text_color=TEXT_MAIN,
                          anchor="w").pack(fill="x", padx=12, pady=(8,2))
            ctk.CTkLabel(card, text=price_str,
                          font=CTkFont(size=10), text_color=TEXT_DIM, anchor="w"
                          ).pack(fill="x", padx=12, pady=(0,4))

            url = f"https://polymarket.com/event/{slug}"
            def _use(u=url):
                self._url_entry.delete(0,"end")
                self._url_entry.insert(0, u)
                self._tabs.set("📈  Trade")
                self._load_market()

            ctk.CTkButton(card, text="Use this market →", height=28, width=140,
                           corner_radius=6, fg_color=ACCENT, hover_color="#4a9aff",
                           font=CTkFont(size=11), command=_use
                           ).pack(anchor="e", padx=12, pady=(0,8))

    # ─────────────────────────────────────────────────────────────────────────
    #  Portfolio refresh
    # ─────────────────────────────────────────────────────────────────────────

    def _refresh_portfolio(self):
        threading.Thread(target=self._portfolio_thread, daemon=True).start()

    def _portfolio_thread(self):
        positions = db.get_all_paper_positions(DESKTOP_USER_ID)
        balance   = db.get_paper_balance(DESKTOP_USER_ID)
        enriched  = []

        for p in positions:
            curr_price = p["avg_price"]  # default to avg if live fetch fails
            try:
                slug = p["market_slug"]
                event = fetch_market_by_slug(slug)
                if event:
                    markets = event.get("markets",[])
                    if markets:
                        market = markets[0]
                        outs    = json.loads(market.get("outcomes","[]"))
                        prices  = [float(x) for x in json.loads(market.get("outcomePrices","[]"))]
                        idx = next((i for i,o in enumerate(outs) if o==p["outcome"]), None)
                        if idx is not None:
                            curr_price = prices[idx]
            except Exception:
                pass
            enriched.append(dict(p) | {"curr_price": curr_price})

        self.after(0, lambda: self._render_portfolio(enriched, balance))

    def _render_portfolio(self, positions: list[dict], balance: float):
        for w in self._port_scroll.winfo_children():
            w.destroy()

        total_pos_value = 0.0
        total_pnl       = 0.0

        if not positions:
            ctk.CTkLabel(self._port_scroll, text="No open positions.",
                          text_color=TEXT_DIM, font=CTkFont(size=12)
                          ).pack(pady=24)
        else:
            for p in positions:
                curr  = p["curr_price"]
                curr_val   = curr * p["shares"]
                cost_basis = p["avg_price"] * p["shares"]
                pnl        = curr_val - cost_basis
                pnl_pct    = (pnl / cost_basis * 100) if cost_basis > 0 else 0
                total_pos_value += curr_val
                total_pnl       += pnl

                c_pnl = GREEN if pnl >= 0 else RED
                card = ctk.CTkFrame(self._port_scroll, fg_color=BG_PANEL, corner_radius=8,
                                     border_color=BORDER, border_width=1)
                card.pack(fill="x", pady=4)

                row1 = ctk.CTkFrame(card, fg_color="transparent")
                row1.pack(fill="x", padx=12, pady=(8,2))
                ctk.CTkLabel(row1, text=p["market_title"][:55]+"…", anchor="w",
                              font=CTkFont(size=11, weight="bold"), text_color=TEXT_MAIN
                              ).pack(side="left")
                pnl_str = f"+${pnl:.2f} ({pnl_pct:+.1f}%)" if pnl>=0 else f"-${abs(pnl):.2f} ({pnl_pct:+.1f}%)"
                ctk.CTkLabel(row1, text=pnl_str, anchor="e",
                              font=CTkFont(size=11, weight="bold"), text_color=c_pnl
                              ).pack(side="right")

                row2 = ctk.CTkFrame(card, fg_color="transparent")
                row2.pack(fill="x", padx=12, pady=(0,8))
                ctk.CTkLabel(row2, text=f"{p['outcome']}  |  {p['shares']:.2f} shares  |  avg ${p['avg_price']:.4f}  →  ${curr:.4f}",
                              anchor="w", font=CTkFont(size=10), text_color=TEXT_DIM
                              ).pack(side="left")
                ctk.CTkLabel(row2, text=f"${curr_val:.2f}", anchor="e",
                              font=CTkFont(size=11), text_color=TEXT_MAIN
                              ).pack(side="right")

        total = balance + total_pos_value
        self._port_cash_lbl.configure(text=f"${balance:.2f}")
        self._port_value_lbl.configure(text=f"${total_pos_value:.2f}")
        self._port_total_lbl.configure(text=f"${total:.2f}")
        c = GREEN if total_pnl >= 0 else RED
        pnl_str = f"+${total_pnl:.2f}" if total_pnl >= 0 else f"-${abs(total_pnl):.2f}"
        self._port_pnl_lbl.configure(text=pnl_str, text_color=c)

    # ─────────────────────────────────────────────────────────────────────────
    #  History
    # ─────────────────────────────────────────────────────────────────────────

    def _load_history(self):
        for w in self._hist_scroll.winfo_children():
            w.destroy()

        trades = db.get_trade_history(DESKTOP_USER_ID)

        wins   = sum(1 for t in trades if t["pnl"] >= 0)
        losses = len(trades) - wins
        total_pnl = sum(t["pnl"] for t in trades)

        self._hist_trades_lbl.configure(text=str(len(trades)))
        self._hist_wins_lbl.configure(text=str(wins))
        self._hist_losses_lbl.configure(text=str(losses))
        c = GREEN if total_pnl >= 0 else RED
        self._hist_total_pnl.configure(
            text=f"+${total_pnl:.2f}" if total_pnl>=0 else f"-${abs(total_pnl):.2f}",
            text_color=c)

        if not trades:
            ctk.CTkLabel(self._hist_scroll, text="No closed trades yet.",
                          text_color=TEXT_DIM, font=CTkFont(size=12)).pack(pady=24)
            return

        # Header
        hdr = ctk.CTkFrame(self._hist_scroll, fg_color=BG_PANEL, corner_radius=6)
        hdr.pack(fill="x", pady=(0,4))

        for col, w in [("Market/Outcome",240),("Bought",70),("Sold",70),("Shares",60),("PnL",80),("Date",110)]:
            ctk.CTkLabel(hdr, text=col, width=w, anchor="w",
                          font=CTkFont(size=10, weight="bold"), text_color=TEXT_DIM
                          ).pack(side="left", padx=8, pady=6)

        for t in reversed(list(trades)):
            pnl = t["pnl"]
            color = GREEN if pnl >= 0 else RED
            row = ctk.CTkFrame(self._hist_scroll, fg_color=BG_PANEL, corner_radius=6,
                                border_color=BORDER, border_width=1)
            row.pack(fill="x", pady=2)

            info = f"{t['market_slug'][:22]}… | {t['outcome']}"
            for val, w in [
                (info,              240),
                (f"${t['buy_price']:.4f}",  70),
                (f"${t['sell_price']:.4f}", 70),
                (f"{t['shares']:.2f}",      60),
                (f"+${pnl:.2f}" if pnl>=0 else f"-${abs(pnl):.2f}", 80),
                (t["closed_at"][:10],       110),
            ]:
                tc = color if val.startswith(("+","-")) and "$" in val else TEXT_MAIN
                ctk.CTkLabel(row, text=val, width=w, anchor="w",
                              font=CTkFont(size=10), text_color=tc
                              ).pack(side="left", padx=8, pady=5)

    # ─────────────────────────────────────────────────────────────────────────
    #  Chart
    # ─────────────────────────────────────────────────────────────────────────

    def _style_axis(self, ax):
        ax.tick_params(colors=TEXT_DIM, labelsize=8)
        ax.spines["bottom"].set_color(BORDER)
        ax.spines["left"].set_color(BORDER)
        ax.spines["top"].set_visible(False)
        ax.spines["right"].set_visible(False)
        ax.yaxis.label.set_color(TEXT_DIM)
        ax.xaxis.label.set_color(TEXT_DIM)
        ax.set_ylabel("Price (USD)", color=TEXT_DIM, fontsize=8)

    def _draw_empty_chart(self):
        self._ax.clear()
        self._ax.set_facecolor(CHART_BG)
        self._style_axis(self._ax)
        self._ax.text(0.5, 0.5, "Load a market to see price history",
                       transform=self._ax.transAxes,
                       ha="center", va="center",
                       color=TEXT_DIM, fontsize=10)
        self._ax.set_xticks([])
        self._ax.set_yticks([])
        self._fig.tight_layout(pad=1.5)
        self._canvas.draw()

    def _redraw_chart(self):
        self._ax.clear()
        self._ax.set_facecolor(CHART_BG)
        self._style_axis(self._ax)

        if not self._current_markets:
            self._draw_empty_chart()
            return

        market   = self._current_markets[0]
        outcomes = self._current_outcomes
        prices   = self._current_prices
        slug     = market.get("slug","?")

        colors = [GREEN, RED, ACCENT, GOLD, "#a371f7"]

        for i, (o, p) in enumerate(zip(outcomes, prices)):
            key  = f"{slug}|{o}"
            hist = price_history.get(key)
            c    = colors[i % len(colors)]

            if len(hist) >= 2:
                ts  = [x[0] for x in hist]
                ps  = [x[1] for x in hist]
                t0  = ts[0]
                xs  = [(t - t0) for t in ts]
                self._ax.plot(xs, ps, color=c, linewidth=1.8, label=o)
                self._ax.fill_between(xs, ps, min(ps)*0.98,
                                       color=c, alpha=0.1)
            else:
                # Single point — draw horizontal dashed line
                self._ax.axhline(y=p, color=c, linewidth=1.2,
                                  linestyle="--", label=f"{o} ({p:.3f})", alpha=0.8)

        self._ax.legend(fontsize=8, frameon=False,
                         labelcolor="white", loc="upper left")
        self._ax.set_xticks([])
        self._fig.tight_layout(pad=1.5)
        self._canvas.draw()

    # ─────────────────────────────────────────────────────────────────────────
    #  Auto-refresh live prices
    # ─────────────────────────────────────────────────────────────────────────

    def _start_auto_refresh(self):
        self._stop_refresh.clear()
        self._refresh_thread = threading.Thread(target=self._refresh_loop, daemon=True)
        self._refresh_thread.start()

    def _refresh_loop(self):
        while not self._stop_refresh.is_set():
            time.sleep(15)  # refresh every 15 seconds
            if self._current_markets:
                self._do_price_refresh()

    def _do_price_refresh(self):
        if not self._current_market:
            return
        slug = self._current_markets[0].get("slug","")
        if not slug:
            return
        event = fetch_market_by_slug(slug)
        if not event:
            return
        markets = event.get("markets",[])
        if not markets:
            return
        market = markets[0]
        try:
            outcomes = json.loads(market.get("outcomes","[]"))
            prices   = [float(p) for p in json.loads(market.get("outcomePrices","[]"))]
        except Exception:
            return

        self._current_outcomes = outcomes
        self._current_prices   = prices

        for o, p in zip(outcomes, prices):
            key = f"{slug}|{o}"
            price_history.add(key, p)

        self.after(0, self._update_price_labels)
        self.after(0, self._redraw_chart)
        self.after(0, self._update_outcome_buttons)

    def _update_price_labels(self):
        if not self._current_markets:
            return
        slug = self._current_markets[0].get("slug","?")
        for o, p in zip(self._current_outcomes, self._current_prices):
            key = f"{slug}|{o}"
            lbl = self._price_labels.get(key)
            if lbl:
                lbl.configure(text=f"${p:.4f}")

    def _update_outcome_buttons(self):
        for w in self._outcome_frame.winfo_children():
            if isinstance(w, ctk.CTkRadioButton):
                outcome = w.cget("value")
                idx = next((i for i,o in enumerate(self._current_outcomes) if o==outcome), None)
                if idx is not None:
                    p = self._current_prices[idx]
                    color = GREEN if outcome.lower() in ("yes","up","higher") else RED
                    w.configure(text=f"{outcome}  ({p:.3f})")

    # ─────────────────────────────────────────────────────────────────────────
    #  Helpers
    # ─────────────────────────────────────────────────────────────────────────

    def _set_status(self, msg: str, color: str = GREEN):
        self._status_lbl.configure(text=msg, text_color=color)

    def _update_balance_display(self):
        bal = db.get_paper_balance(DESKTOP_USER_ID)
        self._balance_lbl.configure(text=f"💵 ${bal:,.2f}")

    def _on_close(self):
        self._stop_refresh.set()
        self.destroy()


# ─────────────────────────────────────────────────────────────────────────────
#  Widget helpers
# ─────────────────────────────────────────────────────────────────────────────

def _section_label(parent, text: str) -> ctk.CTkLabel:
    lbl = ctk.CTkLabel(parent, text=text,
                        font=CTkFont(family="Segoe UI", size=9, weight="bold"),
                        text_color="#58a6ff",
                        anchor="w")
    lbl.pack(fill="x", padx=12, pady=(10,2))
    return lbl

def _stat_label(parent, title: str, initial: str, color: str):
    """Create a stat box inside summary rows."""
    frame = ctk.CTkFrame(parent, fg_color="transparent")
    frame.pack(side="left", fill="x", expand=True, padx=8, pady=8)

    ctk.CTkLabel(frame, text=title,
                  font=CTkFont(size=9), text_color=TEXT_DIM, anchor="w"
                  ).pack(anchor="w")
    val_lbl = ctk.CTkLabel(frame, text=initial,
                             font=CTkFont(size=13, weight="bold"),
                             text_color=color, anchor="w")
    val_lbl.pack(anchor="w")
    return val_lbl


# ─────────────────────────────────────────────────────────────────────────────
#  Entry point
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    app = PolyTradeApp()
    app.mainloop()

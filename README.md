<div align="center">

# ⬡ PolyTrade

### Polymarket Paper Trading Suite — Telegram Bot · AI HFT Agent · Desktop App

[![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB?style=for-the-badge&logo=python&logoColor=white)](https://python.org)
[![python-telegram-bot](https://img.shields.io/badge/python--telegram--bot-21.x-29A8E0?style=for-the-badge&logo=telegram&logoColor=white)](https://python-telegram-bot.org)
[![Polymarket](https://img.shields.io/badge/Polymarket-Live%20Data-9D4EDD?style=for-the-badge)](https://polymarket.com)
[![Platform](https://img.shields.io/badge/Platform-Windows%20%7C%20Linux-lightgrey?style=for-the-badge&logo=linux)](https://github.com)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow?style=for-the-badge)](LICENSE)

> **A professional Polymarket toolkit** — monitor whale wallets via Telegram, run a fully autonomous HFT-style AI paper-trading agent, and use a sleek desktop GUI for manual paper trading — all in one repo.

</div>

---

## 📦 What's Inside

| Component | File | Description |
|-----------|------|-------------|
| 🤖 **Telegram Bot** | `bot.py` | Real-time Polymarket wallet monitoring & trade alerts |
| 🧠 **AI Agent** | `agent.py` | Autonomous BTC paper-trading agent (HFT-style, 1s poll) |
| 🖥️ **Desktop App** | `desktop_app.py` | Dark-theme GUI — live charts, buy/sell, portfolio |
| 📡 **API Client** | `api.py` | Async Polymarket CLOB + Gamma API wrapper |
| 🗄️ **Database** | `db.py` | SQLite persistence (wallets, trades, positions, balances) |
| 📈 **Paper CLI** | `paper_cli.py` | Command-line paper trading tool |
| 🔍 **Scanner** | `scanner.py` | Live Polymarket market scanner |

---

## 🏗️ Architecture Overview

```
┌─────────────────────────────────────────────────────────────────┐
│                        PolyTrade Suite                          │
├─────────────────┬───────────────────┬───────────────────────────┤
│  Telegram Bot   │    AI Agent        │      Desktop App          │
│   (bot.py)      │  (agent.py)        │   (desktop_app.py)        │
│                 │                   │                           │
│ • Wallet alerts │ • Binance WS feed  │ • Market search           │
│ • Paper buy/sell│ • BTC momentum     │ • Live price charts       │
│ • Portfolio view│ • RSI + Kelly      │ • Manual buy/sell         │
│ • Trade history │ • Auto TP/SL/TSL   │ • Portfolio & PnL         │
└────────┬────────┴────────┬──────────┴──────────┬────────────────┘
         │                 │                      │
         └─────────────────┴──────────────────────┘
                           │
              ┌────────────▼────────────┐
              │     Shared Layer        │
              │  api.py · db.py         │
              │  handlers.py · scanner.py│
              │  SQLite (polytrack.db)  │
              └─────────────────────────┘
```

---

## 🧠 AI Agent — How It Works

The agent (`agent.py`) is a fully autonomous BTC paper-trading agent that operates on Polymarket's 5-minute BTC price-direction markets.

### Signal Generation
1. **Binance WebSocket** streams real-time BTC price (sub-second updates)
2. **Dual-timeframe momentum**: 1-min and 3-min momentum must **agree** in direction
3. **RSI Filter** (10-period): avoids entries at extreme overbought/oversold levels
4. **Kelly-fraction position sizing**: risks up to 10% of portfolio on high-edge setups

### Exit Rules (Scalping)
| Rule | Trigger |
|------|---------|
| 🎯 **Take Profit** | +8% ROI |
| 🛑 **Stop Loss** | −5% ROI |
| 📉 **Trailing Stop Loss** | Activates at +4% ROI, trails by 2% |
| ⌛ **Theta Exit** | Lock profit or cut losses when window is nearly closed |
| 🔄 **Reversal Exit** | Momentum flips hard against open position |
| ⚠️ **Hard Timeout** | Force-abandon position held > 11 minutes |

### Risk Management
- **Consecutive loss circuit breaker**: 5-minute cooldown after 5 losses in a row
- **Daily max loss limit**: 20% of starting balance
- **Per-window trade lock**: never re-enters the same market slug in a session
- **20s post-sell cooldown**: prevents overtrading
- **Orphan recovery**: restores open positions on restart

---

## 🖥️ Desktop App — PolyTrade Mini

A dark-theme floating desktop app for manual Polymarket paper trading.

**Features:**
- 🔍 Search live Polymarket markets by keyword
- 📊 Live price chart (auto-refreshes every 15 seconds)
- 💸 Buy / Sell any outcome with virtual USD
- 📁 Portfolio view with live unrealized PnL
- 📜 Trade history with win/loss stats
- 💵 Starts with **$1,000 virtual balance**

### Launch

```powershell
# Windows — PowerShell or CMD
python desktop_app.py
# or double-click:
launch_desktop.bat
```

```bash
# Linux (requires display — not suitable for headless VPS)
python3 desktop_app.py
```

> ⚠️ **Linux:** Tkinter must be installed separately via your system package manager. See [Linux setup](#-quick-start--linux) below.

---

## 🤖 Telegram Bot Commands

| Command | Description |
|---------|-------------|
| `/start` | Welcome screen + main menu |
| `/add_wallet` | Add a Polymarket wallet to monitor |
| `/my_wallets` | List all tracked wallets |
| `/remove_wallet` | Remove a wallet |
| `/paper_buy` | Paper trade: buy an outcome |
| `/paper_sell` | Paper trade: sell a position |
| `/portfolio` | View your open positions & unrealized PnL |
| `/history` | View closed trade history |
| `/help` | Full help text |

### Alert Format

```
🔔 New Polymarket Trade!

👤 Wallet: Whale #1
    0xAbCd…1234

💰 BUY YES "Will BTC close above $100k?"
    1,200 shares @ $0.650

💵 Value: ~$780.00
📅 2026-04-10 14:00 UTC

🔗 View activity
```

---

## 🚀 Quick Start — Windows

### 1 · Install Python 3.10+

Download from [python.org](https://www.python.org/downloads/).
**✅ Check "Add Python to PATH"** during installation.

### 2 · Clone the repo

```powershell
git clone https://github.com/your-username/polytrade.git
cd polytrade
```

### 3 · Create a virtual environment

```powershell
python -m venv venv
.\venv\Scripts\Activate.ps1    # PowerShell
# OR
venv\Scripts\activate           # CMD
```

### 4 · Install dependencies

```powershell
pip install -r requirements.txt
```

### 5 · Configure `.env`

```powershell
copy .env.example .env
```

Open `.env` and fill in your values (see [Environment Variables](#️-environment-variables) below).

### 6 · Run

```powershell
# Telegram Bot
python bot.py

# AI Auto-Trading Agent
python agent.py

# Desktop App
python desktop_app.py

# Paper Trading CLI
python paper_cli.py --help
```

---

## 🐧 Quick Start — Linux

### 1 · Install Python 3.10+ and Tkinter

**Ubuntu / Debian:**
```bash
sudo apt update
sudo apt install python3 python3-pip python3-venv python3-tk -y
```

**Fedora / RHEL:**
```bash
sudo dnf install python3 python3-pip python3-tkinter -y
```

**Arch Linux:**
```bash
sudo pacman -S python python-pip tk
```

> ⚠️ `python3-tk` (Tkinter) is **required** for the Desktop App. It is **not** included in the pip package — install it via your system package manager.

### 2 · Clone, set up, and run

```bash
git clone https://github.com/your-username/polytrade.git
cd polytrade
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
nano .env   # fill in your values

# Telegram Bot
python3 bot.py

# AI Agent
python3 agent.py

# Desktop App (requires a display — local use only)
python3 desktop_app.py
```

---

## ☁️ VPS Deployment (Telegram Bot + AI Agent)

> The Desktop App requires a display. Run `bot.py` and/or `agent.py` on a headless VPS.

### Setup

```bash
git clone https://github.com/your-username/polytrade.git
cd polytrade
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
nano .env          # add BOT_TOKEN and other vars
python3 bot.py     # test it works first
```

### Create a systemd service

```bash
sudo nano /etc/systemd/system/polytrade.service
```

```ini
[Unit]
Description=PolyTrade Telegram Bot
After=network.target

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/home/ubuntu/polytrade
ExecStart=/home/ubuntu/polytrade/venv/bin/python3 bot.py
Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable polytrade
sudo systemctl start polytrade
sudo journalctl -fu polytrade   # live logs
```

> To also run the AI agent as a service, create a second `.service` file pointing to `agent.py`.

---

## ⚙️ Environment Variables

Copy `.env.example` to `.env` and configure:

| Variable | Default | Required | Description |
|----------|---------|----------|-------------|
| `BOT_TOKEN` | — | **Yes** | Telegram bot token from [@BotFather](https://t.me/BotFather) |
| `POLL_INTERVAL` | `10` | No | Seconds between Polymarket wallet checks |
| `DB_PATH` | `polytrack.db` | No | SQLite database file path |
| `LOG_LEVEL` | `INFO` | No | `DEBUG` / `INFO` / `WARNING` / `ERROR` |
| `AGENT_USER_ID` | `999999` | No | Virtual user ID for the AI agent's paper trades |
| `AGENT_CHAT_ID` | `0` | No | Telegram chat ID for agent notifications (auto-detected if `0`) |
| `AGENT_TRADE_USD` | `25` | No | Base USD amount per agent trade (Kelly sizing applies on top) |
| `AGENT_MIN_EDGE` | `0.02` | No | Minimum signal edge (2%) required before entering a trade |
| `AGENT_POLL_SECONDS` | `1` | No | Agent evaluation loop interval in seconds |

---

## 📁 Project Structure

```
polytrade/
├── bot.py              # Telegram bot entry point
├── agent.py            # AI auto-trading agent (HFT-style)
├── handlers.py         # All Telegram handlers & conversation flows
├── api.py              # Async Polymarket CLOB + Gamma API client
├── db.py               # SQLite schema & CRUD helpers
├── scanner.py          # Live market scanner
├── paper_cli.py        # Command-line paper trading tool
├── desktop_app.py      # 🖥️ PolyTrade mini desktop GUI app
├── chart.py            # Chart rendering utilities
├── profit_bot.py       # Profit tracking helpers
├── launch_desktop.bat  # Windows one-click launcher for desktop app
├── requirements.txt    # All Python dependencies
├── .env.example        # Template — copy to .env and fill in values
├── .gitignore          # Keeps .env & venv out of git
└── README.md           # You are here
```

---

## 🔧 Utility — Reset Balances

### Reset the AI Agent balance (user ID `999999`)

```python
# Reset exactly to $1,000
python -c "import db; db.update_paper_balance(999999, 1000.0); print('Agent balance reset to $1000.00')"

# Add $500 to current balance
python -c "import db; b = db.get_paper_balance(999999); db.update_paper_balance(999999, b + 500.0); print(f'New balance: \${db.get_paper_balance(999999):.2f}')"
```

### Reset the Desktop App balance (user ID `9999`)

```python
python -c "import db; db.update_paper_balance(9999, 1000.0); print('Desktop balance reset to $1000.00')"
```

---

## 🛡️ Privacy & Security

- Only **public** Polymarket wallet addresses are monitored — no private keys ever
- The bot **never** asks for seed phrases, passwords, or any sensitive data
- All data is stored **locally** in your SQLite database (`polytrack.db`)
- `.env` is excluded from git via `.gitignore`
- This is a **paper trading** tool — no real money is ever transacted

---

## 🔮 Coming Soon

- [ ] Multi-market agent support (beyond BTC 5-min)
- [ ] Web dashboard for portfolio analytics
- [ ] Wallet copy-trading mode
- [ ] Backtesting framework for agent strategies
- [ ] Push notifications via webhooks

---

## 📜 License

MIT — free to use, modify, and distribute.

> ⚠️ **Disclaimer:** PolyTrade is a paper trading tool for educational and research purposes only. Nothing here constitutes financial advice. Always do your own research (DYOR).

# ⬡ PolyTrade — Polymarket Paper Trading Suite

[![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-blue.svg)](https://python.org)
[![python-telegram-bot](https://img.shields.io/badge/python--telegram--bot-21.x-brightgreen.svg)](https://python-telegram-bot.org)
[![Platform](https://img.shields.io/badge/Platform-Windows%20%7C%20Linux-lightgrey.svg)](#)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

> A professional Polymarket toolkit — a Telegram bot for real-time wallet alerts, an AI auto-trading agent, and a **mini desktop app** for paper trading with live charts and buy/sell — all in one repo.

---

## ✨ What's Inside

| Component | File | Description |
|-----------|------|-------------|
| 🤖 **Telegram Bot** | `bot.py` | Real-time wallet monitoring & alerts |
| 🧠 **AI Agent** | `agent.py` | Auto paper-trading agent (HFT-style) |
| 🖥️ **Desktop App** | `desktop_app.py` | Mini GUI — live charts, buy/sell, portfolio |
| 📡 **API Client** | `api.py` | Async Polymarket API wrapper |
| 🗄️ **Database** | `db.py` | SQLite persistence layer |
| 📈 **Paper CLI** | `paper_cli.py` | Command-line paper trading |

---

## 🖥️ Desktop App — PolyTrade Mini

A dark-theme floating desktop app for Polymarket paper trading.

**Features:**
- 🔍 Search live Polymarket markets by keyword
- 📊 Live price chart (auto-refreshes every 15 seconds)
- 💸 Buy / Sell any outcome with virtual USD
- 📁 Portfolio view with live unrealized PnL
- 📜 Trade history with win/loss stats
- 💵 Starts with **$1,000 virtual balance**

### Run the Desktop App

**Windows:**
```powershell
python desktop_app.py
# or double-click:
launch_desktop.bat
```

**Linux:**
```bash
python3 desktop_app.py
```

> **Linux users:** Tkinter must be installed separately — see [Linux setup](#-quick-start-linux) below.

---

## 🤖 Telegram Bot Commands

| Command | Description |
|---------|-------------|
| `/start` | Welcome screen + main menu |
| `/add_wallet` | Add a wallet to monitor |
| `/my_wallets` | List all your tracked wallets |
| `/remove_wallet` | Remove a wallet |
| `/paper_buy` | Paper trade: buy an outcome |
| `/paper_sell` | Paper trade: sell a position |
| `/portfolio` | View your paper trading positions |
| `/history` | View closed trade history |
| `/help` | Full help text |

---

## 🚀 Quick Start — Windows

### 1 — Install Python 3.10+

Download from [python.org](https://www.python.org/downloads/).  
**✅ Check "Add Python to PATH"** during installation.

### 2 — Clone the repo

```powershell
git clone https://github.com/your-username/polytrade.git
cd polytrade
```

### 3 — Create a virtual environment

```powershell
python -m venv venv
.\venv\Scripts\Activate.ps1    # PowerShell
# OR
venv\Scripts\activate           # CMD
```

### 4 — Install dependencies

```powershell
pip install -r requirements.txt
```

### 5 — Configure `.env`

```powershell
copy .env.example .env
```

Open `.env` and set your bot token:

```env
BOT_TOKEN=123456789:AAF...xyz
```

### 6 — Run

```powershell
# Telegram Bot
python bot.py

# Desktop App
python desktop_app.py

# Paper Trading CLI
python paper_cli.py --help
```

---

## 🐧 Quick Start — Linux

### 1 — Install Python 3.10+ and Tkinter

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

> ⚠️ `python3-tk` (Tkinter) is **required** for the Desktop App. It is NOT included in the Python pip package — it must be installed via your system package manager.

### 2 — Clone the repo

```bash
git clone https://github.com/your-username/polytrade.git
cd polytrade
```

### 3 — Create a virtual environment

```bash
python3 -m venv venv
source venv/bin/activate
```

### 4 — Install dependencies

```bash
pip install -r requirements.txt
```

### 5 — Configure `.env`

```bash
cp .env.example .env
nano .env   # or use any text editor
```

```env
BOT_TOKEN=123456789:AAF...xyz
```

### 6 — Run

```bash
# Telegram Bot
python3 bot.py

# Desktop App (requires a display — use locally, not headless VPS)
python3 desktop_app.py

# Paper Trading CLI
python3 paper_cli.py --help
```

---

## ☁️ VPS Deploy (Linux — Telegram Bot only)

> The Desktop App requires a display. Run `bot.py` or `agent.py` on a headless VPS.

### Setup

```bash
git clone https://github.com/your-username/polytrade.git
cd polytrade
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
nano .env   # add BOT_TOKEN
python3 bot.py   # test it works first
```

### Create a systemd service

```bash
sudo nano /etc/systemd/system/polytrade.service
```

Paste:

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

Enable and start:

```bash
sudo systemctl daemon-reload
sudo systemctl enable polytrade
sudo systemctl start polytrade
sudo journalctl -fu polytrade   # live logs
```

---

## 📁 Project Structure

```
polytrade/
├── bot.py              # Telegram bot entry point
├── agent.py            # AI auto-trading agent
├── handlers.py         # All Telegram handlers & conversation flows
├── api.py              # Async Polymarket API client (aiohttp)
├── db.py               # SQLite schema & CRUD helpers
├── scanner.py          # Market scanner
├── paper_cli.py        # Command-line paper trading tool
├── desktop_app.py      # 🖥️ PolyTrade mini desktop GUI app
├── launch_desktop.bat  # Windows double-click launcher for desktop app
├── requirements.txt    # All dependencies (Windows + Linux)
├── .env.example        # Template — copy to .env
├── .gitignore          # Keeps .env & venv out of git
└── README.md           # You are here
```

---

## ⚙️ Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `BOT_TOKEN` | *(required)* | Telegram bot token from @BotFather |
| `POLL_INTERVAL` | `45` | Seconds between Polymarket checks |
| `DB_PATH` | `polytrack.db` | SQLite database file path |
| `LOG_LEVEL` | `INFO` | `DEBUG` / `INFO` / `WARNING` / `ERROR` |

---

## 🔧 Utility — Reset Agent / Desktop Balance

Reset the **AI Agent** virtual balance (user ID `999999`):

```bash
# Add $1,000 to current balance
python3 -c "import db; b = db.get_paper_balance(999999); db.update_paper_balance(999999, b + 1000.0); print(f'New Balance: \${db.get_paper_balance(999999):.2f}')"

# Reset exactly to $1,000
python3 -c "import db; db.update_paper_balance(999999, 1000.0); print('Reset to \$1000.00')"
```

Reset the **Desktop App** virtual balance (user ID `9999`):

```bash
# Reset desktop app balance to $1,000
python3 -c "import db; db.update_paper_balance(9999, 1000.0); print('Desktop balance reset to \$1000.00')"
```

---

## 🔔 Alert Format (Telegram)

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

## 🛡️ Privacy & Security

- Only **public** Polymarket wallet addresses are used
- The bot **never** asks for private keys, seed phrases, or passwords
- All data is stored locally in your SQLite database (`polytrack.db`)
- `.env` is excluded from git via `.gitignore`
- Paper trading only — no real money is ever used

---

## 📜 License

MIT — free to use, modify, and distribute.

> ⚠️ **Disclaimer:** This is a paper trading tool for educational purposes. Not financial advice. DYOR.

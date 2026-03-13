# 🔔 PolyTrack — Polymarket Trade Monitor Bot

[![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-blue.svg)](https://python.org)
[![python-telegram-bot](https://img.shields.io/badge/python--telegram--bot-21.x-brightgreen.svg)](https://python-telegram-bot.org)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

> A professional, async Telegram bot that monitors public Polymarket wallet addresses and sends you real-time trade alerts — completely free, read-only, no private keys ever.

---

## ✨ Features

| Feature | Description |
|---------|-------------|
| 📡 Real-time alerts | Checks Polymarket every 45 seconds per wallet |
| 🎯 Smart filters | Filter by minimum USD value and/or BUYs-only |
| 👛 Multi-wallet | Track up to 10 wallets per user |
| 🏷️ Nicknames | Friendly names for each wallet |
| 💾 Persistent | SQLite — survives bot restarts |
| 🎨 Premium UX | Inline keyboards, emoji, clean Markdown |
| 🔒 Privacy-first | Only public on-chain data, no keys ever |

---

## 🤖 Bot Commands

| Command | Description |
|---------|-------------|
| `/start` | Welcome screen + main menu |
| `/add_wallet` | Guided 4-step wizard to add a wallet |
| `/my_wallets` | List all your tracked wallets |
| `/remove_wallet` | Remove a wallet with one tap |
| `/help` | Full help text |

---

## 🚀 Quick Start (Windows)

### 1 — Install Python

Download Python 3.10+ from [python.org](https://www.python.org/downloads/).  
**✅ Check "Add Python to PATH"** during installation.

### 2 — Clone / download this repo

```powershell
git clone https://github.com/your-username/polytrack.git
cd polytrack
```

### 3 — Create & activate virtual environment

```powershell
python -m venv venv
.\venv\Scripts\Activate.ps1   # PowerShell
# OR
venv\Scripts\activate          # CMD
```

### 4 — Install dependencies

```powershell
pip install -r requirements.txt
```

### 5 — Get your Bot Token

1. Open Telegram → search for **@BotFather**
2. Send `/newbot` and follow the prompts
3. BotFather will give you a token like `123456789:AAF...xyz`

### 6 — Configure `.env`

```powershell
copy .env.example .env
```

Open `.env` in any editor and paste your token:

```env
BOT_TOKEN=123456789:AAF...xyz
```

### 7 — Run!

```powershell
python bot.py
```

Open Telegram, find your bot, and send `/start` 🎉

---

## 🖥️ VPS Deploy (Linux)

### Copy files to server

```bash
scp -r . user@your-server-ip:/home/user/polytrack/
# OR
git clone https://github.com/your-username/polytrack.git
```

### Setup on server

```bash
cd polytrack
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
nano .env          # add BOT_TOKEN=...
python bot.py      # test it works
```

### Create systemd service

```bash
sudo nano /etc/systemd/system/polytrack.service
```

Paste:

```ini
[Unit]
Description=PolyTrack Telegram Bot
After=network.target

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/home/ubuntu/polytrack
ExecStart=/home/ubuntu/polytrack/venv/bin/python bot.py
Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

Enable & start:

```bash
sudo systemctl daemon-reload
sudo systemctl enable polytrack
sudo systemctl start polytrack
sudo journalctl -fu polytrack   # live logs
```

---

## 📁 Project Structure

```
polytrack/
├── bot.py           # Entry point — ApplicationBuilder, JobQueue
├── handlers.py      # All Telegram handlers & conversation flows
├── api.py           # Async Polymarket API client (aiohttp)
├── db.py            # SQLite schema & CRUD helpers
├── requirements.txt # Pinned dependencies
├── .env.example     # Template — copy to .env
├── .gitignore       # Keeps .env & venv out of git
└── README.md        # You are here
```

---

## 🔔 Alert Format

```
🔔 New Polymarket Trade!

👤 Wallet: Whale #1
    0xAbCd…1234

💰 BUY YES "Will X win the election?" 
    1,200 shares @ $0.650

💵 Value: ~$780.00
📅 2026-02-24 15:45 UTC

🔗 View activity
```

---

## ⚙️ Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `BOT_TOKEN` | *(required)* | Telegram bot token from @BotFather |
| `POLL_INTERVAL` | `45` | Seconds between Polymarket checks |
| `DB_PATH` | `polytrack.db` | SQLite database file path |
| `LOG_LEVEL` | `INFO` | DEBUG / INFO / WARNING / ERROR |

---

## 🛡️ Privacy & Security

- Only **public** Polymarket wallet addresses are used
- The bot **never** asks for private keys, seed phrases, or passwords
- All data is stored locally in your SQLite database
- `.env` is excluded from git via `.gitignore`

---

some more features are coming soon

## 📜 License

MIT — free to use, modify, and distribute.

this was in devlopment not accurate

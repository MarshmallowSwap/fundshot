# FUNDING KING BOT — Bybit Perpetual Funding Rate Monitor

Bot Telegram per monitorare i funding rate delle coppie **Perpetual USDT** su Bybit e generare alert contrarian automatici.

## Funzionalità

### Alert automatici (ogni 60s)
| Livello | Soglia | Direzione |
|---|---|---|
| 🔴 HARD | \|rate\| ≥ 2.00% | SHORT / LONG |
| 🔥 EXTREME | \|rate\| ≥ 1.50% | SHORT / LONG |
| 🚨 HIGH | \|rate\| ≥ 1.00% | SHORT / LONG |
| ℹ️ CHIUSURA | \|rate\| ≥ 0.23% | Consiglio chiusura |
| ✅ RIENTRO | \|rate\| ≤ 0.75% | Normalizzazione |

### Comandi disponibili
- `/start` — Setup guidato e menu principale
- `/help` — Lista completa comandi
- `/status` — Stato connessioni e credenziali (mascherate)
- `/test` — Test manuale connessione Bybit (3 endpoint)
- `/funding_top` — Top 10 funding positivi (SHORT)
- `/funding_bottom` — Top 10 funding negativi (LONG)
- `/saldo` — Saldo wallet Unified (equity, margine, PnL)
- `/posizioni` — Posizioni aperte con PnL $ e %

## Setup

### 1. Clona la repository
```bash
git clone https://github.com/MarshmallowSwap/funding-king-bot.git
cd funding-king-bot
```

### 2. Crea l'ambiente virtuale
```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 3. Configura il `.env`
```bash
cp .env.example .env
```
Modifica `.env` e inserisci **solo** il token Telegram:
```
TELEGRAM_TOKEN=il_tuo_token_da_botfather
```
> Chat ID, API Key e API Secret si configurano via `/start` direttamente su Telegram.

### 4. Avvia il bot
```bash
python3 bot.py
```

### 5. Configura via Telegram
1. Apri il bot su Telegram
2. Invia `/start`
3. Segui il wizard per inserire API Key e API Secret Bybit

## Struttura file
```
funding-king-bot/
├── bot.py           # Entry point, job queue, handlers
├── bybit_client.py  # Client Bybit v5 con HMAC signing corretto
├── alert_logic.py   # Soglie, stati e logica anti-spam
├── commands.py      # Tutti i comandi Telegram + setup wizard
├── requirements.txt
├── .env.example     # Template configurazione
├── .gitignore       # .env escluso dal tracking
└── README.md
```

## Sicurezza
- Il file `.env` è escluso da git tramite `.gitignore`
- Le API key sono mascherate in tutti i messaggi Telegram
- I messaggi contenenti credenziali vengono cancellati automaticamente
- Il wizard è accessibile solo dall'owner del bot

## Requisiti
- Python 3.11+
- Account Bybit con API Key (permessi: Read)
- Bot Telegram creato via @BotFather

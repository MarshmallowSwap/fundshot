# FundShot — Funding Rate Intelligence

> Auto-trading bot for extreme funding rates on Bybit, Binance & Hyperliquid.  
> 🌐 [fundshot.app](https://fundshot.app) · 🤖 [@FundShot_bot](https://t.me/FundShot_bot)

---

## Table of Contents

1. [What is FundShot](#what-is-fundshot)
2. [How It Works](#how-it-works)
3. [Alert Levels](#alert-levels)
4. [60-Day Live Test Results](#60-day-live-test-results)
5. [Auto-Trader Configuration](#auto-trader-configuration)
6. [Guardian Risk Engine](#guardian-risk-engine)
7. [Money Management MM](#money-management-mm)
8. [Supported Exchanges](#supported-exchanges)
9. [Plans and Pricing](#plans-and-pricing)
10. [Architecture](#architecture)
11. [Self-Hosting Setup](#self-hosting-setup)
12. [Security](#security)

---

## What is FundShot

FundShot monitors **500+ perpetual pairs** across Bybit, Binance, and Hyperliquid 24/7 and sends Telegram alerts the moment funding rates hit extreme levels.

On Pro and Elite plans, the bot opens and manages positions automatically:
- Enters on extreme funding (contrarian strategy)
- Shorts collect positive funding, longs collect negative funding
- Manages exits via TP1 partial close + native trailing stop
- Monitors positions every 2 minutes with in-house fallback

---

## How It Works

**Signal flow:**

1. Funding rate polled every 60 seconds across all monitored pairs
2. Level classified: SOFT / HIGH / EXTREME / HARD / JACKPOT
3. Telegram alert sent to users with that exchange configured
4. Auto-Trader checks persistence filter + OI confirmation
5. Position opened if filters pass
6. Monitor loop: TP1 → SL to breakeven → trailing stop → close

**Direction logic:**
- Positive funding → SHORT (longs pay shorts → short to collect)
- Negative funding → LONG (shorts pay longs → long to collect)

---

## Alert Levels

| Level | Threshold | Description |
|-------|-----------|-------------|
| 📊 SOFT | ≥ 0.50% | Entry-level signal |
| 🚨 HIGH | ≥ 1.00% | Significant imbalance |
| 🔥 EXTREME | ≥ 1.50% | Strong opportunity |
| 🔴 HARD | ≥ 2.00% | Very rare, high conviction |
| 💎 JACKPOT | ≥ 2.50% | Maximum opportunity |

Each alert includes: funding rate, direction signal, OI Δ5m, mark price, 24h change, next funding countdown.

**Public Telegram channel** (@FundShot_Public): receives HARD + JACKPOT alerts only (~1-3/day) with auto-trader trade notification and upgrade CTA.

---

## 60-Day Live Test Results

Live trading conducted on **Bybit Demo** — real market conditions, paper capital.

### Test Configuration

| Parameter | Value |
|-----------|-------|
| Exchange | Bybit (Demo mode) |
| Duration | 60 days |
| Starting capital | $10,000 USDT |
| Size per trade | 500 USDT (5% of capital) |
| Leverage | 10x → $5,000 notional per position |
| Min entry level | SOFT+ (funding ≥ 0.5%) |
| Symbols monitored | Top 200 Bybit USDT perpetuals |
| Guardian | Active |
| Money Management | Active — fixed size |
| OI filter | ≥ 1.5% change in 5 minutes |
| Persistence filter | 2 consecutive cycles required |
| Cooldown after close | 30 minutes per symbol |

### Entry Strategy

| Rule | Value |
|------|-------|
| TP1 (partial close) | 30% of position at +0.70% |
| TP1 action | SL moved to breakeven |
| Trailing stop buffer | 0.70% from peak |
| Trailing activation | From TP1 price |
| Stop loss | 5.0% from entry (before TP1) |
| Stop loss after TP1 | Breakeven |
| Max cap | Force-close at +3.0% total |
| Funding exit | Close if funding retreats below threshold |
| Fees included | Taker 0.055% + slippage 0.02% = ~0.15% round-trip |

### What the Numbers Represent

Every trade was executed in real market conditions on Bybit Demo — real order books, real slippage, real funding payments. The only difference from live trading is that capital was virtual. The strategy, timing, and execution logic are identical to what runs on live accounts today.

Live results always visible at: fundshot.app/#track-record (updated daily at 03:00 UTC)

---

## Auto-Trader Configuration

Settings are per-exchange, configured from the dashboard and synced to the bot via `/api/config`. Saved in `/tmp/fs_config_{exchange}.json` on the VPS.

| Parameter | Description | Default |
|-----------|-------------|---------|
| `size` | USDT per trade | 100 |
| `leva` | Leverage multiplier | 10 |
| `sl` | Stop loss % from entry | 5.0 |
| `maxpos` | Max concurrent open positions | 5 |
| `persist` | Number of cycles required before entry | 2 |
| `oi` | Minimum OI Δ5m % to confirm entry | 1.5 |
| `cooldown` | Minutes cooldown per symbol after close | 0 |
| `tp1pct` | TP1 target % | 0.70 |
| `trailing` | Trailing stop buffer % | 0.70 |
| `maxcap` | Max total % gain before force-close | 3.0 |

**Position reload on restart:** On every bot startup, open positions are automatically reloaded from the exchange API. This prevents orphaned positions from going unmonitored after a restart.

---

## Guardian Risk Engine

Guardian is a circuit breaker that automatically pauses the auto-trader when risk thresholds are breached.

### Circuit Breakers

| Rule | Default threshold | Action when triggered |
|------|-------------------|----------------------|
| Max session drawdown | 10% of session capital | Pause all trading |
| Consecutive losses | 3 in a row | 30-minute cooldown |
| Daily loss limit | $100 USDT | Pause until midnight UTC |

### Behavior

- Evaluated every monitor cycle (every 2 minutes)
- Sends Telegram alert when any breaker trips
- Auto-resets: daily loss resets at midnight, cooldown after its duration
- Manual reset available via dashboard toggle
- Settings persist across restarts

---

## Money Management MM

| Setting | Description |
|---------|-------------|
| Fixed size | Same USDT amount per trade regardless of balance |
| Max positions | Hard cap on concurrent open positions |
| Per-exchange isolation | MM settings independent per exchange |
| No compounding (default) | Fixed size, not % of current equity |

MM and Guardian configuration is stored independently per exchange and survives bot restarts.

---

## Supported Exchanges

| Exchange | Status | Auto-Trader | Alerts | Demo available |
|----------|--------|-------------|--------|----------------|
| 🟡 Bybit | Live | Yes | All levels | Yes |
| 🟠 Binance | Live | Yes | All levels | Yes (Testnet) |
| 🟣 Hyperliquid | Live | Alerts only | All levels | No |
| 🔵 OKX | EU Restricted | No | No | — |
| dYdX / Bitget / Gate.io | Coming soon | — | — | — |

**Hyperliquid note:** Connect your ETH wallet address (public, read-only) in Settings to monitor balance and positions. No private key required or stored.

**OKX note:** Geo-blocked from EU VPS (Helsinki). Coming via non-EU relay server.

**Binance note:** Native SL/TP orders not supported on testnet (error -4120). On mainnet they work normally. In testnet mode, positions are protected by the bot's in-house monitor loop.

---

## Plans and Pricing

| Plan | Monthly | One-shot 30d | Lifetime |
|------|---------|-------------|---------|
| Free | $0 | — | — |
| Pro | $20/mo | $25 | — |
| Elite | $45/mo | $55 | $300 |

**Free:** 10 alerts/day · top 10 funding pairs · live dashboard (read-only)

**Pro:** Unlimited alerts · 1 exchange · auto-trading (up to 5 pos/day) · Guardian · backtesting · pre-settlement alerts

**Elite:** Everything in Pro + multi-exchange (Bybit + Binance + Hyperliquid) · unlimited positions · custom thresholds · referral 10% commission

Payments accepted: USDT, BTC, ETH, SOL, BNB, TON via NOWPayments.

**Referral program:** 10% of every payment from referred users (lifetime, including renewals). Payout in USDT TRC20 monthly via NOWPayments Mass Payout when balance ≥ $5.

---

## Architecture

```
fundshot/
├── bot.py                    # Main process — funding_job, trading_job, monitor_positions
├── trader.py                 # FundingTrader class, BinanceFuturesTrader, TradePosition
├── alert_logic.py            # Alert state machine, level classification, message formatting
├── proxy_v6.py               # HTTP proxy — all REST API endpoints (/api/*)
├── commands.py               # Telegram command handlers
├── oi_monitor.py             # Open Interest spike detection
├── user_registry.py          # Multi-user registry, per-exchange client management
├── referral.py               # Referral system + monthly NOWPayments payouts
├── generate_track_record.py  # 60-day performance report generator (cron daily)
├── backtester.py             # Backtesting engine
│
├── exchanges/
│   ├── __init__.py           # Exchange factory (make_client)
│   ├── bybit.py              # Bybit v5 client
│   ├── binance.py            # Binance Futures client
│   ├── hyperliquid.py        # Hyperliquid (alerts + wallet read-only)
│   ├── okx.py                # OKX client (EU geo-blocked)
│   └── models.py             # FundingTicker, Position, WalletBalance dataclasses
│
├── db/
│   ├── supabase_client.py    # Users, credentials, trades (Supabase PostgreSQL)
│   ├── schema.sql            # Database schema
│   └── crypto.py             # AES-256-GCM encryption for API keys
│
├── index.html                # Dashboard SPA (served at /dashboard via Vercel)
├── landing.html              # Landing page (fundshot.app)
├── admin.html                # Admin panel (/admin)
├── sw.js                     # PWA service worker
├── manifest.json             # PWA manifest
│
└── vercel.json               # Vercel static deployment configuration
```

### Infrastructure

| Component | Service | Details |
|-----------|---------|---------|
| Bot + Proxy | Hetzner VPS | Ubuntu 24, Helsinki — 95.217.10.201 |
| Frontend | Vercel | fundshot.app |
| Database | Supabase | PostgreSQL + Row Level Security |
| Payments | NOWPayments | Crypto payments + Mass Payout |
| Domain | IONOS | fundshot.app |

### REST API endpoints (proxy_v6.py on port 8080, nginx proxy)

| Endpoint | Auth | Description |
|----------|------|-------------|
| `GET /api/status` | No | Bot health, last cycle, alerts sent |
| `GET /api/funding` | No | Live funding rates (Bybit, cached 60s) |
| `GET /api/track-record` | No | 60-day performance report |
| `GET /api/alert-history` | JWT | Last 50 alerts sent |
| `GET /api/user/wallet` | JWT | Exchange wallet balance |
| `GET /api/user/positions` | JWT | Open positions per exchange |
| `GET /api/user/exchanges` | JWT | Configured exchanges list |
| `POST /api/user/keys` | JWT | Save / update exchange API keys |
| `DELETE /api/user/keys/{exchange}` | JWT | Remove exchange credentials |
| `POST /api/config` | JWT | Push trading config to bot (immediate) |
| `POST /api/auto-trading` | JWT | Enable / disable auto-trader |
| `GET /api/closed-pnl` | JWT | Closed trade history from Supabase |
| `POST /api/auth/telegram` | No | Telegram OAuth login |

---

## Self-Hosting Setup

### Requirements

- Python 3.11+
- Supabase project
- Telegram bot token (via @BotFather)
- VPS with public IP (nginx recommended)

### 1. Clone and install

```bash
git clone https://github.com/MarshmallowSwap/fundshot.git
cd fundshot
pip install -r requirements.txt
```

### 2. Configure environment

```bash
cp .env.example .env
```

Required variables:

```env
TELEGRAM_TOKEN=your_bot_token
CHAT_ID=your_telegram_chat_id
CHANNEL_ID=@your_public_channel

SUPABASE_URL=https://xxx.supabase.co
SUPABASE_KEY=your_service_role_key
ENCRYPT_KEY=base64_encoded_32_bytes

NOWPAY_API_KEY=your_nowpayments_api_key

AUTO_TRADING=false
```

### 3. Create database schema

Run `db/schema.sql` in your Supabase SQL editor.

### 4. Start services

```bash
sudo systemctl start fundshot        # bot
sudo systemctl start fundshot-proxy  # HTTP proxy
```

### 5. Generate track record

```bash
python3 generate_track_record.py
# Output: /tmp/fs_track_record.json (~3-5 min for 200 symbols × 60 days)
```

Daily cron (3:00 UTC):
```bash
0 3 * * * cd /root/fundshot && python3 generate_track_record.py
```

---

## Security

- API keys encrypted with **AES-256-GCM** before Supabase storage
- Each user has fully isolated credentials (row-level encryption)
- Bot never logs API keys in plain text
- JWT tokens for dashboard authentication (Telegram OAuth)
- Hyperliquid: read-only wallet address — no private key ever stored
- `.env` excluded from git via `.gitignore`

---

*FundShot © 2026 — [support@fundshot.app](mailto:support@fundshot.app)*

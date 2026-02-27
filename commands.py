import os
import logging
import time
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, ConversationHandler

from alert_logic import (
    THRESHOLD_HARD, THRESHOLD_EXTREME, THRESHOLD_HIGH,
    THRESHOLD_CLOSE_TIP, THRESHOLD_RIENTRO,
    get_active_alerts,
)

# ─── STATI CONVERSATION HANDLER ──────────────────────────────────
MENU, WAIT_API_KEY, WAIT_API_SECRET = range(3)


# ─── HELPERS ─────────────────────────────────────────────────────

def _mask(value: str | None) -> str:
    """Maschera una credenziale mostrando inizio e fine."""
    if not value or len(value) < 6:
        return "❌ Non impostata"
    return f"✅ {value[:4]}***{value[-3:]}"


def _env_status() -> dict:
    """Ritorna lo stato attuale delle credenziali dal .env."""
    return {
        "token": os.getenv("TELEGRAM_TOKEN"),
        "chat_id": os.getenv("CHAT_ID"),
        "api_key": os.getenv("BYBIT_API_KEY"),
        "api_secret": os.getenv("BYBIT_API_SECRET"),
    }


def _save_env(key: str, value: str):
    """Aggiorna o aggiunge una variabile nel file .env."""
    env_path = ".env"
    lines = []
    found = False

    if os.path.exists(env_path):
        with open(env_path, "r") as f:
            lines = f.readlines()

    new_lines = []
    for line in lines:
        if line.startswith(f"{key}="):
            new_lines.append(f"{key}={value}\n")
            found = True
        else:
            new_lines.append(line)

    if not found:
        new_lines.append(f"{key}={value}\n")

    with open(env_path, "w") as f:
        f.writelines(new_lines)

    # Aggiorna anche in memoria per effetto immediato
    os.environ[key] = value


def _setup_keyboard(chat_id_detected: str | None = None) -> InlineKeyboardMarkup:
    """Keyboard inline per il menu di setup."""
    env = _env_status()
    api_key_label = "✅ API Key" if env["api_key"] else "🔓 Imposta API Key"
    api_secret_label = "✅ API Secret" if env["api_secret"] else "🔒 Imposta API Secret"
    chat_id_label = f"✅ Chat ID ({chat_id_detected})" if chat_id_detected else "📱 Chat ID"

    keyboard = [
        [InlineKeyboardButton(chat_id_label, callback_data="set_chatid")],
        [InlineKeyboardButton(api_key_label, callback_data="set_apikey")],
        [InlineKeyboardButton(api_secret_label, callback_data="set_apisecret")],
        [InlineKeyboardButton("✅ Verifica configurazione", callback_data="verify")],
    ]
    return InlineKeyboardMarkup(keyboard)


# ─── /start ──────────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    env = _env_status()
    chat_id_detected = str(update.effective_chat.id)

    # Auto-salva Chat ID se non ancora configurato
    if not env["chat_id"]:
        _save_env("CHAT_ID", chat_id_detected)
        env["chat_id"] = chat_id_detected

    configured = bool(env["api_key"] and env["api_secret"])

    if configured:
        # ── Bot già configurato → menu comandi ───────────────────
        msg = (
            "⚡ *FUNDING KING BOT* — Attivo ✅\n\n"
            "Monitoraggio funding Bybit in esecuzione.\n"
            "Usa /help per vedere tutti i comandi disponibili.\n\n"
            f"📱 Chat ID: `{env['chat_id']}`\n"
            f"🔓 API Key: `{_mask(env['api_key'])}`\n"
            f"🔒 API Secret: `{_mask(env['api_secret'])}`"
        )
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("💰 /saldo", callback_data="cmd_saldo"),
                InlineKeyboardButton("📋 /posizioni", callback_data="cmd_posizioni"),
            ],
            [
                InlineKeyboardButton("📊 /funding_top", callback_data="cmd_top"),
                InlineKeyboardButton("📉 /funding_bottom", callback_data="cmd_bottom"),
            ],
            [InlineKeyboardButton("🔄 Aggiorna credenziali", callback_data="setup")],
        ])
        await update.message.reply_text(msg, parse_mode="Markdown", reply_markup=keyboard)
    else:
        # ── Prima configurazione → wizard ─────────────────────────
        msg = (
            "👋 *Benvenuto in Funding King Bot!*\n\n"
            "Il bot non è ancora configurato.\n"
            "Usa i pulsanti qui sotto per completare il setup.\n\n"
            f"🤖 Token Telegram:  ✅ Attivo\n"
            f"📱 Chat ID:         ✅ `{chat_id_detected}` _(rilevato auto)_\n"
            f"🔓 API Key Bybit:   {'✅ Configurata' if env['api_key'] else '❌ Non impostata'}\n"
            f"🔒 API Secret:      {'✅ Configurata' if env['api_secret'] else '❌ Non impostata'}"
        )
        await update.message.reply_text(
            msg,
            parse_mode="Markdown",
            reply_markup=_setup_keyboard(chat_id_detected),
        )
    return MENU


async def setup_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Mostra il menu di setup via callback."""
    query = update.callback_query
    await query.answer()

    env = _env_status()
    chat_id = str(update.effective_chat.id)

    msg = (
        "⚙️ *SETUP CREDENZIALI*\n\n"
        f"🤖 Token Telegram:  ✅ Attivo\n"
        f"📱 Chat ID:         ✅ `{chat_id}`\n"
        f"🔓 API Key:         {_mask(env['api_key'])}\n"
        f"🔒 API Secret:      {_mask(env['api_secret'])}"
    )
    await query.edit_message_text(
        msg,
        parse_mode="Markdown",
        reply_markup=_setup_keyboard(chat_id),
    )
    return MENU


# ─── WIZARD: imposta API KEY ─────────────────────────────────────

async def ask_api_key(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        "🔓 *Imposta API Key Bybit*\n\n"
        "Invia la tua API Key pubblica Bybit (mainnet).\n\n"
        "⚠️ Il messaggio verrà cancellato subito dopo per sicurezza.",
        parse_mode="Markdown",
    )
    return WAIT_API_KEY


async def save_api_key(update: Update, context: ContextTypes.DEFAULT_TYPE):
    value = update.message.text.strip()
    # Cancella il messaggio dell'utente (sicurezza)
    try:
        await update.message.delete()
    except Exception:
        pass

    _save_env("BYBIT_API_KEY", value)
    env = _env_status()
    chat_id = str(update.effective_chat.id)

    await update.message.reply_text(
        f"✅ *API Key salvata!*\n`{_mask(value)}`",
        parse_mode="Markdown",
        reply_markup=_setup_keyboard(chat_id),
    )
    return MENU


# ─── WIZARD: imposta API SECRET ──────────────────────────────────

async def ask_api_secret(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        "🔒 *Imposta API Secret Bybit*\n\n"
        "Invia la tua API Secret Bybit (mainnet).\n\n"
        "⚠️ Il messaggio verrà cancellato subito dopo per sicurezza.",
        parse_mode="Markdown",
    )
    return WAIT_API_SECRET


async def save_api_secret(update: Update, context: ContextTypes.DEFAULT_TYPE):
    value = update.message.text.strip()
    try:
        await update.message.delete()
    except Exception:
        pass

    _save_env("BYBIT_API_SECRET", value)
    chat_id = str(update.effective_chat.id)

    await update.message.reply_text(
        f"✅ *API Secret salvata!*\n`{_mask(value)}`",
        parse_mode="Markdown",
        reply_markup=_setup_keyboard(chat_id),
    )
    return MENU


# ─── WIZARD: verifica configurazione ─────────────────────────────

async def verify_config(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    env = _env_status()
    ok = bool(env["api_key"] and env["api_secret"])

    if ok:
        msg = (
            "✅ *CONFIGURAZIONE COMPLETATA*\n\n"
            f"🤖 Token Telegram:  ✅ Attivo\n"
            f"📱 Chat ID:         ✅ `{env['chat_id']}`\n"
            f"🔓 API Key:         {_mask(env['api_key'])}\n"
            f"🔒 API Secret:      {_mask(env['api_secret'])}\n\n"
            "Il bot è pronto. Usa /help per i comandi.\n"
            "Usa /test per verificare la connessione Bybit."
        )
    else:
        missing = []
        if not env["api_key"]:
            missing.append("API Key")
        if not env["api_secret"]:
            missing.append("API Secret")
        msg = (
            f"⚠️ *Configurazione incompleta*\n\n"
            f"Mancano: {', '.join(missing)}\n\n"
            "Usa i pulsanti per completare il setup."
        )

    await query.edit_message_text(
        msg,
        parse_mode="Markdown",
        reply_markup=_setup_keyboard(env.get("chat_id")),
    )
    return MENU


# ─── INLINE BUTTON: avvia comandi da /start ──────────────────────

async def inline_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Gestisce i pulsanti del menu principale dopo configurazione."""
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "cmd_saldo":
        await query.message.reply_text("Usa il comando /saldo")
    elif data == "cmd_posizioni":
        await query.message.reply_text("Usa il comando /posizioni")
    elif data == "cmd_top":
        await query.message.reply_text("Usa il comando /funding_top")
    elif data == "cmd_bottom":
        await query.message.reply_text("Usa il comando /funding_bottom")
    return MENU


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❌ Operazione annullata.")
    return ConversationHandler.END


# ─── /help ───────────────────────────────────────────────────────

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = (
        "🤖 *FUNDING KING BOT — Comandi disponibili*\n\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        "📊 *FUNDING RATE*\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        "/funding\\_top\n"
        "  → Top 10 simboli funding più alto _(SHORT)_\n\n"
        "/funding\\_bottom\n"
        "  → Top 10 simboli funding più basso _(LONG)_\n\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        "💼 *ACCOUNT*\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        "/saldo\n"
        "  → Equity, wallet balance, margine, PnL aperto\n\n"
        "/posizioni\n"
        "  → Posizioni aperte con PnL $ e %, leva, liquidazione\n\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        "⚙️ *BOT*\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        "/start  → Avvia il bot e menu configurazione\n"
        "/status → Stato connessioni e credenziali\n"
        "/test   → Testa la connessione a Bybit\n"
        "/help   → Mostra questo messaggio\n\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        "🔔 *ALERT AUTOMATICI*\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        "Il bot monitora il funding ogni *60s* e invia alert:\n\n"
        f"🔴 HARD      → |rate| ≥ {THRESHOLD_HARD}%\n"
        f"🔥 EXTREME   → |rate| ≥ {THRESHOLD_EXTREME}%\n"
        f"🚨 HIGH      → |rate| ≥ {THRESHOLD_HIGH}%\n"
        f"ℹ️ CHIUSURA → |rate| ≥ {THRESHOLD_CLOSE_TIP}%\n"
        f"✅ RIENTRO   → |rate| ≤ {THRESHOLD_RIENTRO}%\n\n"
        "Ogni alert include:\n"
        "• Simbolo e rate attuale\n"
        "• Intervallo funding _(1H/2H/4H/8H)_\n"
        "• Direzione contrarian _(LONG/SHORT)_\n"
        "━━━━━━━━━━━━━━━━━━━━━"
    )
    await update.message.reply_text(msg, parse_mode="Markdown")


# ─── /status ─────────────────────────────────────────────────────

async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    env = _env_status()
    bybit_client = context.application.bot_data.get("bybit_client")
    bot_data = context.application.bot_data

    # Controlla connessioni
    tg_ok = "✅ Connesso"
    bybit_pub = "⏳ Non testata"
    bybit_auth = "⏳ Non testata"

    if bybit_client:
        pub_result = await bybit_client.ping_public()
        bybit_pub = f"✅ OK  ({pub_result.get('latency')}ms)" if pub_result.get("ok") else f"❌ Errore"
        if env["api_key"] and env["api_secret"]:
            auth_result = await bybit_client.ping_auth()
            bybit_auth = f"✅ Autenticata  ({auth_result.get('latency')}ms)" if auth_result.get("ok") else f"❌ {auth_result.get('error', 'Errore')}"
        else:
            bybit_auth = "⚠️ API keys non configurate"

    # Uptime
    start_ts = bot_data.get("start_time", time.time())
    uptime_sec = int(time.time() - start_ts)
    h, m = divmod(uptime_sec // 60, 60)
    uptime_str = f"{h}h {m}m"

    # Ultimo ciclo
    last_fetch = bot_data.get("last_fetch_time")
    last_fetch_str = time.strftime("%H:%M:%S", time.localtime(last_fetch)) if last_fetch else "—"
    alert_count = bot_data.get("alert_count_session", 0)
    symbol_count = bot_data.get("symbol_count", 0)

    # Simboli in alert attivo
    active = get_active_alerts()
    if active:
        active_str = "  ".join([f"`{s}` {l.upper()}" for s, l in list(active.items())[:5]])
    else:
        active_str = "Nessuno"

    # Credenziali
    token_val = env.get("token")
    token_str = f"✅ {token_val[:8]}***{token_val[-4:]}" if token_val and len(token_val) > 12 else "✅ Configurato"

    msg = (
        "⚙️ *STATUS BOT — Funding King*\n\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        "🔌 *CONNESSIONI*\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        f"Telegram Bot:    {tg_ok}\n"
        f"Bybit API Pub:   {bybit_pub}\n"
        f"Bybit API Auth:  {bybit_auth}\n\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        "🔑 *CREDENZIALI*\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        f"Token Telegram:  {token_str}\n"
        f"Chat ID:         {_mask(env.get('chat_id'))}\n"
        f"API Key:         {_mask(env.get('api_key'))}\n"
        f"API Secret:      {_mask(env.get('api_secret'))}\n\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        "🤖 *BOT*\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        f"Stato:           ✅ Attivo\n"
        f"Monitoraggio:    ✅ In esecuzione\n"
        f"Intervallo job:  60s\n"
        f"Simboli monit.:  {symbol_count}\n"
        f"Uptime:          {uptime_str}\n\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        "📊 *ULTIMO CICLO*\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        f"Ultimo fetch:    {last_fetch_str}\n"
        f"Alert inviati:   {alert_count}\n"
        f"Simboli in alert: {active_str}\n"
        "━━━━━━━━━━━━━━━━━━━━━"
    )

    await update.message.reply_text(msg, parse_mode="Markdown")


# ─── /test ───────────────────────────────────────────────────────

async def test_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    bybit_client = context.application.bot_data.get("bybit_client")
    if not bybit_client:
        await update.message.reply_text("❌ Client Bybit non inizializzato.")
        return

    # Messaggio temporaneo
    msg = await update.message.reply_text("🧪 *TEST CONNESSIONE BYBIT — in corso...*", parse_mode="Markdown")

    import time as t
    total_start = t.time()
    failed = 0

    # ── Test 1: API pubblica ──────────────────────────────────────
    pub = await bybit_client.ping_public()
    if pub.get("ok"):
        test1 = (
            f"1️⃣  *API PUBBLICA*\n"
            f"Endpoint:   `/v5/market/tickers`\n"
            f"Stato:      ✅ OK\n"
            f"Latenza:    {pub['latency']}ms\n"
            f"Simboli:    {pub['count']} ricevuti\n"
            f"Esempio:    `{pub.get('example', '—')}`"
        )
    else:
        failed += 1
        test1 = (
            f"1️⃣  *API PUBBLICA*\n"
            f"Stato:      ❌ FALLITO\n"
            f"Errore:     {pub.get('error', 'Timeout')}\n"
            f"Latenza:    {pub['latency']}ms"
        )

    # ── Test 2: API autenticata ───────────────────────────────────
    env = _env_status()
    if env["api_key"] and env["api_secret"]:
        auth = await bybit_client.ping_auth()
        if auth.get("ok"):
            test2 = (
                f"2️⃣  *API AUTENTICATA*\n"
                f"Endpoint:   `/v5/account/wallet-balance`\n"
                f"Stato:      ✅ OK\n"
                f"Latenza:    {auth['latency']}ms\n"
                f"retCode:    0\n"
                f"USDT:       {auth.get('usdt', 0):,.2f}"
            )
        else:
            failed += 1
            test2 = (
                f"2️⃣  *API AUTENTICATA*\n"
                f"Stato:      ❌ FALLITO\n"
                f"Errore:     {auth.get('error', 'Errore')}\n"
                f"retCode:    {auth.get('retCode', '—')}\n"
                f"Latenza:    {auth['latency']}ms"
            )
    else:
        test2 = "2️⃣  *API AUTENTICATA*\n⚠️ API keys non configurate — usa /start"
        failed += 1

    # ── Test 3: API posizioni ─────────────────────────────────────
    if failed == 0 or (env["api_key"] and failed < 2):
        pos = await bybit_client.ping_positions()
        if pos.get("ok"):
            test3 = (
                f"3️⃣  *API POSIZIONI*\n"
                f"Endpoint:   `/v5/position/list`\n"
                f"Stato:      ✅ OK\n"
                f"Latenza:    {pos['latency']}ms\n"
                f"Posizioni:  {pos['count']} aperte"
            )
        else:
            failed += 1
            test3 = (
                f"3️⃣  *API POSIZIONI*\n"
                f"Stato:      ❌ FALLITO\n"
                f"Errore:     {pos.get('error', 'Errore')}\n"
                f"Latenza:    {pos['latency']}ms"
            )
    else:
        test3 = "3️⃣  *API POSIZIONI*\n⏭️ Saltato _(dipende da auth)_"

    total_ms = int((t.time() - total_start) * 1000)
    summary = "✅  Tutti i test superati" if failed == 0 else f"⚠️  {failed} test fallito/i\n💡 Usa /start per aggiornare le credenziali"

    result = (
        "🧪 *TEST CONNESSIONE BYBIT*\n\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        f"{test1}\n\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        f"{test2}\n\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        f"{test3}\n\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        f"⏱️  Tempo totale:  {total_ms}ms\n"
        f"{summary}\n"
        "━━━━━━━━━━━━━━━━━━━━━"
    )

    await msg.edit_text(result, parse_mode="Markdown")


# ─── /funding_top ────────────────────────────────────────────────

async def funding_top(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = context.application.bot_data.get("funding_data", [])
    if not data:
        await update.message.reply_text("⏳ Dati funding non ancora disponibili. Riprova tra qualche secondo.")
        return

    sorted_data = sorted(data, key=lambda x: x["rate"], reverse=True)[:10]
    lines = ["📊 *TOP 10 FUNDING POSITIVI* _(SHORT)_\n"]
    for i, d in enumerate(sorted_data, 1):
        rate = d["rate"]
        interval = d["interval"]
        level = _level_emoji(rate)
        lines.append(f"{i}. `{d['symbol']}` {level}\n   `{rate:+.4f}%`  _(ogni {interval}H)_  ⚡ SHORT")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def funding_bottom(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = context.application.bot_data.get("funding_data", [])
    if not data:
        await update.message.reply_text("⏳ Dati funding non ancora disponibili. Riprova tra qualche secondo.")
        return

    sorted_data = sorted(data, key=lambda x: x["rate"])[:10]
    lines = ["📉 *TOP 10 FUNDING NEGATIVI* _(LONG)_\n"]
    for i, d in enumerate(sorted_data, 1):
        rate = d["rate"]
        interval = d["interval"]
        level = _level_emoji(rate)
        lines.append(f"{i}. `{d['symbol']}` {level}\n   `{rate:+.4f}%`  _(ogni {interval}H)_  ⚡ LONG")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


def _level_emoji(rate: float) -> str:
    abs_rate = abs(rate)
    if abs_rate >= THRESHOLD_HARD:
        return "🔴"
    if abs_rate >= THRESHOLD_EXTREME:
        return "🔥"
    if abs_rate >= THRESHOLD_HIGH:
        return "🚨"
    if abs_rate >= THRESHOLD_CLOSE_TIP:
        return "ℹ️"
    return ""


# ─── /saldo ──────────────────────────────────────────────────────

async def saldo_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    bybit_client = context.application.bot_data.get("bybit_client")
    if not bybit_client or not bybit_client.has_keys:
        await update.message.reply_text(
            "⚠️ API Bybit non configurate.\nUsa /start per il setup."
        )
        return

    msg = await update.message.reply_text("💰 Recupero saldo in corso...")
    balance = await bybit_client.get_wallet_balance()

    if balance is None:
        await msg.edit_text("❌ Impossibile recuperare il saldo. Verifica le API keys con /test.")
        return

    upl = balance["totalPerpUPL"]
    upl_emoji = "📈" if upl >= 0 else "📉"
    upl_sign = "+" if upl >= 0 else ""

    coins_lines = "\n".join(
        [f"  {coin}: `{amount:,.2f}`" for coin, amount in balance["coins"].items()]
    ) or "  Nessun saldo"

    text = (
        "💰 *SALDO ACCOUNT — Bybit Unified*\n\n"
        f"Equity totale:        `{balance['totalEquity']:>12,.2f} $`\n"
        f"Wallet balance:       `{balance['totalWalletBalance']:>12,.2f} $`\n"
        f"Margine disponibile:  `{balance['totalAvailableBalance']:>12,.2f} $`\n"
        f"Margine impegnato:    `{balance['totalInitialMargin']:>12,.2f} $`\n\n"
        f"{upl_emoji} *PnL aperto (perps):* `{upl_sign}{upl:,.2f} $`\n\n"
        f"💵 *Coin:*\n{coins_lines}"
    )
    await msg.edit_text(text, parse_mode="Markdown")


# ─── /posizioni ──────────────────────────────────────────────────

async def posizioni_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    bybit_client = context.application.bot_data.get("bybit_client")
    if not bybit_client or not bybit_client.has_keys:
        await update.message.reply_text(
            "⚠️ API Bybit non configurate.\nUsa /start per il setup."
        )
        return

    msg = await update.message.reply_text("📋 Recupero posizioni in corso...")
    positions = await bybit_client.get_positions()

    if not positions:
        await msg.edit_text("📋 *Nessuna posizione aperta al momento.*", parse_mode="Markdown")
        return

    lines = ["📋 *POSIZIONI APERTE — Bybit*\n"]
    total_pnl = 0.0

    for i, p in enumerate(positions, 1):
        side = p["side"]
        side_emoji = "🟢 LONG" if side == "Buy" else "🔴 SHORT"
        pnl = p["unrealisedPnl"]
        pnl_pct = p["pnlPct"]
        total_pnl += pnl

        pnl_sign = "+" if pnl >= 0 else ""
        pnl_emoji = "✅" if pnl >= 0 else "🔴"
        arrow = "▲" if pnl >= 0 else "▼"

        mark_vs_entry = ""
        if side == "Buy":
            mark_vs_entry = "🟢" if p["markPrice"] >= p["avgPrice"] else "🔴"
        else:
            mark_vs_entry = "🟢" if p["markPrice"] <= p["avgPrice"] else "🔴"

        liq = f"`{p['liqPrice']} $`" if p["liqPrice"] else "`—`"

        tp_sl = ""
        if p["takeProfit"] or p["stopLoss"]:
            tp = f"TP: `{p['takeProfit']} $`" if p["takeProfit"] else ""
            sl = f"SL: `{p['stopLoss']} $`" if p["stopLoss"] else ""
            tp_sl = "\n   " + "  |  ".join(filter(None, [tp, sl]))

        status = ""
        if p["positionStatus"] != "Normal":
            status = f"  ⚠️ `{p['positionStatus']}`"

        line = (
            f"{i}) `{p['symbol']}`  {side_emoji}  x{p['leverage']}{status}\n"
            f"   Size:   `{p['size']}`\n"
            f"   Entry:  `{p['avgPrice']:,.2f} $`\n"
            f"   Mark:   {mark_vs_entry} `{p['markPrice']:,.2f} $`\n"
            f"   PnL:    `{pnl_sign}{pnl:,.2f} $`  (`{pnl_sign}{pnl_pct:.1f}%`) {pnl_emoji} {arrow}\n"
            f"   Liq:    {liq}"
            f"{tp_sl}"
        )
        lines.append(line)

    total_sign = "+" if total_pnl >= 0 else ""
    total_emoji = "✅" if total_pnl >= 0 else "🔴"
    lines.append(f"\n─────────────────────")
    lines.append(f"Totale PnL aperto: `{total_sign}{total_pnl:,.2f} $` {total_emoji}")

    await msg.edit_text("\n".join(lines), parse_mode="Markdown")

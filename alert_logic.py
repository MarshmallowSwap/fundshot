# ─────────────────────────────────────────────────────────────────
# ALERT LOGIC — Soglie e stati interni del bot funding
# ─────────────────────────────────────────────────────────────────

import time

# ─── SOGLIE (%) ──────────────────────────────────────────────────
THRESHOLD_HARD       = 2.00
THRESHOLD_EXTREME    = 1.50
THRESHOLD_HIGH       = 1.00
THRESHOLD_CLOSE_TIP  = 0.23
THRESHOLD_RIENTRO    = 0.75
RESET_THRESHOLD      = 0.02
COOLDOWN_SECONDS     = 120

# Ordine di priorità dei livelli (dal più alto al più basso)
LEVEL_ORDER = ["hard", "extreme", "high", "close_tip", "none"]

# ─── STATO INTERNO ───────────────────────────────────────────────
# alert_sent[symbol]  → livello attuale dell'alert per ogni simbolo
# reset_time[symbol]  → timestamp del reset Bybit per ogni simbolo
alert_sent: dict[str, str] = {}
reset_time: dict[str, float] = {}


def classify_funding(rate: float) -> str | None:
    """Classifica il funding rate in un livello operativo."""
    abs_rate = abs(rate)
    if abs_rate >= THRESHOLD_HARD:
        return "hard"
    if abs_rate >= THRESHOLD_EXTREME:
        return "extreme"
    if abs_rate >= THRESHOLD_HIGH:
        return "high"
    if abs_rate >= THRESHOLD_CLOSE_TIP:
        return "close_tip"
    return None


def is_upgrade(current: str, new: str) -> bool:
    """
    Ritorna True se il nuovo livello è un upgrade rispetto al corrente.
    Opzione A: lo stato si resetta SOLO quando tocca RIENTRO (≤0.75%).
    Stesso livello o livello inferiore → False (anti-spam).
    """
    if current == "none":
        return True
    try:
        current_idx = LEVEL_ORDER.index(current)
        new_idx = LEVEL_ORDER.index(new)
        return new_idx < current_idx  # indice più basso = priorità più alta
    except ValueError:
        return False


def _direction(rate: float) -> str:
    """Direzione contrarian in base al segno del funding."""
    return "SHORT" if rate > 0 else "LONG"


def _direction_emoji(rate: float) -> str:
    return "🔴 SHORT" if rate > 0 else "🟢 LONG"


def _format_rate(rate: float) -> str:
    return f"{rate:+.4f}%"


def _format_interval(interval: str) -> str:
    return f"{interval}H"


def format_alert(symbol: str, rate: float, interval: str, level: str) -> str:
    """Formatta il messaggio di alert Telegram."""
    rate_str = _format_rate(rate)
    interval_str = _format_interval(interval)
    direction = _direction_emoji(rate)

    if level == "hard":
        return (
            f"🔴 *HARD FUNDING* — `{symbol}`\n"
            f"Rate:      `{rate_str}`  _(ogni {interval_str})_\n"
            f"Segnale:   ⚡ {direction}"
        )
    if level == "extreme":
        return (
            f"🔥 *EXTREME FUNDING* — `{symbol}`\n"
            f"Rate:      `{rate_str}`  _(ogni {interval_str})_\n"
            f"Segnale:   ⚡ {direction}"
        )
    if level == "high":
        return (
            f"🚨 *HIGH FUNDING* — `{symbol}`\n"
            f"Rate:      `{rate_str}`  _(ogni {interval_str})_\n"
            f"Segnale:   ⚡ {direction}"
        )
    if level == "close_tip":
        chiudi = "SHORT" if rate > 0 else "LONG"
        return (
            f"ℹ️ *CONSIGLIO CHIUSURA* — `{symbol}`\n"
            f"Rate:      `{rate_str}`  _(ogni {interval_str})_\n"
            f"Valuta di chiudere posizioni {chiudi}"
        )
    return f"ℹ️ `{symbol}` funding: `{rate_str}` _(ogni {interval_str})_"


def format_rientro(symbol: str, rate: float, interval: str) -> str:
    """Messaggio di rientro/normalizzazione."""
    return (
        f"✅ *FUNDING RIENTRATO* — `{symbol}`\n"
        f"Rate:      `{_format_rate(rate)}`  _(ogni {_format_interval(interval)})_\n"
        f"Eccesso normalizzato"
    )


def process_funding(symbol: str, rate: float, interval: str) -> str | None:
    """
    Logica principale di decisione alert per un simbolo.
    Ritorna il messaggio da inviare, oppure None se nessun alert.
    """
    current_state = alert_sent.get(symbol, "none")
    now = time.time()

    # ── 1) RESET BYBIT ─────────────────────────────────────────────
    if abs(rate) < RESET_THRESHOLD:
        alert_sent[symbol] = "none"
        reset_time[symbol] = now
        return None  # nessun alert durante il reset

    # ── 2) COOLDOWN POST-RESET ─────────────────────────────────────
    last_reset = reset_time.get(symbol, 0)
    if now - last_reset < COOLDOWN_SECONDS:
        return None  # silenzio durante i 120s

    # ── 3) CLASSIFICA LIVELLO ATTUALE ─────────────────────────────
    level = classify_funding(rate)

    # ── 4) RIENTRO ────────────────────────────────────────────────
    # Solo se il simbolo era in stato di alert (non "none")
    if abs(rate) <= THRESHOLD_RIENTRO and current_state not in ("none", "close_tip"):
        alert_sent[symbol] = "none"
        return format_rientro(symbol, rate, interval)

    # ── 5) NESSUN LIVELLO RILEVANTE ───────────────────────────────
    if level is None:
        return None

    # ── 6) ANTI-SPAM: solo se cambia categoria (Opzione A) ────────
    if not is_upgrade(current_state, level):
        return None

    # ── 7) INVIA ALERT ────────────────────────────────────────────
    alert_sent[symbol] = level
    return format_alert(symbol, rate, interval, level)


def get_active_alerts() -> dict[str, str]:
    """Ritorna tutti i simboli con alert attivo (stato != none)."""
    return {s: l for s, l in alert_sent.items() if l != "none"}


def reset_all_states():
    """Azzera tutti gli stati (utile per test o riavvio)."""
    alert_sent.clear()
    reset_time.clear()

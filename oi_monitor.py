"""
oi_monitor.py — OI spike monitoring on all USDT perpetual pairs.
Alert independent from the main funding rate loop.

Default threshold: spike >= 3% in 5min (or <= -3% for drop)
"""
import logging
import time
import requests

logger = logging.getLogger(__name__)

OI_SPIKE_THRESHOLD = 3.0
OI_DROP_THRESHOLD  = -3.0
_last_oi_alert: dict[str, float] = {}
OI_COOLDOWN_SEC = 300


def _fetch_oi(symbol: str) -> dict | None:
    try:
        r = requests.get(
            "https://api.bybit.com/v5/market/open-interest",
            params={"category": "linear", "symbol": symbol,
                    "intervalTime": "5min", "limit": 3},
            timeout=8
        )
        data = r.json()
        if data.get("retCode") == 0:
            items = data["result"]["list"]
            if len(items) < 2:
                return None
            curr = float(items[0]["openInterest"])
            prev = float(items[1]["openInterest"])
            chg  = (curr - prev) / prev * 100 if prev else 0
            return {"oi": curr, "change_5m": chg}
    except Exception as e:
        logger.debug("fetch_oi %s: %s", symbol, e)
    return None


def _fetch_funding(symbol: str) -> float | None:
    try:
        r = requests.get(
            "https://api.bybit.com/v5/market/tickers",
            params={"category": "linear", "symbol": symbol},
            timeout=5
        )
        data = r.json()
        if data.get("retCode") == 0:
            items = data["result"]["list"]
            if items:
                return float(items[0].get("fundingRate", 0)) * 100
    except Exception:
        pass
    return None


def _get_suggestion(oi_chg: float, funding_pct: float | None) -> str:
    """
    Generate suggested action based on OI change + funding rate.

    OI rising = new positions entering → trend accelerating
      + negative funding → shorts paying → OPEN LONG
      + positive funding → longs paying  → OPEN SHORT
      + neutral funding  → speculative spike

    OI falling = positions closing → trend exhausting
      → CLOSE open positions
    """
    if funding_pct is None:
        if oi_chg >= OI_SPIKE_THRESHOLD:
            return "OI surging — funding unavailable, monitor direction before entering"
        else:
            return "OI dropping sharply — reduce exposure"

    abs_f    = abs(funding_pct)
    strength = "strong" if abs_f >= 0.5 else "moderate" if abs_f >= 0.1 else "weak"

    if oi_chg >= OI_SPIKE_THRESHOLD:
        if funding_pct < -0.01:
            return (
                "🟢 *OPEN LONG*\n"
                "Shorts paying funding (" + f"{funding_pct:+.4f}%" + "), new long positions entering\n"
                "Signal: " + strength
            )
        elif funding_pct > 0.01:
            return (
                "🔴 *OPEN SHORT*\n"
                "Longs paying funding (" + f"{funding_pct:+.4f}%" + "), new short positions entering\n"
                "Signal: " + strength
            )
        else:
            return (
                "⚪ *CAUTION* — speculative spike\n"
                "Neutral funding (" + f"{funding_pct:+.4f}%" + "), no clear direction\n"
                "Avoid new positions"
            )
    else:
        if funding_pct < -0.01:
            return (
                "🟡 *CLOSE LONG* (if open)\n"
                "Momentum exhausting — longs exiting the market\n"
                "Funding still negative (" + f"{funding_pct:+.4f}%" + ") but OI falling"
            )
        elif funding_pct > 0.01:
            return (
                "🟡 *CLOSE SHORT* (if open)\n"
                "Momentum exhausting — shorts exiting the market\n"
                "Funding still positive (" + f"{funding_pct:+.4f}%" + ") but OI falling"
            )
        else:
            return (
                "⚠️ *EXIT POSITIONS*\n"
                "Market unwinding — neutral funding (" + f"{funding_pct:+.4f}%" + "), OI falling\n"
                "Volatility decreasing"
            )


def format_oi_spike_alert(symbol: str, oi_chg: float, funding_pct: float | None) -> str:
    arrow    = "\u25b2" if oi_chg > 0 else "\u25bc"
    emoji    = "\u26a1" if oi_chg >= OI_SPIKE_THRESHOLD else "\U0001f4c9"
    kind     = "SPIKE" if oi_chg >= OI_SPIKE_THRESHOLD else "DROP"
    f_str    = f"{funding_pct:+.4f}%" if funding_pct is not None else "n/a"
    suggestion = _get_suggestion(oi_chg, funding_pct)

    return (
        f"{emoji} *OI {kind}*\n"
        f"*{symbol}*\n"
        "\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n"
        f"\U0001f4ca OI 5m:      `{arrow} {oi_chg:+.2f}%`\n"
        f"\U0001f4b8 Funding:   `{f_str}`\n"
        "\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n"
        f"{suggestion}"
    )


def check_oi_spikes(tickers: list) -> list[tuple[str, str, float]]:
    alerts = []
    now = time.monotonic()

    for ticker in tickers:
        symbol = ticker.get("symbol", "")
        if not symbol.endswith("USDT"):
            continue
        if now - _last_oi_alert.get(symbol, 0) < OI_COOLDOWN_SEC:
            continue

        oi_data = _fetch_oi(symbol)
        if not oi_data:
            continue

        chg = oi_data["change_5m"]
        if chg >= OI_SPIKE_THRESHOLD or chg <= OI_DROP_THRESHOLD:
            funding = _fetch_funding(symbol)
            msg = format_oi_spike_alert(symbol, chg, funding)
            _last_oi_alert[symbol] = now
            logger.info("OI spike %s: %+.2f%%", symbol, chg)
            alerts.append((symbol, msg, chg))

    return alerts

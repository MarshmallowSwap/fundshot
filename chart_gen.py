"""
chart_gen.py — Genera grafici candlestick da Bybit klines e li restituisce come BytesIO.
Usato dal bot per inviare un grafico insieme agli alert di funding.
"""
import io
import logging
import requests
import datetime

logger = logging.getLogger(__name__)

def fetch_klines(symbol: str, interval: str = "15", limit: int = 60, exchange: str = "bybit") -> list:
    """Scarica le klines dall'exchange specificato (API pubblica, no auth)."""
    try:
        if exchange == "binance":
            # Binance Futures: simbolo in formato BTCUSDT, interval in formato 15m
            sym = symbol.replace("-USDT-SWAP", "").replace("/", "")
            r = requests.get(
                "https://fapi.binance.com/fapi/v1/klines",
                params={"symbol": sym, "interval": f"{interval}m", "limit": limit},
                timeout=10,
            )
            data = r.json()
            if isinstance(data, list):
                # Binance: [openTime, open, high, low, close, volume, ...]
                return [[k[0], k[1], k[2], k[3], k[4], k[5]] for k in data]
        elif exchange == "okx":
            # OKX: simbolo in formato BTC-USDT-SWAP
            inst_id = symbol if "-SWAP" in symbol else f"{symbol.replace('USDT','')}-USDT-SWAP"
            r = requests.get(
                "https://www.okx.com/api/v5/market/candles",
                params={"instId": inst_id, "bar": f"{interval}m", "limit": limit},
                timeout=10,
            )
            data = r.json()
            if data.get("code") == "0":
                # OKX: [ts, open, high, low, close, vol, ...] dal più recente
                rows = data.get("data", [])
                return list(reversed(rows))
        else:
            # Bybit (default)
            r = requests.get(
                "https://api.bybit.com/v5/market/kline",
                params={"category": "linear", "symbol": symbol, "interval": interval, "limit": limit},
                timeout=10,
            )
            data = r.json()
            if data.get("retCode") == 0:
                return data["result"]["list"]
    except Exception as e:
        logger.error("fetch_klines %s/%s: %s", exchange, symbol, e)
    return []


def generate_chart(symbol: str, funding_rate: float, exchange: str = "bybit") -> io.BytesIO | None:
    """Genera un grafico candlestick 15m e lo restituisce come BytesIO PNG."""
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import matplotlib.patches as mpatches
        from matplotlib.patches import FancyBboxPatch
    except ImportError:
        logger.warning("matplotlib non installato — grafico non disponibile")
        return None

    klines = fetch_klines(symbol, interval="15", limit=60, exchange=exchange)
    if not klines or len(klines) < 5:
        logger.warning("generate_chart: troppo pochi dati per %s/%s (%d candles)", exchange, symbol, len(klines) if klines else 0)
        return None

    # Bybit restituisce dal più recente al più vecchio — invertiamo
    if exchange == "bybit":
        klines = list(reversed(klines))

    times  = [datetime.datetime.utcfromtimestamp(int(k[0]) / 1000) for k in klines]
    opens  = [float(k[1]) for k in klines]
    highs  = [float(k[2]) for k in klines]
    lows   = [float(k[3]) for k in klines]
    closes = [float(k[4]) for k in klines]

    # Colori
    BG      = "#0d1117"
    GRID    = "#21262d"
    GREEN   = "#26a641"
    RED     = "#f85149"
    TEXT    = "#e6edf3"
    SUBTEXT = "#8b949e"
    ACCENT  = "#58a6ff"

    fig, ax = plt.subplots(figsize=(10, 5), facecolor=BG)
    ax.set_facecolor(BG)

    # Candele
    width = 0.0006  # larghezza relativa
    for i, (t, o, h, l, c) in enumerate(zip(times, opens, highs, lows, closes)):
        color = GREEN if c >= o else RED
        # Corpo
        ax.bar(i, abs(c - o), bottom=min(o, c), color=color, width=0.7, linewidth=0)
        # Stoppino
        ax.plot([i, i], [l, h], color=color, linewidth=0.8, alpha=0.8)

    # Prezzo corrente
    last_price = closes[-1]
    ax.axhline(y=last_price, color=ACCENT, linewidth=0.8, linestyle="--", alpha=0.7)
    ax.text(len(times) - 0.5, last_price, f" {last_price:.4f}",
            color=ACCENT, fontsize=8, va="center", fontweight="bold")

    # Funding rate label
    fr_color = RED if funding_rate > 0 else GREEN
    fr_text = f"Funding: {funding_rate:+.4f}%"
    ax.text(0.01, 0.97, fr_text, transform=ax.transAxes,
            color=fr_color, fontsize=10, fontweight="bold",
            va="top", ha="left",
            bbox=dict(boxstyle="round,pad=0.3", facecolor=BG, edgecolor=fr_color, alpha=0.8))

    # Titolo
    ax.set_title(f"{symbol}  •  15m", color=TEXT, fontsize=12, fontweight="bold", pad=8)

    # Asse X — mostra solo alcune etichette ora
    step = max(1, len(times) // 8)
    ax.set_xticks(range(0, len(times), step))
    ax.set_xticklabels(
        [times[i].strftime("%H:%M") for i in range(0, len(times), step)],
        color=SUBTEXT, fontsize=7
    )
    ax.tick_params(axis="y", colors=SUBTEXT, labelsize=7)
    ax.tick_params(axis="x", colors=SUBTEXT, labelsize=7)

    # Griglia
    ax.grid(True, color=GRID, linewidth=0.5, alpha=0.7)
    ax.set_xlim(-1, len(times))

    # Scala Y dinamica — evita grafico piatto su simboli poco volatili
    price_range = max(highs) - min(lows)
    if price_range < last_price * 0.001:  # range < 0.1% del prezzo
        pad = last_price * 0.005  # aggiungi 0.5% di padding
        ax.set_ylim(min(lows) - pad, max(highs) + pad)

    for spine in ax.spines.values():
        spine.set_edgecolor(GRID)

    plt.tight_layout(pad=0.5)

    buf = io.BytesIO()
    plt.savefig(buf, format="png", dpi=130, facecolor=BG, bbox_inches="tight")
    plt.close(fig)
    buf.seek(0)
    return buf

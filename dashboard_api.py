#!/usr/bin/env python3
"""
dashboard_api.py -- FundShot Dashboard Backend
Flask API che accetta X-API-Key e X-API-Secret header per ogni richiesta.
Avvio: source /root/fundshot/venv/bin/activate && python3 dashboard_api.py
"""
import json
import os
import time
import hmac
import hashlib
import subprocess
import logging

from flask import Flask, request, jsonify
from flask_cors import CORS
import requests

app = Flask(__name__)
CORS(app, resources={r"/api/*": {"origins": "*"}})

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("dashboard_api")

BYBIT_BASE = "https://api.bybit.com"
BYBIT_TEST = "https://api-testnet.bybit.com"
RECV_WINDOW = "20000"


def _base_url(testnet=False):
    return BYBIT_TEST if testnet else BYBIT_BASE


def _sign_qs(api_secret, timestamp, api_key, qs):
    param_str = f"{timestamp}{api_key}{RECV_WINDOW}{qs}"
    return hmac.new(api_secret.encode(),
                    param_str.encode(), hashlib.sha256).hexdigest()


def get_credentials():
    api_key = request.headers.get("X-API-Key", "").strip()
    api_secret = request.headers.get("X-API-Secret", "").strip()
    testnet = request.headers.get("X-Testnet", "false").lower() == "true"
    return api_key, api_secret, testnet


def bybit_get(path, params, api_key, api_secret, testnet):
    from urllib.parse import urlencode
    qs = urlencode(params)
    ts = str(int(time.time() * 1000))
    sig = _sign_qs(api_secret, ts, api_key, qs)
    headers = {
        "X-BAPI-API-KEY": api_key,
        "X-BAPI-SIGN": sig,
        "X-BAPI-SIGN-TYPE": "2",
        "X-BAPI-TIMESTAMP": ts,
        "X-BAPI-RECV-WINDOW": RECV_WINDOW,
    }
    url = f"{_base_url(testnet)}{path}?{qs}"
    r = requests.get(url, headers=headers, timeout=10)
    return r.json()


def bybit_public_get(path, params, testnet=False):
    from urllib.parse import urlencode
    url = f"{_base_url(testnet)}{path}?{urlencode(params)}"
    r = requests.get(url, timeout=10)
    return r.json()


@app.route("/api/ping", methods=["GET", "OPTIONS"])
def ping():
    return jsonify({"ok": True, "message": "FundShot API online"})


@app.route("/api/wallet", methods=["GET", "OPTIONS"])
def wallet():
    if request.method == "OPTIONS":
        return jsonify({}), 200
    api_key, api_secret, testnet = get_credentials()
    if not api_key or not api_secret:
        return jsonify({"ok": False, "error": "Credenziali mancanti"}), 401
    try:
        data = bybit_get("/v5/account/wallet-balance",
                         {"accountType": "UNIFIED"},
                         api_key, api_secret, testnet)
        if data.get("retCode") != 0:
            return jsonify({"ok": False, "error": data.get("retMsg")}), 400
        info = data["result"]["list"][0]
        return jsonify({
            "ok": True,
            "equity": float(info.get("totalEquity", 0)),
            "upnl": float(info.get("totalPerpUPL", 0)),
            "margin": float(info.get("totalInitialMargin", 0)),
            "available": float(info.get("totalAvailableBalance", 0)),
            "realisedPnl": float(info.get("totalRealizedPnl", 0)),
        })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/positions", methods=["GET", "OPTIONS"])
def positions():
    if request.method == "OPTIONS":
        return jsonify({}), 200
    api_key, api_secret, testnet = get_credentials()
    if not api_key or not api_secret:
        return jsonify({"ok": False, "error": "Credenziali mancanti"}), 401
    try:
        data = bybit_get("/v5/position/list",
                         {"category": "linear", "settleCoin": "USDT"},
                         api_key, api_secret, testnet)
        if data.get("retCode") != 0:
            return jsonify({"ok": False, "error": data.get("retMsg")}), 400
        result = []
        for p in data["result"]["list"]:
            size = float(p.get("size", 0))
            if size == 0: continue
            mark = float(p.get("markPrice", 0))
            liq = float(p.get("liqPrice", 0) or 0)
            side = p.get("side", "Buy")
            lev = float(p.get("leverage", 1))
            upnl = float(p.get("unrealisedPnl", 0))
            avg = float(p.get("avgPrice", 1)) or 1
            dist_pct = 0.0
            if liq > 0 and mark > 0:
                dist_pct = (mark - liq) / mark * 100 if side == "Buy" else (liq - mark) / mark * 100
            entry_margin = size * avg / lev if lev > 0 else 1
            result.append({
                "symbol": p.get("symbol"), "side": side, "size": size,
                "markPrice": mark, "entryPrice": avg, "liqPrice": liq,
                "liqDistPct": round(dist_pct, 2), "leverage": lev,
                "unrealisedPnl": upnl,
                "pnlPct": round(upnl / entry_margin * 100, 2) if entry_margin != 0 else 0,
            })
        return jsonify({"ok": True, "positions": result})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/tickers", methods=["GET", "OPTIONS"])
def tickers():
    if request.method == "OPTIONS":
        return jsonify({}), 200
    try:
        data = bybit_public_get("/v5/market/tickers", {"category": "linear"})
        if data.get("retCode") != 0:
            return jsonify({"ok": False, "error": data.get("retMsg")}), 400
        result = []
        for t in data["result"]["list"]:
            fr = float(t.get("fundingRate", 0))
            if fr == 0: continue
            result.append({
                "symbol": t.get("symbol"), "fundingRate": fr,
                "fundingRatePct": round(fr * 100, 4),
                "nextFundingTime": int(t.get("nextFundingTime", 0)),
                "lastPrice": float(t.get("lastPrice", 0)),
                "price24hPcnt": round(float(t.get("price24hPcnt", 0)) * 100, 2),
            })
        result.sort(key=lambda x: abs(x["fundingRatePct"]), reverse=True)
        return jsonify({"ok": True, "tickers": result[:100]})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/analytics", methods=["GET", "OPTIONS"])
def analytics():
    if request.method == "OPTIONS":
        return jsonify({}), 200
    try:
        gf = "/root/fundshot/funding_gains.json"
        d = json.load(open(gf)) if os.path.exists(gf) else {}
        tg = sum(v.get("total_gain_usdt", 0) for v in d.values())
        tc = sum(len(v.get("cycles", [])) for v in d.values())
        w = sum(1 for v in d.values() if v.get("total_gain_usdt", 0) > 0)
        return jsonify({"ok": True, "total_gain": round(tg, 4),
                         "total_cycles": tc, "win_rate": round(w/len(d)*100, 1) if d else 0,
                         "top_symbols": sorted([{"symbol":k,"gain":round(v.get("total_gain_usdt",0),4),"cycles":len(v.get("cycles",[]))} for k,v in d.items()],key=lambda x:x
["gain"],reverse=True)[:10]})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/status", methods=["GET", "OPTIONS"])
def status():
    if request.method == "OPTIONS":
        return jsonify({}), 200
    try:
        r = subprocess.run(["systemctl", "is-active", "fundshot"],
                           capture_output=True, text=True, timeout=5)
        return jsonify({"ok": True, "bot_active": r.stdout.strip() == "active", "api_version": "v1.0"})
    except Exception:
        return jsonify({"ok": True, "bot_active": False, "api_version": "v1.0"})


if __name__ == "__main__":
    log.info("Dashboard API avviata su porta 8081")
    app.run(host="0.0.0.0", port=8081, debug=False)

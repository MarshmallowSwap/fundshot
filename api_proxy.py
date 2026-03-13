import asyncio
#!/usr/bin/env python3
"""
Mini proxy HTTP per Funding King Bot dashboard — v4
Endpoint:
  GET /api/status       → health check
  GET /api/wallet       → wallet UNIFIED (equity, avail, upnl, margin, realisedPnl)
  GET /api/positions    → posizioni lineari USDT (+ cumRealisedPnl, pnlPct)
  GET /api/new-listings → simboli listati negli ultimi 30gg con ticker (fundingRate, nextFundingTime, markPrice)
"""
import hmac, hashlib, time, json, os, urllib.request, urllib.parse, threading
from http.server import HTTPServer, BaseHTTPRequestHandler

API_KEY    = os.environ.get('BYBIT_API_KEY', '')
API_SECRET = os.environ.get('BYBIT_API_SECRET', '')
BASE_URL   = 'https://api.bybit.com/v5'
PORT       = 8080
NEW_DAYS   = 30   # giorni per badge "NEW"

# ── Cache new-listings (aggiorna ogni 5 min) ──
_new_cache     = None
_new_cache_ts  = 0
_new_cache_ttl = 300  # 5 minuti

def bybit_sign(params: dict) -> dict:
    ts      = str(int(time.time() * 1000))
    recv    = '5000'
    qs      = urllib.parse.urlencode(params)
    pre     = ts + API_KEY + recv + qs
    sign    = hmac.new(API_SECRET.encode(), pre.encode(), hashlib.sha256).hexdigest()
    return {
        'X-BAPI-API-KEY':      API_KEY,
        'X-BAPI-TIMESTAMP':    ts,
        'X-BAPI-RECV-WINDOW':  recv,
        'X-BAPI-SIGN':         sign,
    }

def bybit_get(path: str, params: dict, signed=True) -> dict:
    if signed:
        headers = bybit_sign(params)
    else:
        headers = {'Content-Type': 'application/json'}
    qs  = urllib.parse.urlencode(params)
    url = f"{BASE_URL}{path}?{qs}"
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())

def bybit_public(path: str, params: dict) -> dict:
    return bybit_get(path, params, signed=False)

CORS = {
    'Access-Control-Allow-Origin':  '*',
    'Access-Control-Allow-Methods': 'GET, POST, DELETE, OPTIONS',
    'Access-Control-Allow-Headers': 'Content-Type',
    'Content-Type':                 'application/json; charset=utf-8',
}

def calc_pnl_pct(p):
    try:
        upnl = float(p.get('unrealisedPnl', 0))
        avg  = float(p.get('avgPrice', 0))
        sz   = float(p.get('size', 0))
        lev  = float(p.get('leverage', 1) or 1)
        if avg > 0 and sz > 0 and lev > 0:
            margin_used = (avg * sz) / lev
            return round(upnl / margin_used * 100, 4)
    except:
        pass
    return float(p.get('unrealisedPnlPcnt', 0))

def get_new_listings():
    """Restituisce simboli linear USDT listati negli ultimi NEW_DAYS giorni con dati ticker."""
    global _new_cache, _new_cache_ts
    now_ts = time.time()
    if _new_cache and (now_ts - _new_cache_ts) < _new_cache_ttl:
        return _new_cache

    cutoff_ms = int((now_ts - NEW_DAYS * 86400) * 1000)

    # 1. Ottieni tutti gli strumenti linear
    instruments = []
    cursor = ''
    while True:
        params = {'category': 'linear', 'limit': '1000'}
        if cursor:
            params['cursor'] = cursor
        data = bybit_public('/market/instruments-info', params)
        batch = data.get('result', {}).get('list', [])
        instruments.extend(batch)
        cursor = data.get('result', {}).get('nextPageCursor', '')
        if not cursor or not batch:
            break

    # 2. Filtra USDT perpetual listati di recente (launchTime ≤ cutoff)
    new_syms = []
    for inst in instruments:
        sym = inst.get('symbol', '')
        if not sym.endswith('USDT'):
            continue
        launch = int(inst.get('launchTime', 0))
        if launch >= cutoff_ms:
            new_syms.append({
                'symbol':      sym,
                'launchTime':  launch,
                'daysAgo':     round((now_ts * 1000 - launch) / 86400000, 1),
                'status':      inst.get('status', ''),
                'fundingInterval': int(inst.get('fundingInterval', 480)),
            })

    # 3. Ottieni ticker per ciascun simbolo nuovo
    result = []
    for item in sorted(new_syms, key=lambda x: x['launchTime'], reverse=True):
        sym = item['symbol']
        try:
            td = bybit_public('/market/tickers', {'category': 'linear', 'symbol': sym})
            t  = (td.get('result', {}).get('list') or [{}])[0]
            fr  = float(t.get('fundingRate',   0)) * 100
            nft = int(t.get('nextFundingTime', 0))
            mp  = float(t.get('markPrice',     0))
            # next predicted = fundingRate (Bybit lo aggiorna ogni ora)
            item.update({
                'fundingRate':    round(fr, 6),
                'nextFundingTime': nft,
                'markPrice':       mp,
                'price24hPcnt':    round(float(t.get('price24hPcnt', 0)) * 100, 2),
                'turnover24h':     float(t.get('turnover24h', 0)),
            })
        except:
            item.update({'fundingRate': 0, 'nextFundingTime': 0, 'markPrice': 0,
                         'price24hPcnt': 0, 'turnover24h': 0})
        result.append(item)

    _new_cache    = result
    _new_cache_ts = now_ts
    return result


# ── Cache OI / LS ratio (TTL 60s) ────────────────────────────────────────────
_oi_cache = {}
_ls_cache = {}
_cache_ts  = {}

def _cached(key, ttl=60):
    import time
    return _cache_ts.get(key, 0) + ttl > time.time()

def get_open_interest_top(limit=10):
    """Restituisce OI per i top symbol per funding rate."""
    import time
    if _cached('oi') and _oi_cache:
        return _oi_cache.get('data', [])
    try:
        # Prendi top tickers per funding rate
        td = bybit_public('/market/tickers', {'category': 'linear'})
        tickers = td.get('result', {}).get('list', [])
        # Filtra USDT, ordina per |fundingRate|
        tickers = [t for t in tickers if t.get('symbol','').endswith('USDT')]
        tickers.sort(key=lambda x: abs(float(x.get('fundingRate',0))), reverse=True)
        top = tickers[:limit]
        result = []
        for t in top:
            sym = t.get('symbol','')
            try:
                oi_data = bybit_public('/market/open-interest', {
                    'category': 'linear', 'symbol': sym, 'intervalTime': '5min', 'limit': '1'
                })
                oi_list = oi_data.get('result', {}).get('list', [{}])
                oi_val  = float(oi_list[0].get('openInterest', 0)) if oi_list else 0
            except:
                oi_val = float(t.get('openInterestValue', 0))
            result.append({
                'symbol':      sym,
                'fundingRate': round(float(t.get('fundingRate', 0)) * 100, 6),
                'openInterest': round(oi_val, 2),
                'markPrice':   float(t.get('markPrice', 0)),
                'turnover24h': float(t.get('turnover24h', 0)),
            })
        _oi_cache['data'] = result
        _cache_ts['oi'] = time.time()
        return result
    except Exception as e:
        return []

def get_ls_ratio(symbols=None, period='1h'):
    """Restituisce Long/Short ratio per una lista di simboli."""
    import time
    if not symbols:
        td = bybit_public('/market/tickers', {'category': 'linear'})
        tickers = td.get('result', {}).get('list', [])
        tickers = [t for t in tickers if t.get('symbol','').endswith('USDT')]
        tickers.sort(key=lambda x: abs(float(x.get('fundingRate',0))), reverse=True)
        symbols = [t.get('symbol','') for t in tickers[:10]]
    result = []
    for sym in symbols:
        try:
            ls_data = bybit_public('/market/account-ratio', {
                'category': 'linear', 'symbol': sym, 'period': period, 'limit': '1'
            })
            ls_list = ls_data.get('result', {}).get('list', [{}])
            if ls_list:
                buy_r  = float(ls_list[0].get('buyRatio',  0.5))
                sell_r = float(ls_list[0].get('sellRatio', 0.5))
            else:
                buy_r, sell_r = 0.5, 0.5
        except:
            buy_r, sell_r = 0.5, 0.5
        result.append({
            'symbol':    sym,
            'longRatio': round(buy_r * 100, 2),
            'shortRatio': round(sell_r * 100, 2),
        })
    return result

def get_vol_spikes(threshold_pct=200):
    """Restituisce simboli con volume 24h anomalo (> threshold% rispetto media)."""
    try:
        td = bybit_public('/market/tickers', {'category': 'linear'})
        tickers = td.get('result', {}).get('list', [])
        tickers = [t for t in tickers if t.get('symbol','').endswith('USDT')]
        spikes = []
        for t in tickers:
            vol_24h   = float(t.get('volume24h', 0))
            turnover  = float(t.get('turnover24h', 0))
            fr        = float(t.get('fundingRate', 0)) * 100
            pct_chg   = float(t.get('price24hPcnt', 0)) * 100
            if abs(fr) >= 1.0 and vol_24h > 0:
                spikes.append({
                    'symbol':       t.get('symbol', ''),
                    'volume24h':    round(vol_24h, 2),
                    'turnover24h':  round(turnover, 2),
                    'fundingRate':  round(fr, 6),
                    'price24hPcnt': round(pct_chg, 2),
                })
        spikes.sort(key=lambda x: abs(x['fundingRate']), reverse=True)
        return spikes[:20]
    except:
        return []

def compute_analytics():
    """Legge funding_gains.json e calcola metriche avanzate."""
    import math
    gains_path = '/root/funding-king-bot/funding_gains.json'
    try:
        with open(gains_path) as f:
            gains = json.load(f)
    except:
        return {'ok': False, 'msg': 'funding_gains.json non trovato'}

    all_records = []
    for sym, records in gains.items():
        if isinstance(records, list):
            for r in records:
                r['symbol'] = sym
                all_records.append(r)

    if not all_records:
        return {'ok': True, 'total': 0, 'msg': 'Nessun dato disponibile'}

    gains_by_level = {'jackpot': [], 'extreme': [], 'hard': [], 'high': [], 'all': []}
    for r in all_records:
        g = float(r.get('gain', 0))
        lvl = str(r.get('level', 'high')).lower()
        gains_by_level['all'].append(g)
        if lvl in gains_by_level:
            gains_by_level[lvl].append(g)

    def win_rate(lst):
        if not lst: return 0
        return round(sum(1 for x in lst if x > 0) / len(lst) * 100, 1)

    def avg(lst):
        return round(sum(lst)/len(lst), 4) if lst else 0

    def max_drawdown(lst):
        if not lst: return 0
        peak, dd = lst[0], 0
        cumulative = 0
        for g in lst:
            cumulative += g
            if cumulative > peak: peak = cumulative
            dd = min(dd, cumulative - peak)
        return round(dd, 4)

    def sharpe(lst, rf=0):
        if len(lst) < 2: return 0
        mu = sum(lst)/len(lst)
        std = math.sqrt(sum((x-mu)**2 for x in lst)/len(lst))
        return round((mu - rf) / std, 3) if std > 0 else 0

    def sortino(lst, rf=0):
        if len(lst) < 2: return 0
        mu = sum(lst)/len(lst)
        neg = [x for x in lst if x < rf]
        if not neg: return 9.99
        dd_std = math.sqrt(sum((x-rf)**2 for x in neg)/len(neg))
        return round((mu - rf) / dd_std, 3) if dd_std > 0 else 0

    all_g = gains_by_level['all']

    # Heatmap per ora
    heatmap = {}
    for r in all_records:
        ts = r.get('timestamp', r.get('ts', ''))
        try:
            from datetime import datetime
            dt = datetime.fromisoformat(str(ts).replace('Z', '+00:00'))
            h = dt.hour
        except:
            h = 0
        heatmap[h] = heatmap.get(h, 0) + float(r.get('gain', 0))

    return {
        'ok': True,
        'total_records': len(all_records),
        'total_gain': round(sum(all_g), 4),
        'win_rate': win_rate(all_g),
        'avg_gain': avg(all_g),
        'max_drawdown': max_drawdown(all_g),
        'sharpe': sharpe(all_g),
        'sortino': sortino(all_g),
        'by_level': {
            lvl: {
                'count': len(lst),
                'win_rate': win_rate(lst),
                'avg_gain': avg(lst),
                'total': round(sum(lst), 4),
            }
            for lvl, lst in gains_by_level.items() if lvl != 'all'
        },
        'heatmap': {str(h): round(v, 4) for h, v in sorted(heatmap.items())},
    }

class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def send_cors(self, code=200, body=b'{}'):
        self.send_response(code)
        for k, v in CORS.items():
            self.send_header(k, v)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_cors(200, b'{}')

    def _read_body(self) -> bytes:
        length = int(self.headers.get('Content-Length', 0))
        return self.rfile.read(length) if length > 0 else b'{}'

    def send_cors_headers(self):
        self.send_response(200)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, DELETE, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type, Authorization')
        self.end_headers()

    def send_cors(self, code, body):
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, DELETE, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type, Authorization')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)


    def do_POST(self):
        path = self.path.split('?')[0]
        try:
            if path == '/api/alert-config':
                body = self._read_body()
                new_cfg = json.loads(body.decode('utf-8'))
                cfg_path = '/root/funding-king-bot/alert_config.json'
                # Carica config esistente (o default)
                if os.path.exists(cfg_path):
                    with open(cfg_path) as _f:
                        current = json.load(_f)
                else:
                    current = {
                        'enabled': {
                            'critico': True, 'hard': True, 'extreme': True, 'high': True,
                            'close_tip': True, 'warn_tip': False, 'rientro': True,
                            'next_funding': True, 'pump_dump': False, 'level_change': False,
                            'liquidation': True, 'multi_pos': False
                        },
                        'thresholds': {
                            'critico': 2.50, 'hard': 2.00, 'extreme': 1.50, 'high': 1.00,
                            'close_tip': 0.75, 'warn_tip': 0.25, 'rientro': 0.20
                        }
                    }
                # Merge parziale
                if 'enabled' in new_cfg:
                    current.setdefault('enabled', {}).update(new_cfg['enabled'])
                if 'thresholds' in new_cfg:
                    for k, v in new_cfg['thresholds'].items():
                        try:
                            current.setdefault('thresholds', {})[k] = float(v)
                        except (ValueError, TypeError):
                            pass
                with open(cfg_path, 'w') as _f:
                    json.dump(current, _f, indent=2)
                out = {'ok': True, 'msg': 'Config salvata', 'config': current}
                self.send_cors(200, json.dumps(out).encode())
            elif path == "/api/close-by-mm":
                _av = globals().get("_CLOSE_AVAIL", False)
                if not _av:
                    self.send_cors(500, json.dumps({"ok": False, "msg": globals().get("_CLOSE_ERR_MSG","unavail")}).encode())
                else:
                    b2 = self._read_body()
                    d2 = json.loads(b2.decode("utf-8")) if b2 else {}
                    thr = float(d2.get("threshold", 15.0))
                    lp = _asyncio.new_event_loop()
                    r1 = lp.run_until_complete(_close_by_mm(thr))
                    lp.close()
                    self.send_cors(200, json.dumps({"ok": True, "result": r1}).encode())
            elif path == "/api/close-by-pnl":
                _av = globals().get("_CLOSE_AVAIL", False)
                if not _av:
                    self.send_cors(500, json.dumps({"ok": False, "msg": globals().get("_CLOSE_ERR_MSG","unavail")}).encode())
                else:
                    b3 = self._read_body()
                    d3 = json.loads(b3.decode("utf-8")) if b3 else {}
                    neg = float(d3.get("neg_threshold", -5.0))
                    pos = d3.get("pos_threshold", None)
                    if pos is not None: pos = float(pos)
                    lp2 = _asyncio.new_event_loop()
                    r2 = lp2.run_until_complete(_close_by_pnl(neg, pos))
                    lp2.close()
                    self.send_cors(200, json.dumps({"ok": True, "result": r2}).encode())
            else:
                self.send_cors(404, b'{"ok":false,"msg":"not found"}')
        except Exception as e:
            err = json.dumps({'ok': False, 'msg': str(e)}).encode()
            self.send_cors(500, err)

    def do_DELETE(self):
        path = self.path.split('?')[0]
        try:
            if path == '/api/alert-config':
                default_cfg = {
                    'enabled': {
                        'critico': True, 'hard': True, 'extreme': True, 'high': True,
                        'close_tip': True, 'warn_tip': False, 'rientro': True,
                        'next_funding': True, 'pump_dump': False, 'level_change': False,
                        'liquidation': True, 'multi_pos': False
                    },
                    'thresholds': {
                        'critico': 2.50, 'hard': 2.00, 'extreme': 1.50, 'high': 1.00,
                        'close_tip': 0.75, 'warn_tip': 0.25, 'rientro': 0.20
                    }
                }
                cfg_path = '/root/funding-king-bot/alert_config.json'
                with open(cfg_path, 'w') as _f:
                    json.dump(default_cfg, _f, indent=2)
                out = {'ok': True, 'msg': 'Config ripristinata ai default', 'config': default_cfg}
                self.send_cors(200, json.dumps(out).encode())
            elif path == "/api/close-by-mm":
                _av = globals().get("_CLOSE_AVAIL", False)
                if not _av:
                    self.send_cors(500, json.dumps({"ok": False, "msg": globals().get("_CLOSE_ERR_MSG","unavail")}).encode())
                else:
                    b2 = self._read_body()
                    d2 = json.loads(b2.decode("utf-8")) if b2 else {}
                    thr = float(d2.get("threshold", 15.0))
                    lp = _asyncio.new_event_loop()
                    r1 = lp.run_until_complete(_close_by_mm(thr))
                    lp.close()
                    self.send_cors(200, json.dumps({"ok": True, "result": r1}).encode())
            elif path == "/api/close-by-pnl":
                _av = globals().get("_CLOSE_AVAIL", False)
                if not _av:
                    self.send_cors(500, json.dumps({"ok": False, "msg": globals().get("_CLOSE_ERR_MSG","unavail")}).encode())
                else:
                    b3 = self._read_body()
                    d3 = json.loads(b3.decode("utf-8")) if b3 else {}
                    neg = float(d3.get("neg_threshold", -5.0))
                    pos = d3.get("pos_threshold", None)
                    if pos is not None: pos = float(pos)
                    lp2 = _asyncio.new_event_loop()
                    r2 = lp2.run_until_complete(_close_by_pnl(neg, pos))
                    lp2.close()
                    self.send_cors(200, json.dumps({"ok": True, "result": r2}).encode())
            else:
                self.send_cors(404, b'{"ok":false,"msg":"not found"}')
        except Exception as e:
            err = json.dumps({'ok': False, 'msg': str(e)}).encode()
            self.send_cors(500, err)

    def do_GET(self):
        path = self.path.split('?')[0]
        try:
            if path == '/api/wallet':
                data = bybit_get('/account/wallet-balance', {'accountType': 'UNIFIED'})
                acc  = (data.get('result', {}).get('list') or [{}])[0]
                coins = acc.get('coin', [])
                total_realised = sum(float(c.get('cumRealisedPnl', 0)) for c in coins)
                out  = {
                    'ok':          data.get('retCode') == 0,
                    'equity':      float(acc.get('totalEquity', 0)),
                    'avail':       float(acc.get('totalAvailableBalance', 0)),
                    'upnl':        float(acc.get('totalPerpUPL', 0)),
                    'margin':      float(acc.get('totalInitialMargin', 0)),
                    'walletBal':   float(acc.get('totalWalletBalance', 0)),
                    'realisedPnl': total_realised,
                }
                self.send_cors(200, json.dumps(out).encode())

            elif path == '/api/positions':
                data = bybit_get('/position/list', {'category': 'linear', 'settleCoin': 'USDT', 'limit': '50'})
                raw  = data.get('result', {}).get('list', [])
                pos  = [p for p in raw if float(p.get('size', 0)) > 0]
                out  = {
                    'ok':        data.get('retCode') == 0,
                    'positions': [{
                        'symbol':             p.get('symbol'),
                        'side':               p.get('side'),
                        'size':               float(p.get('size', 0)),
                        'avgPrice':           float(p.get('avgPrice', 0)),
                        'markPrice':          float(p.get('markPrice', 0)),
                        'liqPrice':           float(p.get('liqPrice', 0) or 0),
                        'unrealisedPnl':      float(p.get('unrealisedPnl', 0)),
                        'unrealisedPnlPcnt':  calc_pnl_pct(p),
                        'cumRealisedPnl':     float(p.get('cumRealisedPnl', 0)),
                        'leverage':           p.get('leverage', ''),
                        'positionValue':      float(p.get('positionValue', 0)),
                    } for p in pos],
                }
                self.send_cors(200, json.dumps(out).encode())

            elif path == '/api/new-listings':
                result = get_new_listings()
                out = {'ok': True, 'count': len(result), 'items': result}
                self.send_cors(200, json.dumps(out).encode())


            elif path == '/api/tickers':
                td = bybit_public('/market/tickers', {'category': 'linear'})
                raw = td.get('result', {}).get('list', [])
                out = {
                    'ok': True,
                    'count': len(raw),
                    'tickers': [{
                        'symbol':          t.get('symbol'),
                        'fundingRate':     round(float(t.get('fundingRate', 0)) * 100, 6),
                        'nextFundingTime': int(t.get('nextFundingTime', 0)),
                        'markPrice':       float(t.get('markPrice', 0)),
                        'indexPrice':      float(t.get('indexPrice', 0)),
                        'fundingInterval': int(t.get('fundingRateTimestamp', 0)),
                        'price24hPcnt':    round(float(t.get('price24hPcnt', 0)) * 100, 2),
                        'turnover24h':     float(t.get('turnover24h', 0)),
                        'volume24h':       float(t.get('volume24h', 0)),
                        'openInterestValue': float(t.get('openInterestValue', 0)),
                    } for t in raw if t.get('symbol','').endswith('USDT')]
                }
                self.send_cors(200, json.dumps(out).encode())

            elif path == '/api/oi':
                data = get_open_interest_top(10)
                self.send_cors(200, json.dumps({'ok': True, 'items': data}).encode())

            elif path == '/api/ls-ratio':
                params_qs = self.path.split('?')[1] if '?' in self.path else ''
                import urllib.parse as up
                qs = dict(up.parse_qsl(params_qs))
                syms = qs.get('symbols', '').split(',') if qs.get('symbols') else None
                period = qs.get('period', '1h')
                data = get_ls_ratio(syms, period)
                self.send_cors(200, json.dumps({'ok': True, 'items': data}).encode())

            elif path == '/api/vol-spikes':
                data = get_vol_spikes()
                self.send_cors(200, json.dumps({'ok': True, 'items': data}).encode())

            elif path == '/api/analytics':
                data = compute_analytics()
                self.send_cors(200, json.dumps(data).encode())

            elif path == '/api/alert-config':
                # GET: restituisce configurazione alert corrente
                try:
                    cfg_path = '/root/funding-king-bot/alert_config.json'
                    if os.path.exists(cfg_path):
                        with open(cfg_path) as _f:
                            cfg = json.load(_f)
                    else:
                        # Default se non esiste ancora
                        cfg = {
                            'enabled': {
                                'critico': True, 'hard': True, 'extreme': True, 'high': True,
                                'close_tip': True, 'warn_tip': False, 'rientro': True,
                                'next_funding': True, 'pump_dump': False, 'level_change': False,
                                'liquidation': True, 'multi_pos': False
                            },
                            'thresholds': {
                                'critico': 2.50, 'hard': 2.00, 'extreme': 1.50, 'high': 1.00,
                                'close_tip': 0.75, 'warn_tip': 0.25, 'rientro': 0.20
                            }
                        }
                    out = {'ok': True, 'config': cfg}
                except Exception as _e:
                    out = {'ok': False, 'msg': str(_e)}
                self.send_cors(200, json.dumps(out).encode())

            elif path == '/api/status':
                out = {'ok': True, 'msg': 'proxy running v4', 'key_set': bool(API_KEY)}
                self.send_cors(200, json.dumps(out).encode())

            elif path == "/api/close-by-mm":
                _av = globals().get("_CLOSE_AVAIL", False)
                if not _av:
                    self.send_cors(500, json.dumps({"ok": False, "msg": globals().get("_CLOSE_ERR_MSG","unavail")}).encode())
                else:
                    b2 = self._read_body()
                    d2 = json.loads(b2.decode("utf-8")) if b2 else {}
                    thr = float(d2.get("threshold", 15.0))
                    lp = _asyncio.new_event_loop()
                    r1 = lp.run_until_complete(_close_by_mm(thr))
                    lp.close()
                    self.send_cors(200, json.dumps({"ok": True, "result": r1}).encode())
            elif path == "/api/close-by-pnl":
                _av = globals().get("_CLOSE_AVAIL", False)
                if not _av:
                    self.send_cors(500, json.dumps({"ok": False, "msg": globals().get("_CLOSE_ERR_MSG","unavail")}).encode())
                else:
                    b3 = self._read_body()
                    d3 = json.loads(b3.decode("utf-8")) if b3 else {}
                    neg = float(d3.get("neg_threshold", -5.0))
                    pos = d3.get("pos_threshold", None)
                    if pos is not None: pos = float(pos)
                    lp2 = _asyncio.new_event_loop()
                    r2 = lp2.run_until_complete(_close_by_pnl(neg, pos))
                    lp2.close()
                    self.send_cors(200, json.dumps({"ok": True, "result": r2}).encode())
            else:
                self.send_cors(404, b'{"ok":false,"msg":"not found"}')

        except Exception as e:
            err = json.dumps({'ok': False, 'msg': str(e)}).encode()
            self.send_cors(500, err)

if __name__ == '__main__':
    if not API_KEY or not API_SECRET:
        print("ERRORE: BYBIT_API_KEY / BYBIT_API_SECRET non impostati")
        exit(1)
    srv = HTTPServer(('0.0.0.0', PORT), Handler)
    print(f"API proxy v4 avviato su porta {PORT}")
    srv.serve_forever()

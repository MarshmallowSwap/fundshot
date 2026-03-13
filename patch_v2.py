import re, shutil, os

BOT_DIR = '/root/fundshot'

# ══════════════════════════════════════════════════════════════
# 1. PATCH proxy_v5.py — aggiungi endpoint trading/switch e guardian
# ══════════════════════════════════════════════════════════════
proxy_path = f'{BOT_DIR}/proxy_v5.py'
shutil.copy(proxy_path, proxy_path + '.bak_v2')

with open(proxy_path, 'r') as f:
    proxy = f.read()

PROXY_NEW_ENDPOINTS = '''
        # ── TRADING SWITCH ────────────────────────────────────────
        elif path == '/api/trading/switch':
            if method == 'GET':
                sw = {'enabled': False, 'testnet': True}
                swf = os.path.join(os.path.dirname(CONFIG_FILE), 'trading_switch.json')
                if os.path.exists(swf):
                    try:
                        with open(swf) as f: sw = json.load(f)
                    except: pass
                self._json(sw)
            elif method == 'POST':
                data = self._body()
                swf = os.path.join(os.path.dirname(CONFIG_FILE), 'trading_switch.json')
                try:
                    with open(swf, 'w') as f: json.dump(data, f, indent=2)
                    # Aggiorna anche .env sul bot
                    env_path = os.path.join(BOT_PATH, '.env')
                    if os.path.exists(env_path):
                        with open(env_path, 'r') as f: env = f.read()
                        enabled = str(data.get('enabled', False)).lower()
                        testnet = str(data.get('testnet', True)).lower()
                        if 'AUTO_TRADING=' in env:
                            env = re.sub(r'AUTO_TRADING=\\S*', f'AUTO_TRADING={enabled}', env)
                        else:
                            env += f'\\nAUTO_TRADING={enabled}'
                        if 'TRADING_TESTNET=' in env:
                            env = re.sub(r'TRADING_TESTNET=\\S*', f'TRADING_TESTNET={testnet}', env)
                        else:
                            env += f'\\nTRADING_TESTNET={testnet}'
                        with open(env_path, 'w') as f: f.write(env)
                    self._json({'ok': True, 'enabled': data.get('enabled')})
                except Exception as e:
                    self._json({'ok': False, 'error': str(e)}, 500)

        # ── GUARDIAN CONFIG ───────────────────────────────────────
        elif path == '/api/guardian':
            gf = os.path.join(os.path.dirname(CONFIG_FILE), 'guardian_config.json')
            if method == 'GET':
                cfg = {'mmr_pct': 15.0, 'profit_target': 20.0, 'max_loss': -15.0}
                if os.path.exists(gf):
                    try:
                        with open(gf) as f: cfg = json.load(f)
                    except: pass
                self._json(cfg)
            elif method == 'POST':
                data = self._body()
                try:
                    with open(gf, 'w') as f: json.dump(data, f, indent=2)
                    self._json({'ok': True})
                except Exception as e:
                    self._json({'ok': False, 'error': str(e)}, 500)

        # ── TRADER CONFIG (parametri strategia) ──────────────────
        elif path == '/api/trader-config':
            tf = os.path.join(BOT_PATH, 'trader_config.json')
            if method == 'GET':
                cfg = {}
                if os.path.exists(tf):
                    try:
                        with open(tf) as f: cfg = json.load(f)
                    except: pass
                self._json(cfg)
            elif method == 'POST':
                data = self._body()
                try:
                    with open(tf, 'w') as f: json.dump(data, f, indent=2)
                    self._json({'ok': True})
                except Exception as e:
                    self._json({'ok': False, 'error': str(e)}, 500)

'''

# Aggiungi BOT_PATH constant dopo PORT
if 'BOT_PATH' not in proxy:
    proxy = proxy.replace(
        "PORT        = 8080",
        "PORT        = 8080\nBOT_PATH    = '/root/fundshot'"
    )

# Aggiungi import re se manca
if 'import re' not in proxy:
    proxy = proxy.replace('import json, os,', 'import json, os, re,')

# Inserisci i nuovi endpoint prima della riga "else: self._json"
# Cerca il pattern della gestione 404
anchor = "            else:\n                self._json({'error': 'not found'}"
if anchor not in proxy:
    # prova variante
    anchor = "            else:\n                self._json({\"error\": \"not found\"}"
if anchor in proxy:
    proxy = proxy.replace(anchor, PROXY_NEW_ENDPOINTS + anchor)
    print("✅ Endpoint proxy aggiunti")
else:
    # Fallback: cerca qualsiasi 404 handler
    m = re.search(r'([ \t]+else:\s*\n\s*self\._json\([^)]*not.found[^)]*\))', proxy)
    if m:
        proxy = proxy[:m.start()] + PROXY_NEW_ENDPOINTS + proxy[m.start():]
        print("✅ Endpoint proxy aggiunti (regex fallback)")
    else:
        # Inserisci prima di end_server/main
        proxy = proxy.rstrip() + "\n\n" + "# Nuovi endpoint aggiunti via patch\n" + PROXY_NEW_ENDPOINTS
        print("⚠️ Endpoint proxy aggiunti in fondo (manuale)")

with open(proxy_path, 'w') as f:
    f.write(proxy)
print(f"✅ proxy_v5.py salvato")


# ══════════════════════════════════════════════════════════════
# 2. PATCH index.html — Master Switch + Guardian + Auto Trading
# ══════════════════════════════════════════════════════════════
html_path = f'{BOT_DIR}/index.html'
shutil.copy(html_path, html_path + '.bak_v2')

with open(html_path, 'r') as f:
    html = f.read()

# ── Blocco HTML da inserire all'inizio di page-settings ──────
SETTINGS_HTML = '''
  <!-- ══ MASTER SWITCH ══════════════════════════════════════ -->
  <div class="set-card" style="border:1px solid rgba(139,92,246,.3);background:rgba(139,92,246,.06)">
    <div class="set-card-head">
      <div style="flex:1">
        <div class="set-card-title" style="color:#a78bfa">⚡ Master Switch</div>
        <div class="set-card-sub">Modalità operativa del bot</div>
      </div>
      <span id="ms-badge" style="border-radius:20px;padding:4px 14px;font-size:.82rem;font-weight:700;
        background:rgba(0,200,100,.15);color:#00c864;border:1px solid rgba(0,200,100,.3)">🔔 ALERT ONLY</span>
    </div>
    <div class="set-card-body">
      <div class="set-row" style="margin-bottom:12px">
        <div class="set-row-lbl">🔔 Alert Only</div>
        <div style="display:flex;gap:8px;align-items:center">
          <button id="ms-btn-alert"  class="btn btn-primary btn-sm" onclick="msSet(false)" style="min-width:130px">🔔 ALERT ONLY</button>
          <button id="ms-btn-trade"  class="btn btn-ghost  btn-sm" onclick="msSet(true)"  style="min-width:130px">⚡ TRADING ATTIVO</button>
        </div>
      </div>
      <div class="set-row">
        <div class="set-row-lbl">🧪 Testnet mode</div>
        <label class="toggle-sw"><input type="checkbox" id="ms-testnet" checked onchange="msSave()"><span class="slider"></span></label>
      </div>
      <div id="ms-msg" style="font-size:.8rem;margin-top:8px;min-height:16px;color:var(--green)"></div>
    </div>
  </div>

  <!-- ══ GUARDIAN ════════════════════════════════════════════ -->
  <div class="set-card" style="border:1px solid rgba(239,68,68,.25);background:rgba(239,68,68,.04)">
    <div class="set-card-head">
      <div>
        <div class="set-card-title" style="color:#f87171">🛡️ Guardian — Protezione Automatica</div>
        <div class="set-card-sub">Chiude posizioni al superamento delle soglie di rischio</div>
      </div>
    </div>
    <div class="set-card-body">
      <div class="set-row">
        <div>
          <div class="set-row-lbl">Soglia MMR (%)</div>
          <div style="font-size:.75rem;color:var(--text3)">Chiude se maintenance margin scende sotto questa soglia</div>
        </div>
        <input type="number" id="gd-mmr"    class="inp" value="15"  min="1"   max="50"  step="1"   style="width:90px;padding:5px 8px;font-size:.85rem">
      </div>
      <div class="set-row">
        <div>
          <div class="set-row-lbl">Profit target (%)</div>
          <div style="font-size:.75rem;color:var(--text3)">Chiude la posizione al raggiungimento del guadagno</div>
        </div>
        <input type="number" id="gd-profit" class="inp" value="20"  min="1"   max="200" step="1"   style="width:90px;padding:5px 8px;font-size:.85rem">
      </div>
      <div class="set-row">
        <div>
          <div class="set-row-lbl">Max loss portfolio (%)</div>
          <div style="font-size:.75rem;color:var(--text3)">Stop globale al raggiungimento della perdita massima</div>
        </div>
        <input type="number" id="gd-loss"   class="inp" value="-15" min="-100" max="-1" step="1"   style="width:90px;padding:5px 8px;font-size:.85rem">
      </div>
      <div class="api-save-row" style="margin-top:12px">
        <button class="btn btn-primary btn-sm" onclick="gdSave()">💾 Salva Guardian</button>
        <span id="gd-msg" style="font-size:.8rem;color:var(--green)"></span>
      </div>

      <div style="border-top:1px solid rgba(255,255,255,.07);margin:14px 0 10px"></div>

      <!-- Chiusura per MM% -->
      <div style="font-size:.83rem;font-weight:600;color:#f87171;margin-bottom:8px">🔴 Chiusura Posizioni per MM%</div>
      <div class="set-row-lbl" style="margin-bottom:8px;font-size:.78rem;color:var(--text2)">
        Chiude tutte le posizioni sotto la soglia Maintenance Margin
      </div>
      <div class="set-row" style="margin-bottom:10px">
        <div class="set-row-lbl">Soglia MM% trigger</div>
        <input type="number" id="gd-mm-trigger" class="inp" value="15" min="1" max="50" step="1" style="width:90px;padding:5px 8px;font-size:.85rem">
      </div>
      <button class="btn btn-sm" onclick="gdCloseByMM()"
        style="background:rgba(239,68,68,.15);color:#ef4444;border:1px solid rgba(239,68,68,.3);margin-bottom:14px">
        ⚡ Esegui Chiusura per MM%
      </button>

      <!-- Chiusura per PnL -->
      <div style="font-size:.83rem;font-weight:600;color:#f59e0b;margin-bottom:8px">🟡 Chiusura Posizioni per PnL Portfolio</div>
      <div class="set-row-lbl" style="margin-bottom:8px;font-size:.78rem;color:var(--text2)">
        Chiude quando il PnL totale raggiunge la soglia impostata
      </div>
      <div class="set-row" style="margin-bottom:10px">
        <div class="set-row-lbl">Soglia PnL (USDT) — negativo=stop loss, positivo=take profit</div>
        <input type="number" id="gd-pnl-trigger" class="inp" value="-20" step="1" style="width:90px;padding:5px 8px;font-size:.85rem">
      </div>
      <button class="btn btn-sm" onclick="gdCloseByPnl()"
        style="background:rgba(245,158,11,.15);color:#f59e0b;border:1px solid rgba(245,158,11,.3)">
        ⚡ Esegui Chiusura per PnL
      </button>
    </div>
  </div>

  <!-- ══ AUTO TRADING PARAMS ═══════════════════════════════════ -->
  <div class="set-card" style="border:1px solid rgba(99,102,241,.25);background:rgba(99,102,241,.04)">
    <div class="set-card-head">
      <div style="flex:1">
        <div class="set-card-title" style="color:#818cf8">🤖 Auto Trading — Parametri</div>
        <div class="set-card-sub">Strategia mean reversion su funding estremo</div>
      </div>
      <span style="background:rgba(244,63,94,.12);color:#ef4444;border:1px solid rgba(244,63,94,.3);
        border-radius:20px;padding:4px 14px;font-size:.82rem;font-weight:700">BETA</span>
    </div>
    <div class="set-card-body">

      <div class="alert-section-lbl" style="margin-top:0">🛡️ Risk Management</div>
      <div class="set-row">
        <div class="set-row-lbl">💰 Size per trade (USDT)</div>
        <input type="number" id="tr-size"   class="inp" value="50"  min="1"   step="1"   style="width:90px;padding:5px 8px;font-size:.85rem" oninput="trUpdateNotionale()">
      </div>
      <div class="set-row">
        <div class="set-row-lbl">⚡ Leva</div>
        <input type="number" id="tr-leva"   class="inp" value="2"   min="1" max="10" step="1" style="width:90px;padding:5px 8px;font-size:.85rem" oninput="trUpdateNotionale()">
      </div>
      <div class="set-row" style="opacity:.7">
        <div class="set-row-lbl">📐 Notionale</div>
        <span id="tr-notionale" style="font-size:.85rem;font-weight:700;color:var(--text1)">100 USDT</span>
      </div>
      <div class="set-row">
        <div class="set-row-lbl">🔒 Max posizioni</div>
        <input type="number" id="tr-maxpos" class="inp" value="2"   min="1" max="10" step="1" style="width:90px;padding:5px 8px;font-size:.85rem">
      </div>
      <div class="set-row">
        <div class="set-row-lbl">🛡️ Stop Loss (%)</div>
        <input type="number" id="tr-sl-pct" class="inp" value="1.2" min="0.1" max="20" step="0.1" style="width:90px;padding:5px 8px;font-size:.85rem">
      </div>
      <div class="set-row">
        <div class="set-row-lbl">🎯 TP1 — % posizione chiusa</div>
        <input type="number" id="tr-tp1pct" class="inp" value="30"  min="10" max="90" step="5"   style="width:90px;padding:5px 8px;font-size:.85rem">
      </div>

      <div class="alert-section-lbl">🔍 Filtri Entrata</div>
      <div class="set-row">
        <div class="set-row-lbl">⏱ Persistenza min (periodi)</div>
        <input type="number" id="tr-persist-n" class="inp" value="2"   min="1" max="10" step="1"   style="width:90px;padding:5px 8px;font-size:.85rem">
      </div>
      <div class="set-row">
        <div class="set-row-lbl">📊 OI change min (%)</div>
        <input type="number" id="tr-oi-pct"    class="inp" value="1.5" min="0.1" max="20" step="0.1" style="width:90px;padding:5px 8px;font-size:.85rem">
      </div>
      <div class="set-row">
        <div class="set-row-lbl">⏰ Min minuti al reset</div>
        <input type="number" id="tr-minreset"   class="inp" value="30"  min="5" max="120" step="5"   style="width:90px;padding:5px 8px;font-size:.85rem">
      </div>

      <div class="alert-section-lbl">🎯 TP per livello</div>
      <div style="overflow-x:auto">
        <table style="width:100%;border-collapse:collapse;font-size:.82rem">
          <thead>
            <tr style="color:var(--text3);text-align:left">
              <th style="padding:6px 8px">Livello</th>
              <th style="padding:6px 8px">TP1 (%)</th>
              <th style="padding:6px 8px">Trailing (%)</th>
              <th style="padding:6px 8px">Max cap (%)</th>
            </tr>
          </thead>
          <tbody>
            <tr>
              <td style="padding:5px 8px"><span class="lvl lvl-jackpot">🤑 JACKPOT</span></td>
              <td style="padding:5px 8px"><input type="number" id="tr-tp-jackpot-tp1"   class="inp" value="1.2" step="0.1" min="0.1" style="width:70px;padding:4px 6px;font-size:.82rem"></td>
              <td style="padding:5px 8px"><input type="number" id="tr-tp-jackpot-trail" class="inp" value="1.2" step="0.1" min="0.1" style="width:70px;padding:4px 6px;font-size:.82rem"></td>
              <td style="padding:5px 8px"><input type="number" id="tr-tp-jackpot-cap"   class="inp" value="6.0" step="0.5" min="1"   style="width:70px;padding:4px 6px;font-size:.82rem"></td>
            </tr>
            <tr>
              <td style="padding:5px 8px"><span class="lvl lvl-hard">🔴 HARD</span></td>
              <td style="padding:5px 8px"><input type="number" id="tr-tp-hard-tp1"      class="inp" value="1.2" step="0.1" min="0.1" style="width:70px;padding:4px 6px;font-size:.82rem"></td>
              <td style="padding:5px 8px"><input type="number" id="tr-tp-hard-trail"    class="inp" value="1.2" step="0.1" min="0.1" style="width:70px;padding:4px 6px;font-size:.82rem"></td>
              <td style="padding:5px 8px"><input type="number" id="tr-tp-hard-cap"      class="inp" value="6.0" step="0.5" min="1"   style="width:70px;padding:4px 6px;font-size:.82rem"></td>
            </tr>
            <tr>
              <td style="padding:5px 8px"><span class="lvl lvl-extreme">🔥 EXTREME</span></td>
              <td style="padding:5px 8px"><input type="number" id="tr-tp-extreme-tp1"   class="inp" value="1.0" step="0.1" min="0.1" style="width:70px;padding:4px 6px;font-size:.82rem"></td>
              <td style="padding:5px 8px"><input type="number" id="tr-tp-extreme-trail" class="inp" value="1.0" step="0.1" min="0.1" style="width:70px;padding:4px 6px;font-size:.82rem"></td>
              <td style="padding:5px 8px"><input type="number" id="tr-tp-extreme-cap"   class="inp" value="5.0" step="0.5" min="1"   style="width:70px;padding:4px 6px;font-size:.82rem"></td>
            </tr>
            <tr>
              <td style="padding:5px 8px"><span class="lvl lvl-high">🚨 HIGH</span></td>
              <td style="padding:5px 8px"><input type="number" id="tr-tp-high-tp1"      class="inp" value="0.8" step="0.1" min="0.1" style="width:70px;padding:4px 6px;font-size:.82rem"></td>
              <td style="padding:5px 8px"><input type="number" id="tr-tp-high-trail"    class="inp" value="0.8" step="0.1" min="0.1" style="width:70px;padding:4px 6px;font-size:.82rem"></td>
              <td style="padding:5px 8px"><input type="number" id="tr-tp-high-cap"      class="inp" value="4.0" step="0.5" min="1"   style="width:70px;padding:4px 6px;font-size:.82rem"></td>
            </tr>
          </tbody>
        </table>
      </div>

      <div class="api-save-row" style="margin-top:16px;flex-wrap:wrap;gap:8px">
        <button class="btn btn-primary btn-sm" onclick="trSaveConfig()">💾 Salva sul Bot</button>
        <button class="btn btn-ghost   btn-sm" onclick="trResetDefaults()">↺ Default</button>
        <button class="btn btn-ghost   btn-sm" onclick="trExportJson()">↗ Esporta JSON</button>
      </div>
      <div id="tr-msg" style="font-size:.8rem;min-height:16px;margin-top:6px;color:var(--green)"></div>
    </div>
  </div>

'''

# ── JS da inserire ────────────────────────────────────────────
SETTINGS_JS = '''
/* ══════════════════════════════════════════
   MASTER SWITCH
══════════════════════════════════════════ */
async function msLoad() {
  try {
    const r = await fetch(PROXY_URL + '/api/trading/switch');
    const d = await r.json();
    msRender(d.enabled || false, d.testnet !== false);
  } catch { msRender(false, true); }
}
function msRender(enabled, testnet) {
  const badge  = document.getElementById('ms-badge');
  const btnA   = document.getElementById('ms-btn-alert');
  const btnT   = document.getElementById('ms-btn-trade');
  const tstChk = document.getElementById('ms-testnet');
  if (badge) {
    if (enabled) {
      badge.textContent = '⚡ TRADING ATTIVO';
      badge.style.background = 'rgba(139,92,246,.2)';
      badge.style.color = '#a78bfa';
      badge.style.border = '1px solid rgba(139,92,246,.4)';
    } else {
      badge.textContent = '🔔 ALERT ONLY';
      badge.style.background = 'rgba(0,200,100,.15)';
      badge.style.color = '#00c864';
      badge.style.border = '1px solid rgba(0,200,100,.3)';
    }
  }
  if (btnA) btnA.className = enabled ? 'btn btn-ghost btn-sm' : 'btn btn-primary btn-sm';
  if (btnT) btnT.className = enabled ? 'btn btn-primary btn-sm' : 'btn btn-ghost btn-sm';
  if (tstChk) tstChk.checked = testnet;
  // aggiorna badge navbar
  const nb = document.querySelector('.nav-mode-badge');
  if (nb) nb.textContent = enabled ? '⚡ TRADING' : '🔔 ALERT ONLY';
}
async function msSet(enabled) {
  const testnet = document.getElementById('ms-testnet')?.checked !== false;
  await msSend(enabled, testnet);
}
async function msSave() {
  const enabled = document.getElementById('ms-btn-trade')?.classList.contains('btn-primary') || false;
  const testnet = document.getElementById('ms-testnet')?.checked !== false;
  await msSend(enabled, testnet);
}
async function msSend(enabled, testnet) {
  const msg = document.getElementById('ms-msg');
  try {
    const r = await fetch(PROXY_URL + '/api/trading/switch', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({enabled, testnet})
    });
    const d = await r.json();
    msRender(enabled, testnet);
    if (msg) { msg.style.color='var(--green)'; msg.textContent = enabled ? '✅ Trading attivato — bot riavviato' : '✅ Modalità alert — trading disattivato'; }
    setTimeout(()=>{ if(msg) msg.textContent=''; }, 4000);
  } catch(e) {
    if (msg) { msg.style.color='#ef4444'; msg.textContent='❌ Errore: ' + e.message; }
  }
}

/* ══════════════════════════════════════════
   GUARDIAN
══════════════════════════════════════════ */
async function gdLoad() {
  try {
    const r = await fetch(PROXY_URL + '/api/guardian');
    const d = await r.json();
    const s = (id, v) => { const el=document.getElementById(id); if(el) el.value=v; };
    s('gd-mmr',    d.mmr_pct    || 15);
    s('gd-profit', d.profit_target || 20);
    s('gd-loss',   d.max_loss   || -15);
  } catch {}
}
async function gdSave() {
  const g = (id) => parseFloat(document.getElementById(id)?.value) || 0;
  const data = { mmr_pct: g('gd-mmr'), profit_target: g('gd-profit'), max_loss: g('gd-loss') };
  const msg = document.getElementById('gd-msg');
  try {
    await fetch(PROXY_URL + '/api/guardian', {
      method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(data)
    });
    if(msg){ msg.style.color='var(--green)'; msg.textContent='✅ Guardian salvato'; }
    setTimeout(()=>{ if(msg) msg.textContent=''; }, 3000);
  } catch(e) {
    if(msg){ msg.style.color='#ef4444'; msg.textContent='❌ '+e.message; }
  }
}
async function gdCloseByMM() {
  const thr = document.getElementById('gd-mm-trigger')?.value || 15;
  if (!confirm(`Chiudere TUTTE le posizioni con MM < ${thr}%?`)) return;
  try {
    const r = await fetch(PROXY_URL + '/api/close-by-mm?threshold=' + thr);
    const d = await r.json();
    showToast(d.message || 'Chiusura MM eseguita', 'ok');
  } catch(e) { showToast('Errore: '+e.message, 'err'); }
}
async function gdCloseByPnl() {
  const thr = document.getElementById('gd-pnl-trigger')?.value || -20;
  if (!confirm(`Chiudere posizioni con PnL < ${thr} USDT?`)) return;
  try {
    const r = await fetch(PROXY_URL + '/api/close-by-pnl?threshold=' + thr);
    const d = await r.json();
    showToast(d.message || 'Chiusura PnL eseguita', 'ok');
  } catch(e) { showToast('Errore: '+e.message, 'err'); }
}

/* ══════════════════════════════════════════
   AUTO TRADING PARAMS
══════════════════════════════════════════ */
const TR_DEFAULTS = {
  size:50, leva:2, maxpos:2, slPct:1.2, tp1pct:30, persistN:2, oiPct:1.5, minreset:30,
  tp:{ jackpot:[1.2,1.2,6.0], hard:[1.2,1.2,6.0], extreme:[1.0,1.0,5.0], high:[0.8,0.8,4.0] }
};
function trLoadCfg(){ try{ return JSON.parse(localStorage.getItem('fk_tr_cfg')||'null')||TR_DEFAULTS; } catch{ return TR_DEFAULTS; } }
function trSaveLoc(c){ localStorage.setItem('fk_tr_cfg', JSON.stringify(c)); }
function trGet(id){ return parseFloat(document.getElementById(id)?.value)||0; }
function trSet(id,v){ const el=document.getElementById(id); if(el) el.value=v; }
function trUpdateNotionale(){
  const s=trGet('tr-size')||50, l=trGet('tr-leva')||2;
  const el=document.getElementById('tr-notionale');
  if(el) el.textContent=(s*l).toFixed(0)+' USDT';
}
function trInitSettings(){
  const c=trLoadCfg();
  trSet('tr-size',      c.size);    trSet('tr-leva',    c.leva);
  trSet('tr-maxpos',    c.maxpos);  trSet('tr-sl-pct',  c.slPct);
  trSet('tr-tp1pct',    c.tp1pct);  trSet('tr-persist-n',c.persistN);
  trSet('tr-oi-pct',    c.oiPct);   trSet('tr-minreset', c.minreset);
  const tp=c.tp||TR_DEFAULTS.tp;
  ['jackpot','hard','extreme','high'].forEach(lvl=>{
    const v=tp[lvl]||TR_DEFAULTS.tp[lvl];
    trSet(`tr-tp-${lvl}-tp1`,v[0]); trSet(`tr-tp-${lvl}-trail`,v[1]); trSet(`tr-tp-${lvl}-cap`,v[2]);
  });
  trUpdateNotionale();
}
async function trSaveConfig(){
  const cfg={
    size:trGet('tr-size'), leva:trGet('tr-leva'), maxpos:trGet('tr-maxpos'),
    slPct:trGet('tr-sl-pct'), tp1pct:trGet('tr-tp1pct'),
    persistN:trGet('tr-persist-n'), oiPct:trGet('tr-oi-pct'), minreset:trGet('tr-minreset'),
    tp:{
      jackpot:[trGet('tr-tp-jackpot-tp1'),trGet('tr-tp-jackpot-trail'),trGet('tr-tp-jackpot-cap')],
      hard:   [trGet('tr-tp-hard-tp1'),   trGet('tr-tp-hard-trail'),   trGet('tr-tp-hard-cap')],
      extreme:[trGet('tr-tp-extreme-tp1'),trGet('tr-tp-extreme-trail'),trGet('tr-tp-extreme-cap')],
      high:   [trGet('tr-tp-high-tp1'),   trGet('tr-tp-high-trail'),   trGet('tr-tp-high-cap')],
    }
  };
  trSaveLoc(cfg);
  // Converti in formato trader.py e salva sul server
  const out={
    size:cfg.size, leva:cfg.leva, maxpos:cfg.maxpos, sl:cfg.slPct, tp1pct:cfg.tp1pct,
    persist:cfg.persistN, oi:cfg.oiPct, minreset:cfg.minreset, tp:cfg.tp
  };
  const msg=document.getElementById('tr-msg');
  try {
    await fetch(PROXY_URL+'/api/trader-config',{
      method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(out)
    });
    if(msg){ msg.style.color='var(--green)'; msg.textContent='✅ Config salvata sul bot'; }
  } catch(e) {
    if(msg){ msg.style.color='#ef4444'; msg.textContent='❌ '+e.message; }
  }
  setTimeout(()=>{ if(msg) msg.textContent=''; },4000);
}
function trResetDefaults(){ trSaveLoc(TR_DEFAULTS); trInitSettings(); showToast('↺ Default ripristinati','ok'); }
function trExportJson(){
  const cfg=trLoadCfg();
  const out={size:cfg.size,leva:cfg.leva,maxpos:cfg.maxpos,sl:cfg.slPct,tp1pct:cfg.tp1pct,
    persist:cfg.persistN,oi:cfg.oiPct,minreset:cfg.minreset,tp:cfg.tp};
  const a=document.createElement('a');
  a.href=URL.createObjectURL(new Blob([JSON.stringify(out,null,2)],{type:'application/json'}));
  a.download='trader_config.json'; a.click();
}

/* ── Init quando si apre settings ── */
function _initSettingsPage(){
  msLoad();
  gdLoad();
  trInitSettings();
}
'''

# ── Inserisci HTML dopo <div class="page" id="page-settings"> ──
anchor_html = '<div class="page" id="page-settings">\n  <div class="page-header">⚙️ Impostazioni Bot</div>'
if anchor_html in html:
    html = html.replace(anchor_html, anchor_html + '\n' + SETTINGS_HTML)
    print("✅ Blocco HTML settings inserito")
else:
    print("❌ Anchor HTML non trovato")

# ── Inserisci JS prima di /* ─── CONFIG ─── */ ──
js_anchor = "/* ─── CONFIG ─── */"
if js_anchor in html:
    html = html.replace(js_anchor, SETTINGS_JS + "\n" + js_anchor)
    print("✅ JS settings inserito")
else:
    print("❌ Anchor JS non trovato")

# ── Aggiungi chiamata _initSettingsPage() al showPage settings ──
sp_anchor = "if (id === 'settings') setTimeout(syncPullAll, 120);"
if sp_anchor in html:
    html = html.replace(sp_anchor, sp_anchor + "\n  if (id === 'settings') _initSettingsPage();")
    print("✅ _initSettingsPage() agganciato")
else:
    print("⚠️  showPage anchor non trovato — inizializzazione manuale")

# ── Rimuovi il vecchio blocco Mode Badge "ALERT ONLY" hardcoded ──
old_badge = '''  <!-- Mode Badge (Alert Only - hardcoded) -->
  <div class="set-card">
    <div class="set-card-head">
      <div style="flex:1">
        <div class="set-card-title">⚡ Modalità Operativa</div>
        <div class="set-card-sub">Il bot opera esclusivamente in modalità alert</div>
      </div>
      <span style="background:rgba(0,200,100,.15);color:#00c864;border:1px solid rgba(0,200,100,.3);
        border-radius:20px;padding:4px 14px;font-size:.82rem;font-weight:700;letter-spacing:.5px">
        🔔 ALERT ONLY
      </span>
    </div>
  </div>'''
if old_badge in html:
    html = html.replace(old_badge, '')
    print("✅ Vecchio badge ALERT ONLY rimosso")

with open(html_path, 'w') as f:
    f.write(html)
print(f"✅ index.html salvato ({html.count(chr(10))} righe)")
print("\n✅ PATCH COMPLETATA")

-- ============================================================
-- Funding King SaaS — Schema Supabase
-- Eseguire in ordine nel SQL Editor di Supabase
-- ============================================================

-- ── Utenti ────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS users (
  id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  chat_id           BIGINT UNIQUE NOT NULL,
  telegram_handle   TEXT DEFAULT '',
  plan              TEXT DEFAULT 'free' CHECK (plan IN ('free','pro','elite')),
  active_exchanges  TEXT[] DEFAULT '{}',
  created_at        TIMESTAMPTZ DEFAULT now(),
  updated_at        TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_users_chat_id ON users(chat_id);

-- ── Credenziali exchange (cifrate AES-256) ─────────────────
CREATE TABLE IF NOT EXISTS exchange_credentials (
  id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id          UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  exchange         TEXT NOT NULL CHECK (exchange IN ('bybit','binance','okx','hyperliquid')),
  api_key_enc      TEXT DEFAULT '',
  api_secret_enc   TEXT DEFAULT '',
  passphrase_enc   TEXT DEFAULT '',   -- solo OKX
  wallet_enc       TEXT DEFAULT '',   -- solo Hyperliquid
  environment      TEXT DEFAULT 'demo' CHECK (environment IN ('demo','live')),
  is_active        BOOLEAN DEFAULT true,
  created_at       TIMESTAMPTZ DEFAULT now(),
  updated_at       TIMESTAMPTZ DEFAULT now(),
  UNIQUE(user_id, exchange)
);

CREATE INDEX IF NOT EXISTS idx_creds_user_id ON exchange_credentials(user_id);
CREATE INDEX IF NOT EXISTS idx_creds_active  ON exchange_credentials(is_active);

-- ── Impostazioni trading per utente/exchange ───────────────
CREATE TABLE IF NOT EXISTS user_settings (
  user_id            UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  exchange           TEXT NOT NULL,
  trade_size_usdt    FLOAT DEFAULT 50.0,
  leverage           INT DEFAULT 5,
  min_level          TEXT DEFAULT 'high',
  auto_trading       BOOLEAN DEFAULT false,
  custom_thresholds  JSONB DEFAULT '{}',
  updated_at         TIMESTAMPTZ DEFAULT now(),
  PRIMARY KEY (user_id, exchange)
);

-- ── Storico trade ─────────────────────────────────────────
CREATE TABLE IF NOT EXISTS trades (
  id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id       UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  exchange      TEXT NOT NULL,
  symbol        TEXT NOT NULL,
  side          TEXT NOT NULL,
  entry_price   FLOAT,
  exit_price    FLOAT,
  pnl_usdt      FLOAT,
  level         TEXT,
  opened_at     TIMESTAMPTZ,
  closed_at     TIMESTAMPTZ,
  close_reason  TEXT DEFAULT '',
  created_at    TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_trades_user_id  ON trades(user_id);
CREATE INDEX IF NOT EXISTS idx_trades_exchange ON trades(exchange);
CREATE INDEX IF NOT EXISTS idx_trades_closed   ON trades(closed_at DESC);

-- ── Trigger: aggiorna updated_at automaticamente ───────────
CREATE OR REPLACE FUNCTION update_updated_at()
RETURNS TRIGGER AS $$
BEGIN
  NEW.updated_at = now();
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE TRIGGER trg_users_updated_at
  BEFORE UPDATE ON users
  FOR EACH ROW EXECUTE FUNCTION update_updated_at();

CREATE OR REPLACE TRIGGER trg_creds_updated_at
  BEFORE UPDATE ON exchange_credentials
  FOR EACH ROW EXECUTE FUNCTION update_updated_at();

-- ── Row Level Security (RLS) ───────────────────────────────
-- Abilita RLS su tutte le tabelle
ALTER TABLE users               ENABLE ROW LEVEL SECURITY;
ALTER TABLE exchange_credentials ENABLE ROW LEVEL SECURITY;
ALTER TABLE user_settings        ENABLE ROW LEVEL SECURITY;
ALTER TABLE trades               ENABLE ROW LEVEL SECURITY;

-- Il backend usa la service_role key → bypassa RLS automaticamente
-- Le policy qui sotto sono per eventuali client diretti (futuro)

-- ── Verifica ──────────────────────────────────────────────
-- SELECT table_name FROM information_schema.tables
-- WHERE table_schema = 'public'
-- ORDER BY table_name;

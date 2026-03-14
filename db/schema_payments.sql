-- ============================================================
-- FundShot — Schema pagamenti (aggiunta a schema.sql)
-- Eseguire nel SQL Editor di Supabase
-- ============================================================

-- ── Aggiorna tabella users con colonne billing ─────────────
ALTER TABLE users
  ADD COLUMN IF NOT EXISTS plan_expires_at  TIMESTAMPTZ DEFAULT NULL,
  ADD COLUMN IF NOT EXISTS billing_type     TEXT DEFAULT NULL CHECK (billing_type IN ('recurring','oneshot', NULL)),
  ADD COLUMN IF NOT EXISTS subscription_id  TEXT DEFAULT NULL;

-- ── Tabella pagamenti ──────────────────────────────────────
CREATE TABLE IF NOT EXISTS payments (
  id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id          UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  chat_id          BIGINT NOT NULL,
  nowpay_id        TEXT UNIQUE,          -- payment_id da NOWPayments
  plan             TEXT NOT NULL CHECK (plan IN ('pro','elite')),
  billing_type     TEXT NOT NULL CHECK (billing_type IN ('recurring','oneshot')),
  amount_usd       FLOAT NOT NULL,
  currency         TEXT NOT NULL,        -- btc | eth | usdt | sol | bnb | ton
  status           TEXT DEFAULT 'pending' CHECK (status IN ('pending','confirmed','failed','expired')),
  pay_address      TEXT DEFAULT '',
  pay_amount       FLOAT DEFAULT 0,
  actually_paid    FLOAT DEFAULT 0,
  created_at       TIMESTAMPTZ DEFAULT now(),
  updated_at       TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_payments_user_id   ON payments(user_id);
CREATE INDEX IF NOT EXISTS idx_payments_nowpay_id ON payments(nowpay_id);
CREATE INDEX IF NOT EXISTS idx_payments_status    ON payments(status);

-- Trigger updated_at
CREATE OR REPLACE TRIGGER trg_payments_updated_at
  BEFORE UPDATE ON payments
  FOR EACH ROW EXECUTE FUNCTION update_updated_at();

-- RLS
ALTER TABLE payments ENABLE ROW LEVEL SECURITY;

-- Aggiunge email e subscription plan IDs alla tabella users
ALTER TABLE users
  ADD COLUMN IF NOT EXISTS email TEXT DEFAULT NULL;

-- ── Referral system ───────────────────────────────────────────────────────────
ALTER TABLE users
  ADD COLUMN IF NOT EXISTS referral_code    TEXT UNIQUE DEFAULT NULL,
  ADD COLUMN IF NOT EXISTS referred_by      TEXT DEFAULT NULL;  -- referral_code del referrer

CREATE TABLE IF NOT EXISTS referrals (
  id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  referrer_user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  referred_user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  status           TEXT DEFAULT 'pending' CHECK (status IN ('pending','converted','rewarded')),
  reward_days      INT DEFAULT 0,
  created_at       TIMESTAMPTZ DEFAULT now(),
  converted_at     TIMESTAMPTZ DEFAULT NULL,
  UNIQUE(referred_user_id)  -- ogni utente può avere un solo referrer
);

CREATE INDEX IF NOT EXISTS idx_referrals_referrer ON referrals(referrer_user_id);
CREATE INDEX IF NOT EXISTS idx_referrals_status   ON referrals(status);

ALTER TABLE referrals ENABLE ROW LEVEL SECURITY;

-- Colonne referral su users
ALTER TABLE users
  ADD COLUMN IF NOT EXISTS is_influencer             BOOLEAN DEFAULT false,
  ADD COLUMN IF NOT EXISTS referral_balance_usd      FLOAT DEFAULT 0,
  ADD COLUMN IF NOT EXISTS referral_total_earned_usd FLOAT DEFAULT 0,
  ADD COLUMN IF NOT EXISTS referral_discount_pct     FLOAT DEFAULT 0,
  ADD COLUMN IF NOT EXISTS referral_wallet_usdt      TEXT DEFAULT NULL;

"""
email_service.py — FundShot SaaS
Invio email transazionali via Resend (noreply@fundshot.app).
"""

import json
import logging
import os
import urllib.request
import urllib.error
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

RESEND_API_KEY = os.getenv("RESEND_API_KEY", "")
FROM_EMAIL     = "FundShot <noreply@fundshot.app>"
SUPPORT_EMAIL  = "support@fundshot.app"


def _send(to: str, subject: str, html: str) -> bool:
    if not RESEND_API_KEY:
        logger.warning("RESEND_API_KEY non configurato — email non inviata")
        return False
    payload = json.dumps({
        "from":    FROM_EMAIL,
        "to":      [to],
        "subject": subject,
        "html":    html,
    }).encode()
    req = urllib.request.Request(
        "https://api.resend.com/emails",
        data=payload,
        headers={
            "Authorization": f"Bearer {RESEND_API_KEY}",
            "Content-Type":  "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            logger.info("Email inviata a %s: %s", to, subject)
            return True
    except urllib.error.HTTPError as e:
        logger.error("Resend error %s: %s", e.code, e.read().decode())
        return False
    except Exception as e:
        logger.error("Email error: %s", e)
        return False


def send_payment_confirmed(
    to_email: str,
    username: str,
    plan: str,
    billing_type: str,
    amount_usd: float,
    currency: str,
    expires_at: str,
) -> bool:
    plan_label    = plan.capitalize()
    billing_label = "Monthly Recurring" if billing_type == "recurring" else "One-Shot 30 days"
    subject = f"✅ Payment confirmed — FundShot {plan_label}"
    html = f"""
<!DOCTYPE html>
<html>
<head><meta charset="utf-8"></head>
<body style="font-family: -apple-system, sans-serif; background:#0f0f0f; color:#e0e0e0; margin:0; padding:40px 20px;">
  <div style="max-width:520px; margin:0 auto; background:#1a1a1a; border-radius:12px; padding:32px; border:1px solid #2a2a2a;">
    <div style="text-align:center; margin-bottom:24px;">
      <span style="font-size:48px;">⚡</span>
      <h1 style="color:#f0b429; margin:8px 0 0; font-size:24px;">FundShot</h1>
    </div>
    <h2 style="color:#4ade80; margin:0 0 16px;">Payment Confirmed ✅</h2>
    <p style="color:#a0a0a0; margin:0 0 24px;">
      Hi <strong style="color:#e0e0e0;">@{username}</strong>, your payment has been received and your plan is now active.
    </p>
    <div style="background:#111; border-radius:8px; padding:20px; margin-bottom:24px; border:1px solid #2a2a2a;">
      <table style="width:100%; border-collapse:collapse;">
        <tr><td style="color:#888; padding:6px 0;">Plan</td>
            <td style="color:#f0b429; font-weight:bold; text-align:right;">{plan_label}</td></tr>
        <tr><td style="color:#888; padding:6px 0;">Billing</td>
            <td style="color:#e0e0e0; text-align:right;">{billing_label}</td></tr>
        <tr><td style="color:#888; padding:6px 0;">Amount</td>
            <td style="color:#e0e0e0; text-align:right;">${amount_usd:.2f} ({currency.upper()})</td></tr>
        <tr><td style="color:#888; padding:6px 0;">Expires</td>
            <td style="color:#e0e0e0; text-align:right;">{expires_at}</td></tr>
      </table>
    </div>
    <p style="color:#a0a0a0; margin:0 0 24px; font-size:14px;">
      Your FundShot bot is now fully unlocked. Use <strong style="color:#e0e0e0;">/plan</strong> in Telegram to check your subscription status.
    </p>
    <div style="text-align:center;">
      <a href="https://t.me/FundShot_bot" style="background:#f0b429; color:#000; text-decoration:none; padding:12px 28px; border-radius:8px; font-weight:bold; display:inline-block;">
        Open FundShot Bot →
      </a>
    </div>
    <hr style="border:none; border-top:1px solid #2a2a2a; margin:24px 0;">
    <p style="color:#555; font-size:12px; text-align:center; margin:0;">
      Questions? Reply to this email or contact <a href="mailto:{SUPPORT_EMAIL}" style="color:#f0b429;">{SUPPORT_EMAIL}</a>
    </p>
  </div>
</body>
</html>
"""
    return _send(to_email, subject, html)


def send_plan_expiring(
    to_email: str,
    username: str,
    plan: str,
    expires_at: str,
    days_left: int,
) -> bool:
    subject = f"⚠️ Your FundShot {plan.capitalize()} plan expires in {days_left} days"
    html = f"""
<!DOCTYPE html>
<html>
<head><meta charset="utf-8"></head>
<body style="font-family: -apple-system, sans-serif; background:#0f0f0f; color:#e0e0e0; margin:0; padding:40px 20px;">
  <div style="max-width:520px; margin:0 auto; background:#1a1a1a; border-radius:12px; padding:32px; border:1px solid #2a2a2a;">
    <div style="text-align:center; margin-bottom:24px;">
      <span style="font-size:48px;">⚡</span>
      <h1 style="color:#f0b429; margin:8px 0 0; font-size:24px;">FundShot</h1>
    </div>
    <h2 style="color:#f0b429; margin:0 0 16px;">Plan Expiring Soon ⚠️</h2>
    <p style="color:#a0a0a0; margin:0 0 24px;">
      Hi <strong style="color:#e0e0e0;">@{username}</strong>,
      your <strong style="color:#f0b429;">{plan.capitalize()}</strong> plan expires on
      <strong style="color:#e0e0e0;">{expires_at}</strong> ({days_left} days left).
    </p>
    <p style="color:#a0a0a0; margin:0 0 24px; font-size:14px;">
      Renew now to keep auto-trading and all Pro features active without interruption.
    </p>
    <div style="text-align:center;">
      <a href="https://t.me/FundShot_bot" style="background:#f0b429; color:#000; text-decoration:none; padding:12px 28px; border-radius:8px; font-weight:bold; display:inline-block;">
        Renew with /upgrade →
      </a>
    </div>
    <hr style="border:none; border-top:1px solid #2a2a2a; margin:24px 0;">
    <p style="color:#555; font-size:12px; text-align:center; margin:0;">
      <a href="mailto:{SUPPORT_EMAIL}" style="color:#f0b429;">{SUPPORT_EMAIL}</a>
    </p>
  </div>
</body>
</html>
"""
    return _send(to_email, subject, html)

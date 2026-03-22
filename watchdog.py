#!/usr/bin/env python3
"""
watchdog.py — FundShot
Monitora l'API e manda alert Telegram se è down.
Eseguito ogni 5 minuti da systemd timer.
"""
import os, sys, time, json, urllib.request, urllib.error

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
CHAT_ID   = os.getenv("CHAT_ID", "")
API_URL   = "http://localhost:8080/api/status"
STATE_FILE= "/tmp/fs_watchdog_state.json"
TIMEOUT   = 8  # secondi

def send_telegram(msg: str):
    if not BOT_TOKEN or not CHAT_ID:
        print("No BOT_TOKEN/CHAT_ID — skip telegram")
        return
    url  = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    data = json.dumps({"chat_id": CHAT_ID, "text": msg, "parse_mode": "Markdown"}).encode()
    req  = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=10)
    except Exception as e:
        print(f"Telegram error: {e}")

def load_state() -> dict:
    try:
        return json.loads(open(STATE_FILE).read())
    except:
        return {"was_down": False, "down_since": 0, "notified": False}

def save_state(state: dict):
    open(STATE_FILE, "w").write(json.dumps(state))

def check_api() -> tuple[bool, str]:
    try:
        req = urllib.request.Request(API_URL)
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            data = json.loads(r.read())
            if data.get("ok") and data.get("status") == "operational":
                return True, f"uptime {data.get('uptime_hours',0):.1f}h"
            return False, f"status={data.get('status','?')}"
    except urllib.error.URLError as e:
        return False, f"URLError: {e.reason}"
    except Exception as e:
        return False, str(e)

def main():
    state = load_state()
    ok, reason = check_api()
    now = int(time.time())

    if not ok:
        if not state["was_down"]:
            # Primo rilevamento down
            state["was_down"]  = True
            state["down_since"] = now
            state["notified"]  = False
            save_state(state)
            print(f"DOWN detected: {reason}")

        elif not state["notified"]:
            # Down confermato (secondo check) — manda alert
            down_sec = now - state["down_since"]
            send_telegram(
                f"🔴 *FundShot BOT DOWN*\n\n"
                f"⏱ Down since: {down_sec//60} min ago\n"
                f"❌ Reason: `{reason}`\n\n"
                f"Check: `systemctl status fundshot-proxy`\n"
                f"`journalctl -u fundshot-proxy -n 20`"
            )
            state["notified"] = True
            save_state(state)
            print(f"Alert sent — down for {down_sec}s: {reason}")

    else:
        if state["was_down"]:
            # Era down, ora è tornato su
            down_sec = now - state.get("down_since", now)
            send_telegram(
                f"✅ *FundShot BOT RECOVERED*\n\n"
                f"⏱ Was down for: {down_sec//60} min\n"
                f"📊 {reason}"
            )
            print(f"RECOVERED after {down_sec}s")

        state = {"was_down": False, "down_since": 0, "notified": False}
        save_state(state)

if __name__ == "__main__":
    main()

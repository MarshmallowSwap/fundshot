"""
twitter_forwarder.py — Forwards @FundShot_app tweets to Telegram public channel.
Uses Nitter RSS (no API key needed, completely free).
Runs as a job every 10 minutes inside the bot scheduler.

Tweet tagged with #alert → also sent to ALL users via bot DM.
All other tweets → public channel only.
"""

import os, logging, hashlib
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

log = logging.getLogger(__name__)

TWITTER_HANDLE  = os.getenv("TWITTER_HANDLE", "FundShot_app")
NITTER_INSTANCES = [
    "https://nitter.privacydev.net",
    "https://nitter.poast.org",
    "https://nitter.cz",
    "https://nitter.1d4.us",
]
LAST_TWEET_KEY  = "twitter_last_id"


def _get_rss_url() -> str:
    return f"{NITTER_INSTANCES[0]}/{TWITTER_HANDLE}/rss"


async def _fetch_rss() -> list[dict]:
    """Fetch and parse RSS feed. Returns list of {id, text, url, is_retweet}."""
    import aiohttp
    tweets = []
    for base in NITTER_INSTANCES:
        url = f"{base}/{TWITTER_HANDLE}/rss"
        try:
            async with aiohttp.ClientSession() as s:
                async with s.get(url, timeout=aiohttp.ClientTimeout(total=8),
                                 headers={"User-Agent": "FundShot/1.0"}) as r:
                    if r.status != 200:
                        continue
                    xml_text = await r.text()
                    root = ET.fromstring(xml_text)
                    channel = root.find("channel")
                    if channel is None:
                        continue
                    for item in channel.findall("item"):
                        title = item.findtext("title", "")
                        link  = item.findtext("link", "")
                        desc  = item.findtext("description", "")
                        guid  = item.findtext("guid", link)
                        # Prendi il testo pulito dal title (Nitter format)
                        text = title.replace("R to @", "").strip()
                        is_rt = title.startswith("RT ")
                        tweets.append({
                            "id":        hashlib.md5(guid.encode()).hexdigest(),
                            "text":      text,
                            "url":       link,
                            "is_retweet": is_rt,
                            "raw_guid":  guid,
                        })
                    log.info("Twitter RSS: fetched %d tweets from %s", len(tweets), base)
                    return tweets
        except Exception as e:
            log.warning("Nitter %s failed: %s", base, e)
            continue
    return []


async def _get_last_id() -> str | None:
    """Reads last forwarded tweet ID from Supabase."""
    try:
        from db.supabase_client import get_client
        db = get_client()
        res = db.table("bot_state").select("value").eq("key", LAST_TWEET_KEY).execute()
        if res.data:
            return res.data[0]["value"]
    except Exception as e:
        log.warning("_get_last_id: %s", e)
    return None


async def _save_last_id(tweet_id: str):
    """Saves last forwarded tweet ID to Supabase."""
    try:
        from db.supabase_client import get_client
        db = get_client()
        db.table("bot_state").upsert({"key": LAST_TWEET_KEY, "value": tweet_id}).execute()
    except Exception as e:
        log.warning("_save_last_id: %s", e)


def _format_tweet(tweet: dict) -> str:
    """Formats tweet for Telegram."""
    text = tweet["text"]
    url  = tweet["url"]
    # Pulisci URL Nitter → Twitter
    tg_url = url.replace("nitter.privacydev.net", "x.com")\
                .replace("nitter.poast.org", "x.com")\
                .replace("nitter.cz", "x.com")\
                .replace("nitter.1d4.us", "x.com")
    msg = (
        f"🐦 *@FundShot\\_app*\n\n"
        f"{text}\n\n"
        f"[View on X]({tg_url})"
    )
    return msg


async def check_and_forward(bot) -> int:
    """
    Main job — checks for new tweets and forwards to Telegram.
    Returns number of tweets forwarded.
    """
    channel_id = os.getenv("CHANNEL_ID", "")
    if not channel_id:
        log.warning("CHANNEL_ID not set — Twitter forwarder skipped")
        return 0

    tweets = await _fetch_rss()
    if not tweets:
        return 0

    last_id = await _get_last_id()
    forwarded = 0

    # Process from oldest to newest
    for tweet in reversed(tweets):
        if tweet["id"] == last_id:
            break
        if tweet["is_retweet"]:
            continue  # Skip retweets

        msg = _format_tweet(tweet)

        # → Public channel (always)
        try:
            await bot.send_message(
                chat_id=channel_id,
                text=msg,
                parse_mode="Markdown",
                disable_web_page_preview=False,
            )
            log.info("Twitter → channel: %s", tweet["text"][:60])
        except Exception as e:
            log.error("Failed to forward tweet to channel: %s", e)
            continue

        # → All users DM (only if #alert in text)
        if "#alert" in tweet["text"].lower() or "#update" in tweet["text"].lower():
            try:
                from db.supabase_client import get_client
                db = get_client()
                res = db.table("users").select("chat_id").neq("chat_id", None).execute()
                users = res.data or []
                count = 0
                for u in users:
                    try:
                        await bot.send_message(
                            chat_id=u["chat_id"],
                            text=f"📢 *FundShot Update*\n\n{msg}",
                            parse_mode="Markdown",
                            disable_web_page_preview=True,
                        )
                        count += 1
                    except Exception:
                        pass
                log.info("Twitter #alert → %d users", count)
            except Exception as e:
                log.error("Failed to DM users: %s", e)

        forwarded += 1
        await _save_last_id(tweet["id"])

    return forwarded

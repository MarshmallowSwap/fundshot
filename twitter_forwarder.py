"""
twitter_forwarder.py — Forwards @FundShot_app tweets to Telegram public channel.
Uses multiple RSS sources (RSSHub + Nitter fallbacks). No API key needed.

All tweets → public channel
Tweets with #alert or #update → also DM to all users
"""

import os, logging, hashlib
import xml.etree.ElementTree as ET

log = logging.getLogger(__name__)

TWITTER_HANDLE = os.getenv("TWITTER_HANDLE", "FundShot_app")
LAST_TWEET_KEY = "twitter_last_id"

# RSS sources — tried in order until one works
RSS_SOURCES = [
    # RSSHub public instances (most reliable)
    f"https://rsshub.app/twitter/user/{TWITTER_HANDLE}",
    f"https://rsshub.rssforever.com/twitter/user/{TWITTER_HANDLE}",
    f"https://hub.slarker.me/twitter/user/{TWITTER_HANDLE}",
    # Nitter fallbacks
    f"https://nitter.poast.org/{TWITTER_HANDLE}/rss",
    f"https://nitter.cz/{TWITTER_HANDLE}/rss",
    f"https://nitter.privacydev.net/{TWITTER_HANDLE}/rss",
]


async def _fetch_rss() -> list[dict]:
    """Fetch and parse RSS from first working source."""
    import aiohttp, ssl
    ssl_ctx = ssl.create_default_context()
    ssl_ctx.check_hostname = False
    ssl_ctx.verify_mode = ssl.CERT_NONE

    for url in RSS_SOURCES:
        try:
            async with aiohttp.ClientSession() as s:
                async with s.get(
                    url,
                    timeout=aiohttp.ClientTimeout(total=10),
                    headers={"User-Agent": "Mozilla/5.0 FundShotBot/1.0"},
                    ssl=ssl_ctx,
                ) as r:
                    if r.status != 200:
                        continue
                    xml_text = await r.text()
                    if "<item>" not in xml_text:
                        continue

                    root = ET.fromstring(xml_text)
                    channel = root.find("channel")
                    if channel is None:
                        continue

                    tweets = []
                    for item in channel.findall("item"):
                        title = (item.findtext("title") or "").strip()
                        link  = (item.findtext("link") or "").strip()
                        guid  = (item.findtext("guid") or link).strip()
                        desc  = (item.findtext("description") or "").strip()

                        # Pulizia testo
                        text = title if title else desc
                        # Rimuovi tag HTML basilari
                        import re
                        text = re.sub(r'<[^>]+>', '', text).strip()
                        if not text:
                            continue

                        is_rt = text.startswith("RT @")

                        tweets.append({
                            "id":         hashlib.md5(guid.encode()).hexdigest(),
                            "text":       text,
                            "url":        link.replace("nitter.poast.org", "x.com")
                                            .replace("nitter.cz", "x.com")
                                            .replace("nitter.privacydev.net", "x.com"),
                            "is_retweet": is_rt,
                        })

                    if tweets:
                        log.info("Twitter RSS: %d tweets da %s", len(tweets), url)
                        return tweets

        except Exception as e:
            log.warning("RSS source %s: %s", url, e)
            continue

    log.warning("Twitter RSS: nessuna fonte disponibile")
    return []


async def _get_last_id() -> str | None:
    try:
        from db.supabase_client import get_client
        res = get_client().table("bot_state").select("value").eq("key", LAST_TWEET_KEY).execute()
        if res.data:
            return res.data[0]["value"]
    except Exception as e:
        log.warning("_get_last_id: %s", e)
    return None


async def _save_last_id(tweet_id: str):
    try:
        from db.supabase_client import get_client
        get_client().table("bot_state").upsert({"key": LAST_TWEET_KEY, "value": tweet_id}).execute()
    except Exception as e:
        log.warning("_save_last_id: %s", e)


def _format_tweet(tweet: dict) -> str:
    text = tweet["text"].replace("*", "\\*").replace("_", "\\_").replace("`", "\\`")
    url  = tweet["url"]
    # Normalizza URL verso x.com
    if "nitter" in url:
        import re
        url = re.sub(r'https?://[^/]+/', 'https://x.com/', url)
    return (
        f"🐦 *@FundShot\\_app*\n\n"
        f"{text}\n\n"
        f"[View on X ↗]({url})"
    )


async def check_and_forward(bot) -> int:
    channel_id = os.getenv("CHANNEL_ID", "")
    if not channel_id:
        log.warning("CHANNEL_ID not set — Twitter forwarder skipped")
        return 0

    tweets = await _fetch_rss()
    if not tweets:
        return 0

    last_id = await _get_last_id()
    forwarded = 0

    for tweet in reversed(tweets):
        if tweet["id"] == last_id:
            break
        if last_id is None and forwarded == 0:
            # First run — save current latest without forwarding (avoid flood)
            await _save_last_id(tweets[0]["id"])
            log.info("Twitter: first run — saved latest ID, forwarding starts next cycle")
            return 0
        if tweet["is_retweet"]:
            continue

        msg = _format_tweet(tweet)

        # → Canale pubblico
        try:
            await bot.send_message(
                chat_id=channel_id,
                text=msg,
                parse_mode="Markdown",
                disable_web_page_preview=False,
            )
            log.info("🐦 Tweet → channel: %s", tweet["text"][:60])
            forwarded += 1
        except Exception as e:
            log.error("Tweet → channel failed: %s", e)
            continue

        await _save_last_id(tweet["id"])

        # → DM a tutti gli utenti se #alert o #update
        text_lower = tweet["text"].lower()
        if "#alert" in text_lower or "#update" in text_lower:
            try:
                from db.supabase_client import get_client
                res = get_client().table("users").select("chat_id").neq("chat_id", None).execute()
                count = 0
                for u in (res.data or []):
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
                log.info("🐦 Tweet #alert → %d users", count)
            except Exception as e:
                log.error("Tweet DM users failed: %s", e)

    return forwarded

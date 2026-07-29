#!/usr/bin/env python3
"""
Mini's AI & Robotics Daily Digest
----------------------------------
1. Pulls entries published in the last LOOKBACK_HOURS from a curated list of RSS feeds.
2. De-duplicates against previously-seen entries (state.json, committed back to the repo).
3. Sends the raw items to Gemini to get a short, readable daily digest.
4. Renders a nice HTML page from the digest.
5. Sends that HTML page to your Telegram chat as a document, plus a short text summary.
"""

import os
import json
import time
import html
import logging
from datetime import datetime, timezone, timedelta

import feedparser
import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("digest_agent")

# ---------- Config ----------
FEEDS_FILE = "feeds.json"
STATE_FILE = "state.json"
OUTPUT_DIR = "output"
LOOKBACK_HOURS = int(os.environ.get("LOOKBACK_HOURS", "26"))  # a little over 24h to avoid gaps
MAX_ITEMS_PER_FEED = int(os.environ.get("MAX_ITEMS_PER_FEED", "5"))
MAX_TOTAL_ITEMS_FOR_GEMINI = int(os.environ.get("MAX_TOTAL_ITEMS_FOR_GEMINI", "100"))
MIN_TARGET_ITEMS = int(os.environ.get("MIN_TARGET_ITEMS", "25"))

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")


# ---------- Helpers ----------
def load_json(path, default):
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            log.warning("Could not read %s: %s", path, e)
    return default


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def entry_timestamp(entry):
    for key in ("published_parsed", "updated_parsed"):
        val = entry.get(key)
        if val:
            return datetime(*val[:6], tzinfo=timezone.utc)
    return None


def fetch_all_entries(feeds):
    cutoff = datetime.now(timezone.utc) - timedelta(hours=LOOKBACK_HOURS)
    all_items = []
    for feed in feeds:
        name, url = feed["name"], feed["url"]
        try:
            parsed = feedparser.parse(url)
            if parsed.bozo and not parsed.entries:
                log.warning("Feed failed to parse: %s (%s)", name, parsed.get("bozo_exception"))
                continue
        except Exception as e:
            log.warning("Error fetching %s: %s", name, e)
            continue

        count = 0
        for entry in parsed.entries:
            if count >= MAX_ITEMS_PER_FEED:
                break
            ts = entry_timestamp(entry)
            # If no timestamp, still include it (some feeds omit dates) but mark unknown
            if ts is not None and ts < cutoff:
                continue
            guid = entry.get("id") or entry.get("link")
            if not guid:
                continue
            all_items.append({
                "feed": name,
                "tier": feed.get("tier", "recommended"),
                "title": html.unescape(entry.get("title", "Untitled")).strip(),
                "link": entry.get("link", ""),
                "summary": html.unescape(entry.get("summary", ""))[:500],
                "guid": guid,
                "published": ts.isoformat() if ts else None,
            })
            count += 1
        log.info("Fetched %d recent item(s) from %s", count, name)
    return all_items


def dedupe_against_state(items, state):
    seen = set(state.get("seen_guids", []))
    fresh = [it for it in items if it["guid"] not in seen]
    return fresh


def update_state(state, items):
    seen = set(state.get("seen_guids", []))
    for it in items:
        seen.add(it["guid"])
    # keep state bounded
    seen_list = list(seen)[-3000:]
    state["seen_guids"] = seen_list
    state["last_run"] = datetime.now(timezone.utc).isoformat()
    return state


PLACEHOLDER_BLOCK


# ---------- Telegram ----------
def send_telegram_document(filepath, caption):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        log.error("Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID; skipping send.")
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendDocument"
    with open(filepath, "rb") as f:
        resp = requests.post(
            url,
            data={"chat_id": TELEGRAM_CHAT_ID, "caption": caption[:1024]},
            files={"document": (os.path.basename(filepath), f, "text/html")},
            timeout=30,
        )
    if resp.status_code != 200:
        log.error("Telegram sendDocument failed: %s %s", resp.status_code, resp.text)
        return False
    log.info("Sent digest HTML to Telegram.")
    return True


def send_telegram_message(text):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    resp = requests.post(
        url,
        data={"chat_id": TELEGRAM_CHAT_ID, "text": text[:4096], "disable_web_page_preview": True},
        timeout=30,
    )
    if resp.status_code != 200:
        log.error("Telegram sendMessage failed: %s %s", resp.status_code, resp.text)
        return False
    return True


# ---------- Main ----------
def main():
    feeds = load_json(FEEDS_FILE, [])
    if not feeds:
        log.error("No feeds configured in %s", FEEDS_FILE)
        return

    state = load_json(STATE_FILE, {"seen_guids": []})

    log.info("Fetching entries from %d feeds (lookback=%dh)...", len(feeds), LOOKBACK_HOURS)
    items = fetch_all_entries(feeds)
    log.info("Total fetched (pre-dedupe): %d", len(items))

    fresh_items = dedupe_against_state(items, state)
    log.info("New items after de-dupe: %d", len(fresh_items))

    date_str = datetime.now(timezone.utc).strftime("%A, %d %B %Y")

    if not fresh_items:
        log.info("No new items today. Sending a short 'nothing new' notice.")
        os.makedirs(OUTPUT_DIR, exist_ok=True)
        html_content = build_html(None, [], date_str)
        out_path = os.path.join(OUTPUT_DIR, "digest.html")
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(html_content)
        send_telegram_message(f"🤖 AI & Robotics Digest — {date_str}\n\nNo new stories since the last run.")
        save_json(STATE_FILE, update_state(state, items))
        return

    digest_md = summarize_with_gemini(fresh_items)

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    html_content = build_html(digest_md, fresh_items, date_str)
    out_path = os.path.join(OUTPUT_DIR, "digest.html")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html_content)
    log.info("Wrote %s", out_path)

    caption = f"🤖 Mini's AI & Robotics Daily Digest\n{date_str}\n{len(fresh_items)} new stories."
    send_telegram_document(out_path, caption)

    state = update_state(state, items)
    save_json(STATE_FILE, state)
    log.info("Done.")


if __name__ == "__main__":
    main()

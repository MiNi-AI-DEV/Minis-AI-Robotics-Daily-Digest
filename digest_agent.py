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
MAX_ITEMS_PER_FEED = int(os.environ.get("MAX_ITEMS_PER_FEED", "8"))
MAX_TOTAL_ITEMS_FOR_GEMINI = int(os.environ.get("MAX_TOTAL_ITEMS_FOR_GEMINI", "80"))

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


# ---------- Gemini summarization ----------
def summarize_with_gemini(items):
    if not GEMINI_API_KEY:
        log.warning("No GEMINI_API_KEY set, skipping AI summarization; using raw list instead.")
        return None
    if not items:
        return None

    try:
        import google.generativeai as genai
    except ImportError:
        log.warning("google-generativeai not installed, skipping summarization.")
        return None

    genai.configure(api_key=GEMINI_API_KEY)
    model = genai.GenerativeModel("gemini-2.0-flash")

    trimmed = items[:MAX_TOTAL_ITEMS_FOR_GEMINI]
    listing = "\n".join(
        f"- [{it['feed']}] {it['title']} :: {it['summary'][:200]} (LINK:{it['link']})"
        for it in trimmed
    )

    prompt = f"""You are curating a daily AI & Robotics news digest for a technical reader.
Below is a raw list of article titles, short summaries, and links gathered from RSS feeds in the last 24 hours.

Your job:
1. Group related items into 4-8 thematic sections (e.g. "Model Releases", "Research Papers", "Robotics", "Industry & Funding", "Policy & Safety").
2. For each section, write 2-5 concise bullet points. Each bullet should be one sentence summarizing what happened, in your own words (do not copy text verbatim).
3. At the end of each bullet, include the source name and link in this exact format: (Source: FEED_NAME | LINK)
4. Skip items that are duplicates or too minor to matter.
5. Write a 2-3 sentence "Top Story" intro at the very top highlighting the single most important development.
6. Output valid Markdown only, no preamble, no explanation of what you're doing.

Raw items:
{listing}
"""

    try:
        response = model.generate_content(prompt)
        return response.text
    except Exception as e:
        log.warning("Gemini summarization failed: %s", e)
        return None


# ---------- HTML rendering ----------
def markdown_to_html_fragment(md_text):
    """Very small markdown->HTML converter covering headings, bullets, bold, links."""
    import re
    lines = md_text.splitlines()
    html_lines = []
    in_list = False
    for line in lines:
        line = line.rstrip()
        if not line.strip():
            if in_list:
                html_lines.append("</ul>")
                in_list = False
            continue
        heading_match = re.match(r"^(#{1,6})\s+(.*)", line)
        bullet_match = re.match(r"^[-*]\s+(.*)", line)
        if heading_match:
            if in_list:
                html_lines.append("</ul>")
                in_list = False
            level = min(len(heading_match.group(1)) + 1, 4)  # shift down one level, cap h4
            html_lines.append(f"<h{level}>{inline_md(heading_match.group(2))}</h{level}>")
        elif bullet_match:
            if not in_list:
                html_lines.append("<ul>")
                in_list = True
            html_lines.append(f"<li>{inline_md(bullet_match.group(1))}</li>")
        else:
            if in_list:
                html_lines.append("</ul>")
                in_list = False
            html_lines.append(f"<p>{inline_md(line)}</p>")
    if in_list:
        html_lines.append("</ul>")
    return "\n".join(html_lines)


def inline_md(text):
    import re
    text = html.escape(text)
    text = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"\(Source:\s*(.*?)\s*\|\s*(https?://\S+)\)",
                  r'<span class="source">(Source: <a href="\2" target="_blank">\1</a>)</span>', text)
    text = re.sub(r"(?<!href=\")\b(https?://\S+)\b", r'<a href="\1" target="_blank">\1</a>', text)
    return text


def render_fallback_html(items):
    """If Gemini isn't available, just list raw items grouped by feed."""
    by_feed = {}
    for it in items:
        by_feed.setdefault(it["feed"], []).append(it)
    parts = []
    for feed, feed_items in by_feed.items():
        parts.append(f"<h3>{html.escape(feed)}</h3><ul>")
        for it in feed_items:
            parts.append(f'<li><a href="{it["link"]}" target="_blank">{html.escape(it["title"])}</a></li>')
        parts.append("</ul>")
    return "\n".join(parts)


def build_html(digest_markdown, items, date_str):
    if digest_markdown:
        body = markdown_to_html_fragment(digest_markdown)
    else:
        body = render_fallback_html(items)

    total_sources = len({it["feed"] for it in items})

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Mini's AI &amp; Robotics Daily Digest — {date_str}</title>
<style>
  :root {{
    --bg: #0f1117;
    --card: #171a23;
    --text: #e8eaf0;
    --muted: #9aa1b4;
    --accent: #6ea8fe;
    --accent2: #7ee0c3;
  }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0; padding: 0;
    background: var(--bg);
    color: var(--text);
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
    line-height: 1.6;
  }}
  .wrap {{ max-width: 760px; margin: 0 auto; padding: 32px 20px 60px; }}
  header {{ text-align: center; margin-bottom: 32px; }}
  header .kicker {{
    color: var(--accent2); text-transform: uppercase; letter-spacing: 2px;
    font-size: 12px; font-weight: 600;
  }}
  header h1 {{ font-size: 28px; margin: 8px 0 4px; }}
  header .date {{ color: var(--muted); font-size: 14px; }}
  .card {{
    background: var(--card); border-radius: 14px; padding: 24px 28px;
    margin-bottom: 20px; border: 1px solid #262a38;
  }}
  h2 {{ color: var(--accent); font-size: 20px; margin-top: 28px; }}
  h3 {{ color: var(--accent); font-size: 18px; margin-top: 22px; }}
  h4 {{ color: var(--accent2); font-size: 16px; }}
  ul {{ padding-left: 20px; }}
  li {{ margin-bottom: 10px; }}
  a {{ color: var(--accent2); text-decoration: none; }}
  a:hover {{ text-decoration: underline; }}
  .source {{ color: var(--muted); font-size: 13px; }}
  footer {{ text-align: center; color: var(--muted); font-size: 12px; margin-top: 30px; }}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <div class="kicker">Mini's AI Robotics Daily Digest</div>
    <h1>🤖 AI &amp; Robotics — Today's Briefing</h1>
    <div class="date">{date_str} · {len(items)} stories from {total_sources} sources</div>
  </header>
  <div class="card">
    {body}
  </div>
  <footer>Generated automatically via GitHub Actions + Gemini · RSS sources curated for AI &amp; Robotics</footer>
</div>
</body>
</html>
"""


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

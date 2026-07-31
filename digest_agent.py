#!/usr/bin/env python3
"""
Mini's AI & Robotics Daily Digest
----------------------------------
1. Pulls entries from a curated list of RSS feeds (up to 5 per site to start).
2. Permanently de-dupes against everything ever sent before (state.json) —
   an article is NEVER sent twice, not this week, not next year.
3. If we don't have at least MIN_TOTAL_ITEMS fresh items, widens the lookback
   window and per-feed cap and tries again, so you always get a full digest.
4. Sends the fresh items to Gemini, which returns a clean JSON list of
   {title, summary (5-6 lines), source, link} — one card per news item.
5. Renders a polished HTML+CSS+JS page (with a live search/filter box).
6. Sends that HTML page to your Telegram chat as a document.
"""

import os
import re
import json
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

MIN_TOTAL_ITEMS = int(os.environ.get("MIN_TOTAL_ITEMS", "25"))
# Escalating attempts: (lookback_hours, max_items_per_feed)
FETCH_ATTEMPTS = [(26, 5), (48, 8), (72, 12), (168, 15)]

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


def strip_html(text):
    text = re.sub(r"<[^>]+>", " ", text or "")
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def entry_timestamp(entry):
    for key in ("published_parsed", "updated_parsed"):
        val = entry.get(key)
        if val:
            return datetime(*val[:6], tzinfo=timezone.utc)
    return None


def fetch_all_entries(feeds, lookback_hours, max_per_feed):
    cutoff = datetime.now(timezone.utc) - timedelta(hours=lookback_hours)
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
            if count >= max_per_feed:
                break
            ts = entry_timestamp(entry)
            if ts is not None and ts < cutoff:
                continue
            guid = entry.get("id") or entry.get("link")
            if not guid:
                continue
            all_items.append({
                "feed": name,
                "tier": feed.get("tier", "recommended"),
                "title": strip_html(entry.get("title", "Untitled")),
                "link": entry.get("link", ""),
                "raw_summary": strip_html(entry.get("summary", ""))[:1200],
                "guid": guid,
                "published": ts.isoformat() if ts else None,
            })
            count += 1
        log.info("Fetched %d recent item(s) from %s", count, name)
    return all_items


def dedupe_against_state(items, state):
    seen = set(state.get("seen_guids", []))
    fresh, unique_guids = [], set()
    for it in items:
        if it["guid"] in seen or it["guid"] in unique_guids:
            continue
        unique_guids.add(it["guid"])
        fresh.append(it)
    return fresh


def gather_fresh_items(feeds, state):
    """Escalate lookback window / per-feed cap until we hit MIN_TOTAL_ITEMS or run out of attempts."""
    last_all_items = []
    for lookback_hours, max_per_feed in FETCH_ATTEMPTS:
        log.info("Attempt: lookback=%dh, max_per_feed=%d", lookback_hours, max_per_feed)
        all_items = fetch_all_entries(feeds, lookback_hours, max_per_feed)
        last_all_items = all_items
        fresh = dedupe_against_state(all_items, state)
        log.info("-> %d fresh items after de-dupe", len(fresh))
        if len(fresh) >= MIN_TOTAL_ITEMS:
            return fresh, all_items
    # Ran out of attempts; return whatever we have (may be < MIN_TOTAL_ITEMS on a very quiet day)
    return dedupe_against_state(last_all_items, state), last_all_items


def update_state(state, items):
    seen = set(state.get("seen_guids", []))
    for it in items:
        seen.add(it["guid"])
    state["seen_guids"] = list(seen)[-8000:]  # generous cap; guids never expire in practice
    state["last_run"] = datetime.now(timezone.utc).isoformat()
    return state


# ---------- Gemini summarization ----------
def summarize_with_gemini(items):
    """Returns a list of dicts: {title, summary, source, link}. Falls back to None on failure."""
    if not GEMINI_API_KEY:
        log.warning("No GEMINI_API_KEY set; using raw feed summaries instead of Gemini.")
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

    BATCH_SIZE = 20
    results = []
    for i in range(0, len(items), BATCH_SIZE):
        batch = items[i:i + BATCH_SIZE]
        listing = "\n\n".join(
            f"ITEM {j}\nSOURCE: {it['feed']}\nTITLE: {it['title']}\nLINK: {it['link']}\nRAW: {it['raw_summary'][:600]}"
            for j, it in enumerate(batch)
        )
        prompt = f"""You are curating a daily AI & Robotics news digest for a technical reader.
For EACH item below, write a clear title and a 5-6 line (5-6 sentence) plain-English summary
explaining what happened, why it matters, and any key details/numbers. Do not copy the raw
text verbatim — rewrite it in your own words. Keep the original source name and link unchanged.

Return ONLY a valid JSON array (no markdown fences, no commentary), one object per item, in this exact shape:
[{{"title": "...", "summary": "...", "source": "...", "link": "..."}}, ...]

Items:
{listing}
"""
        try:
            response = model.generate_content(prompt)
            text = response.text.strip()
            text = re.sub(r"^```(json)?", "", text.strip())
            text = re.sub(r"```$", "", text.strip())
            parsed = json.loads(text)
            for obj in parsed:
                if all(k in obj for k in ("title", "summary", "source", "link")):
                    results.append(obj)
        except Exception as e:
            log.warning("Gemini summarization failed for batch starting at %d: %s", i, e)
            continue

    return results if results else None


def fallback_summaries(items):
    """No-Gemini fallback: use the raw RSS summary, trimmed to a readable length."""
    out = []
    for it in items:
        summary = it["raw_summary"] or "No description provided by the source."
        if len(summary) > 500:
            summary = summary[:500].rsplit(" ", 1)[0] + "…"
        out.append({
            "title": it["title"],
            "summary": summary,
            "source": it["feed"],
            "link": it["link"],
        })
    return out


# ---------- HTML rendering ----------
def render_cards_html(digest_items):
    cards = []
    for it in digest_items:
        title = html.escape(it.get("title", "Untitled"))
        summary = html.escape(it.get("summary", ""))
        source = html.escape(it.get("source", "Unknown source"))
        link = html.escape(it.get("link", "#"))
        cards.append(f"""
        <article class="card" data-search="{title.lower()} {summary.lower()} {source.lower()}">
          <div class="card-top">
            <span class="badge">{source}</span>
          </div>
          <h2 class="card-title">{title}</h2>
          <p class="card-summary">{summary}</p>
          <a class="card-link" href="{link}" target="_blank" rel="noopener">Read full article &rarr;</a>
        </article>""")
    return "\n".join(cards)


def build_html(digest_items, date_str, sources_count):
    cards_html = render_cards_html(digest_items)
    total = len(digest_items)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Mini's AI &amp; Robotics Daily Digest — {date_str}</title>
<style>
  :root {{
    --bg: #0b0d13;
    --bg-glow: radial-gradient(circle at 20% -10%, #1c2540 0%, #0b0d13 45%);
    --card: #141824;
    --card-border: #232a3d;
    --text: #eef0f6;
    --muted: #9aa1b8;
    --accent: #7aa2ff;
    --accent2: #6bf0c2;
    --badge-bg: #1d2a44;
  }}
  * {{ box-sizing: border-box; }}
  html {{ scroll-behavior: smooth; }}
  body {{
    margin: 0;
    background: var(--bg-glow), var(--bg);
    color: var(--text);
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
    line-height: 1.6;
    min-height: 100vh;
  }}
  .wrap {{ max-width: 900px; margin: 0 auto; padding: 40px 20px 70px; }}
  header {{ text-align: center; margin-bottom: 28px; }}
  header .kicker {{
    color: var(--accent2); text-transform: uppercase; letter-spacing: 3px;
    font-size: 12px; font-weight: 700;
  }}
  header h1 {{ font-size: 30px; margin: 10px 0 6px; }}
  header .date {{ color: var(--muted); font-size: 14px; }}
  .searchbar {{ margin: 28px auto 8px; max-width: 560px; position: relative; }}
  .searchbar input {{
    width: 100%; padding: 13px 16px 13px 42px; border-radius: 999px;
    border: 1px solid var(--card-border); background: var(--card); color: var(--text);
    font-size: 15px; outline: none; transition: border-color .15s ease;
  }}
  .searchbar input:focus {{ border-color: var(--accent); }}
  .searchbar::before {{
    content: "\\1F50D"; position: absolute; left: 16px; top: 50%; transform: translateY(-50%);
    font-size: 14px; opacity: .7;
  }}
  .count-line {{ text-align: center; color: var(--muted); font-size: 13px; margin-bottom: 26px; }}
  .grid {{ display: grid; grid-template-columns: 1fr; gap: 16px; }}
  @media (min-width: 680px) {{ .grid {{ grid-template-columns: 1fr 1fr; }} }}
  .card {{
    background: var(--card); border: 1px solid var(--card-border); border-radius: 16px;
    padding: 20px 22px; display: flex; flex-direction: column; gap: 10px;
    transition: transform .15s ease, border-color .15s ease;
  }}
  .card:hover {{ transform: translateY(-3px); border-color: var(--accent); }}
  .card-top {{ display: flex; justify-content: space-between; align-items: center; }}
  .badge {{
    background: var(--badge-bg); color: var(--accent2); font-size: 11px; font-weight: 700;
    padding: 4px 10px; border-radius: 999px; letter-spacing: .3px;
  }}
  .card-title {{ font-size: 18px; margin: 0; color: var(--text); }}
  .card-summary {{ font-size: 14.5px; color: var(--muted); margin: 0; flex-grow: 1; }}
  .card-link {{ font-size: 13.5px; font-weight: 600; color: var(--accent); text-decoration: none; margin-top: 4px; }}
  .card-link:hover {{ text-decoration: underline; }}
  .empty-state {{ text-align: center; color: var(--muted); padding: 40px 0; display: none; }}
  footer {{ text-align: center; color: var(--muted); font-size: 12px; margin-top: 40px; }}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <div class="kicker">Mini's AI Robotics Daily Digest</div>
    <h1>AI &amp; Robotics — Today's Briefing</h1>
    <div class="date">{date_str}</div>
  </header>

  <div class="searchbar">
    <input type="text" id="searchInput" placeholder="Search today's stories…" oninput="filterCards()">
  </div>
  <div class="count-line" id="countLine">{total} stories from {sources_count} sources</div>

  <div class="grid" id="grid">
    {cards_html}
  </div>
  <div class="empty-state" id="emptyState">No stories match your search.</div>

  <footer>Generated automatically via GitHub Actions + Gemini &middot; RSS sources curated for AI &amp; Robotics</footer>
</div>

<script>
  function filterCards() {{
    const q = document.getElementById('searchInput').value.trim().toLowerCase();
    const cards = document.querySelectorAll('.card');
    let visible = 0;
    cards.forEach(card => {{
      const match = card.dataset.search.includes(q);
      card.style.display = match ? 'flex' : 'none';
      if (match) visible++;
    }});
    document.getElementById('emptyState').style.display = visible === 0 ? 'block' : 'none';
    document.getElementById('countLine').textContent =
      q ? (visible + ' matching stories') : ('{total} stories from {sources_count} sources');
  }}
</script>
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
    date_str = datetime.now(timezone.utc).strftime("%A, %d %B %Y")

    fresh_items, all_items_seen_this_run = gather_fresh_items(feeds, state)
    log.info("Final fresh item count: %d", len(fresh_items))

    os.makedirs(OUTPUT_DIR, exist_ok=True)

    if not fresh_items:
        html_content = build_html([], date_str, 0)
        out_path = os.path.join(OUTPUT_DIR, "digest.html")
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(html_content)
        send_telegram_message(f"AI & Robotics Digest — {date_str}\n\nNo new stories since the last run.")
        save_json(STATE_FILE, update_state(state, all_items_seen_this_run))
        return

    digest_items = summarize_with_gemini(fresh_items)
    if not digest_items:
        digest_items = fallback_summaries(fresh_items)

    sources_count = len({d.get("source", "") for d in digest_items})
    html_content = build_html(digest_items, date_str, sources_count)
    out_path = os.path.join(OUTPUT_DIR, "digest.html")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html_content)
    log.info("Wrote %s with %d stories", out_path, len(digest_items))

    caption = f"Mini's AI & Robotics Daily Digest\n{date_str}\n{len(digest_items)} new stories."
    send_telegram_document(out_path, caption)

    state = update_state(state, all_items_seen_this_run)
    save_json(STATE_FILE, state)
    log.info("Done.")


if __name__ == "__main__":
    main()

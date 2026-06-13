#!/usr/bin/env python3
"""
HR News Bot — sends fresh HR / AI-in-HR headlines to Telegram.

Pulls from Google News RSS (free, no API key), removes duplicates, and posts
only items it has not sent before.

QUICK START
  1. Create a bot: in Telegram open @BotFather -> /newbot -> copy the token.
  2. Get your chat id: open @userinfobot in Telegram -> Start -> copy the "Id".
  3. Open YOUR new bot and press Start (required, or it cannot message you).
  4. Install deps:        pip3 install feedparser requests
  5. Preview (no sending): python3 hr_news_bot.py --dry-run
  6. Provide credentials and run for real (see run.sh / cron below):
         export TELEGRAM_BOT_TOKEN="123456789:AA..."
         export TELEGRAM_CHAT_ID="123456789"
         python3 hr_news_bot.py

Never hardcode the token in this file or share it with anyone.
"""

import os
import sys
import json
import time
import html
import urllib.parse
from pathlib import Path
from datetime import datetime, timezone

import requests
import feedparser

# === EDIT YOUR TOPICS HERE ===================================================

# English queries -> global, English-language news. Comment out (#) any you
# don't want, add your own freely.
QUERIES_EN = [
    # AI & tech in HR
    '"human resources" (AI OR "artificial intelligence")',
    '"HR tech" OR "HR technology" OR HRIS',
    '"AI recruiting" OR "AI hiring" OR "AI in recruitment"',
    '"generative AI" (workplace OR HR)',
    '("AI agents" OR "agentic AI") workplace',
    '"people analytics" OR "workforce analytics"',
    # Future of work
    '"future of work"',
    '"hybrid work" OR "remote work" OR "return to office"',
    '"four-day work week" OR "4-day work week"',
    '"workplace trends"',
    # Talent & hiring
    '"talent acquisition" OR "talent management"',
    '"skills-based hiring" OR "skills-based organization"',
    '"hiring trends" OR "recruiting trends"',
    # Employee experience
    '"employee experience"',
    '"employee engagement"',
    '"employee wellbeing" OR "employee burnout"',
    # Learning & skills
    'reskilling OR upskilling',
    '"learning and development" OR "L&D"',
    '"skills gap"',
    # Pay, performance, policy
    '"pay transparency"',
    '"performance management"',
    '"total rewards" OR "compensation trends"',
    # Strategy & leadership
    'CHRO OR "chief people officer"',
    '"HR transformation" OR "human capital"',
    'DEI OR "diversity equity inclusion"',
]

# Russian queries -> RuNet news. Empty by default. Uncomment to switch on.
QUERIES_RU = [
    # '"управление персоналом" (нейросети OR ИИ)',
    # '"будущее работы" OR "рынок труда"',
    # '"подбор персонала" тренды',
    # '"корпоративная культура" OR "вовлечённость персонала"',
]

# How many items to post per run (caps the very first run). None = no limit.
MAX_PER_RUN = 25

# Skip anything older than this many hours. None = no age filter.
MAX_AGE_HOURS = 72

# =============================================================================

EN_LOCALE = {"hl": "en-US", "gl": "US", "ceid": "US:en"}
RU_LOCALE = {"hl": "ru", "gl": "RU", "ceid": "RU:ru"}

STATE_FILE = Path(__file__).with_name("seen.json")
RSS_BASE = "https://news.google.com/rss/search"
TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"
HEADERS = {"User-Agent": "Mozilla/5.0 (HRNewsBot)"}


def build_feed_url(query: str, locale: dict) -> str:
    params = {"q": query, "hl": locale["hl"], "gl": locale["gl"], "ceid": locale["ceid"]}
    return RSS_BASE + "?" + urllib.parse.urlencode(params)


def load_seen() -> list:
    if STATE_FILE.exists():
        try:
            data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            return list(data) if isinstance(data, list) else []
        except Exception:
            return []
    return []


def save_seen(seen_list: list) -> None:
    # Keep only the most recent ~5000 links so the file never grows forever.
    STATE_FILE.write_text(
        json.dumps(seen_list[-5000:], ensure_ascii=False), encoding="utf-8"
    )


def entry_timestamp(entry) -> float:
    pp = getattr(entry, "published_parsed", None)
    if not pp:
        return 0.0
    return time.mktime(pp)


def age_ok(ts: float) -> bool:
    if MAX_AGE_HOURS is None or ts == 0.0:
        return True
    pub = datetime.fromtimestamp(ts, tz=timezone.utc)
    age_h = (datetime.now(timezone.utc) - pub).total_seconds() / 3600
    return age_h <= MAX_AGE_HOURS


def fetch_feed(url: str):
    resp = requests.get(url, headers=HEADERS, timeout=20)
    resp.raise_for_status()
    return feedparser.parse(resp.content)


def collect_new_items(seen: set):
    items, seen_now = [], set()
    for queries, locale in ((QUERIES_EN, EN_LOCALE), (QUERIES_RU, RU_LOCALE)):
        for query in queries:
            url = build_feed_url(query, locale)
            try:
                feed = fetch_feed(url)
            except Exception as exc:
                print(f"[warn] query failed: {query!r} -> {exc}", file=sys.stderr)
                continue
            for entry in feed.entries:
                link = getattr(entry, "link", "").strip()
                title = getattr(entry, "title", "").strip()
                if not link or not title:
                    continue
                if link in seen or link in seen_now:
                    continue
                ts = entry_timestamp(entry)
                if not age_ok(ts):
                    continue
                seen_now.add(link)
                source = ""
                src = getattr(entry, "source", None)
                if src is not None and getattr(src, "title", None):
                    source = src.title
                items.append({"title": title, "link": link, "source": source, "ts": ts})
            time.sleep(0.4)  # be gentle with the feed
    items.sort(key=lambda x: x["ts"], reverse=True)  # newest first
    return items


def format_messages(items: list) -> list:
    lines = []
    for it in items:
        title = html.escape(it["title"])
        src = f" — {html.escape(it['source'])}" if it["source"] else ""
        link = html.escape(it["link"])
        lines.append(f'• <a href="{link}">{title}</a>{src}')
    header = f"🗞 HR news — {len(items)} new ({datetime.now().strftime('%d %b %H:%M')})"
    chunks, current, length = [], [header], len(header) + 1
    for line in lines:
        if length + len(line) + 1 > 3800:  # Telegram limit is 4096 chars
            chunks.append("\n".join(current))
            current, length = [], 0
        current.append(line)
        length += len(line) + 1
    if current:
        chunks.append("\n".join(current))
    return chunks


def send_to_telegram(token: str, chat_id: str, text: str) -> bool:
    resp = requests.post(
        TELEGRAM_API.format(token=token),
        data={
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        },
        timeout=30,
    )
    if not resp.ok:
        print(f"[error] Telegram: {resp.status_code} {resp.text}", file=sys.stderr)
    return resp.ok


def main() -> None:
    dry_run = "--dry-run" in sys.argv
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not dry_run and (not token or not chat_id):
        sys.exit(
            "Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID "
            "(or run with --dry-run to preview without sending)."
        )

    seen_list = load_seen()
    seen = set(seen_list)
    items = collect_new_items(seen)

    if MAX_PER_RUN:
        items = items[:MAX_PER_RUN]

    if not items:
        print("No new items.")
        return

    messages = format_messages(items)

    if dry_run:
        for msg in messages:
            print(msg)
            print("-" * 60)
        print(f"[dry-run] {len(items)} item(s) found, nothing sent, state unchanged.")
        return

    ok = True
    for msg in messages:
        if not send_to_telegram(token, chat_id, msg):
            ok = False
            break
        time.sleep(1)

    if ok:  # only remember items once they were actually delivered
        for it in items:
            seen_list.append(it["link"])
        save_seen(seen_list)
        print(f"Sent {len(items)} item(s) in {len(messages)} message(s).")
    else:
        print("Send failed; state not updated, will retry next run.", file=sys.stderr)


if __name__ == "__main__":
    main()

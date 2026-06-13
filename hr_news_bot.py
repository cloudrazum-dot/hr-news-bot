#!/usr/bin/env python3
"""
HR News Bot — sends fresh HR / AI-in-HR news WITH short summaries to Telegram.

Uses the GNews API (free tier), which returns a title, a 1-2 sentence summary,
the source, and a link for each article. De-duplicates and posts only new items.

QUICK START
  1. Free API key at https://gnews.io  (sign up -> copy your API key).
  2. In GitHub add three secrets:
        GNEWS_API_KEY, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
  3. Locally for testing:
        export GNEWS_API_KEY="..."
        export TELEGRAM_BOT_TOKEN="..."
        export TELEGRAM_CHAT_ID="..."
        python3 hr_news_bot.py --dry-run      # preview, nothing sent

Never hardcode the keys here or share them with anyone.
"""

import os
import sys
import json
import time
import html
from pathlib import Path
from datetime import datetime, timezone

import requests

# === EDIT YOUR TOPICS HERE ===================================================
# Each query = one API request per run, and the free GNews plan allows ~100
# requests/day. This set is ~32 queries; at 2 runs/day that's ~64 requests/day
# (under 100, with room for manual test runs). Syntax: "exact phrase", AND, OR, NOT, ( ).

QUERIES_EN = [
    # AI in / around HR
    '(AI OR "artificial intelligence" OR "generative AI") AND ("human resources" OR HR OR recruiting OR hiring)',
    '("AI agents" OR "agentic AI" OR "AI automation") AND (workplace OR employees OR jobs OR workforce)',
    '("AI Act" OR "algorithmic management" OR "AI bias" OR "responsible AI") AND (hiring OR HR OR workplace)',
    # HR tech & future of work
    '"HR technology" OR "HR tech" OR "HR software" OR "HRIS" OR "HR automation"',
    '"future of work" OR "workforce of the future" OR "human-AI collaboration"',
    '"people analytics" OR "workforce analytics" OR "data-driven HR"',
    # Talent
    '"talent acquisition" OR "skills-based hiring" OR "candidate experience" OR "employer brand"',
    '"talent management" OR "succession planning" OR "leadership pipeline" OR "high-potential" OR "talent review"',
    '"internal mobility" OR "talent marketplace" OR "skills-based organization" OR "skills taxonomy"',
    'reskilling OR upskilling OR "skills gap" OR "skills shortage"',
    # Develop, manage, reward
    '"learning and development" OR "corporate training" OR "leadership development"',
    '"performance management" OR "performance review" OR "continuous feedback"',
    '"pay transparency" OR "compensation strategy" OR "total rewards" OR "pay equity"',
    '"employee wellbeing" OR "workplace burnout" OR "employee mental health"',
    '("diversity equity inclusion" OR DEI OR "workplace inclusion") NOT politics',
    '"return to office" OR "hybrid work" OR "four-day work week" OR "remote work"',
    # Strategy, market, managers, experience
    'CHRO OR "chief people officer" OR "HR transformation" OR "people strategy"',
    '"labor market" OR "talent shortage" OR "labor shortage" OR layoffs OR "quiet quitting"',
    '"manager effectiveness" OR "frontline managers" OR "manager enablement"',
    '"employee experience" OR "employee engagement" OR "employee retention"',
]

QUERIES_RU = [
    '("искусственный интеллект" OR нейросети OR ИИ) AND ("управление персоналом" OR HR OR подбор OR рекрутинг)',
    '"рынок труда" OR "дефицит кадров" OR "кадровый голод" OR "массовые сокращения"',
    '"подбор персонала" OR рекрутинг OR "HR-технологии" OR "бренд работодателя"',
    '"вовлечённость персонала" OR "корпоративная культура" OR "удержание персонала"',
    '"обучение персонала" OR "развитие персонала" OR "корпоративное обучение"',
    '"кадровый резерв" OR "управление талантами" OR "развитие талантов" OR преемственность',
    '"оплата труда" OR "система мотивации" OR "оценка персонала" OR "управление эффективностью"',
    '"выгорание сотрудников" OR "благополучие сотрудников" OR "психологическое здоровье"',
    '"удалённая работа" OR "гибридный формат" OR "возвращение в офис"',
    '"HR-аналитика" OR "аналитика персонала" OR "автоматизация HR" OR "цифровизация HR"',
    '"HR-директор" OR "директор по персоналу" OR "человеческий капитал"',
    '"развитие лидеров" OR "лидерские программы" OR "управленческие компетенции"',
]

# Max items to post per run. With many topics and only 2 runs/day, a higher
# cap means fewer fresh items get pushed to the next run. None = no limit.
MAX_PER_RUN = 40
# Skip anything older than this many hours. None = no age filter.
MAX_AGE_HOURS = 72
# Articles to request per query (GNews free tier max is 10).
ARTICLES_PER_QUERY = 10
# =============================================================================

STATE_FILE = Path(__file__).with_name("seen.json")
GNEWS_URL = "https://gnews.io/api/v4/search"
TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"


def load_seen() -> list:
    if STATE_FILE.exists():
        try:
            data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            return list(data) if isinstance(data, list) else []
        except Exception:
            return []
    return []


def save_seen(seen_list: list) -> None:
    STATE_FILE.write_text(
        json.dumps(seen_list[-5000:], ensure_ascii=False), encoding="utf-8"
    )


def parse_dt(published_at: str) -> float:
    # GNews gives ISO timestamps like "2026-06-13T09:00:00Z"
    if not published_at:
        return 0.0
    try:
        return datetime.fromisoformat(published_at.replace("Z", "+00:00")).timestamp()
    except Exception:
        return 0.0


def age_ok(ts: float) -> bool:
    if MAX_AGE_HOURS is None or ts == 0.0:
        return True
    pub = datetime.fromtimestamp(ts, tz=timezone.utc)
    age_h = (datetime.now(timezone.utc) - pub).total_seconds() / 3600
    return age_h <= MAX_AGE_HOURS


def fetch_query(query: str, lang: str, apikey: str) -> list:
    params = {
        "q": query,
        "lang": lang,
        "max": ARTICLES_PER_QUERY,
        "sortby": "publishedAt",
        "in": "title,description",  # match only in title + summary -> less noise
        "apikey": apikey,
    }
    resp = requests.get(GNEWS_URL, params=params, timeout=20)
    if resp.status_code == 429:
        raise RuntimeError("GNews daily limit reached (429)")
    if resp.status_code in (401, 403):
        raise RuntimeError(f"GNews rejected the key ({resp.status_code}) - check GNEWS_API_KEY")
    resp.raise_for_status()
    return resp.json().get("articles", [])


def collect_new_items(seen: set, apikey: str) -> list:
    items, seen_now = [], set()
    for queries, lang in ((QUERIES_EN, "en"), (QUERIES_RU, "ru")):
        for query in queries:
            try:
                articles = fetch_query(query, lang, apikey)
            except RuntimeError as exc:
                # Key/quota problem: stop early, keep what we already gathered.
                print(f"[stop] {exc}", file=sys.stderr)
                return items
            except Exception as exc:
                print(f"[warn] query failed: {query!r} -> {exc}", file=sys.stderr)
                continue
            for art in articles:
                url = (art.get("url") or "").strip()
                title = (art.get("title") or "").strip()
                if not url or not title:
                    continue
                if url in seen or url in seen_now:
                    continue
                ts = parse_dt(art.get("publishedAt", ""))
                if not age_ok(ts):
                    continue
                seen_now.add(url)
                items.append({
                    "title": title,
                    "summary": (art.get("description") or "").strip(),
                    "url": url,
                    "source": (art.get("source") or {}).get("name", ""),
                    "ts": ts,
                })
            time.sleep(1.1)  # GNews free tier allows ~1 request/second
    items.sort(key=lambda x: x["ts"], reverse=True)  # newest first
    return items


def format_messages(items: list) -> list:
    blocks = []
    for it in items:
        title = html.escape(it["title"])
        url = html.escape(it["url"])
        block = f'<a href="{url}"><b>{title}</b></a>'
        summary = it["summary"]
        if summary:
            if len(summary) > 300:
                summary = summary[:297].rstrip() + "…"
            block += "\n" + html.escape(summary)
        if it["source"]:
            block += f'\n<i>{html.escape(it["source"])}</i>'
        blocks.append(block)

    header = f"🗞 HR news — {len(items)} new ({datetime.now().strftime('%d %b %H:%M')})"
    chunks, current, length = [], [header], len(header)
    for block in blocks:
        if length + len(block) + 2 > 3900:  # Telegram hard limit is 4096
            chunks.append("\n\n".join(current))
            current, length = [], 0
        current.append(block)
        length += len(block) + 2
    if current:
        chunks.append("\n\n".join(current))
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
    apikey = os.environ.get("GNEWS_API_KEY")
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")

    if not apikey:
        sys.exit("Set GNEWS_API_KEY (free key at https://gnews.io).")
    if not dry_run and (not token or not chat_id):
        sys.exit("Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID (or use --dry-run).")

    seen_list = load_seen()
    seen = set(seen_list)
    items = collect_new_items(seen, apikey)

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

    if ok:
        for it in items:
            seen_list.append(it["url"])
        save_seen(seen_list)
        print(f"Sent {len(items)} item(s) in {len(messages)} message(s).")
    else:
        print("Send failed; state not updated, will retry next run.", file=sys.stderr)


if __name__ == "__main__":
    main()

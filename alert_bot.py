#!/usr/bin/env python3
"""
London alert bot.

Polls a handful of feeds, scores each new item for "does this matter to
someone living in London", and pushes to your phone via ntfy:
  - score >= URGENT_THRESHOLD  -> immediate max-priority push
  - score >= DIGEST_THRESHOLD  -> saved for the daily digest
  - everything else            -> ignored

The daily digest doubles as a heartbeat: if you stop getting it,
the bot is broken. If every feed fails several runs in a row, it
also pushes a warning.

Runs anywhere Python runs. Designed for GitHub Actions on a cron,
so it keeps going if your laptop is off or your flat loses power.
Claude scoring is optional; without an API key it falls back to
rules + keywords.
"""
import datetime as dt
import hashlib
import json
import os
import re
import sys

import feedparser
import requests

# ---------- config ----------
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "")
NTFY_SERVER = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-haiku-5-5")
STATE_FILE = os.environ.get("STATE_FILE", "state.json")
DIGEST_HOUR_UTC = int(os.environ.get("DIGEST_HOUR_UTC", "8"))
URGENT_THRESHOLD = int(os.environ.get("URGENT_THRESHOLD", "8"))
DIGEST_THRESHOLD = int(os.environ.get("DIGEST_THRESHOLD", "5"))
FAIL_ALERT_AFTER = int(os.environ.get("FAIL_ALERT_AFTER", "3"))
DRY_RUN = os.environ.get("DRY_RUN") == "1"

FEEDS = {
    "GDACS": "https://www.gdacs.org/xml/rss.xml",
    "Met Office London & SE": "https://www.metoffice.gov.uk/public/data/PWSCache/WarningsRSS/Region/se",
    "BBC UK": "https://feeds.bbci.co.uk/news/uk/rss.xml",
    "BBC World": "https://feeds.bbci.co.uk/news/world/rss.xml",
    "GOV.UK": "https://www.gov.uk/search/news-and-communications.atom",
}

CRITICAL = [
    r"nuclear", r"missile", r"martial law", r"state of emergency", r"invasion",
    r"declar\w* war", r"article 5", r"mobilis", r"mobiliz", r"terror", r"explosion",
    r"chemical attack", r"radiation", r"evacuat", r"curfew", r"blackout",
    r"power cut", r"grid (failure|collapse|outage)", r"national grid",
    r"cyber ?attack", r"internet outage", r"fuel shortage", r"water shortage",
    r"food shortage", r"bank run", r"cobra", r"emergency alert", r"red warning",
    r"major incident",
]
LOCAL = [r"\blondon\b", r"\buk\b", r"britain", r"british", r"england", r"south[- ]east"]

# ---------- state ----------
def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"seen": {}, "digest": [], "last_digest": None, "fail_streak": 0, "bootstrapped": False}


def save_state(state):
    cutoff = (dt.datetime.utcnow() - dt.timedelta(days=7)).isoformat()
    state["seen"] = {k: v for k, v in state["seen"].items() if v >= cutoff}
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=1, sort_keys=True)


def item_id(source, entry):
    raw = entry.get("id") or entry.get("link") or entry.get("title", "")
    return hashlib.sha1(f"{source}|{raw}".encode()).hexdigest()[:16]

# ---------- fetching ----------
def fetch_all():
    items, ok = [], 0
    for source, url in FEEDS.items():
        try:
            r = requests.get(url, timeout=20, headers={"User-Agent": "london-alert-bot/1.0"})
            r.raise_for_status()
            feed = feedparser.parse(r.content)
            ok += 1
        except Exception as e:  # noqa: BLE001
            print(f"[warn] {source}: {e}", file=sys.stderr)
            continue
        for e in feed.entries[:40]:
            items.append({
                "id": item_id(source, e),
                "source": source,
                "title": (e.get("title") or "").strip(),
                "summary": re.sub(r"<[^>]+>", " ", e.get("summary") or "")[:400].strip(),
                "link": e.get("link") or "",
                "gdacs_level": (e.get("gdacs_alertlevel") or "").lower(),
            })
    return items, ok

# ---------- scoring ----------
def rule_floor(item):
    """Hard floors for structured warning feeds. Never lowered by the LLM."""
    t = (item["title"] + " " + item["summary"]).lower()
    if item["source"].startswith("Met Office"):
        if "red" in t:
            return 9
        if "amber" in t:
            return 6
        if "yellow" in t:
            return 3
    if item["source"] == "GDACS":
        return {"red": 6, "orange": 4}.get(item["gdacs_level"], 1)
    return 0


def keyword_score(item):
    t = (item["title"] + " " + item["summary"]).lower()
    hits = sum(bool(re.search(p, t)) for p in CRITICAL)
    local = any(re.search(p, t) for p in LOCAL)
    if hits == 0:
        return 0
    score = 4 + 2 * min(hits, 2)
    if local:
        score += 2
    return min(score, 10)


LLM_PROMPT = """You triage news for one person living in London who is preparing for emergencies.
Score each item 0-10:
10 = immediate threat to safety or essential services in London (attack, red weather warning for London, grid/water/telecoms failure, evacuation, government emergency alert).
7-9 = major escalation likely to affect London or the UK within days (war escalation involving UK/NATO, national infrastructure failure, nationwide shortages, COBR convened for a national threat).
4-6 = significant world or UK event worth knowing today, no direct impact expected.
0-3 = routine news, politics as usual, sport, culture, opinion.
Be strict: most items are 0-3. Headlines that speculate or recap are lower than ones reporting something happening now.
Return ONLY a JSON array: [{"id": "...", "score": N, "why": "max 12 words"}]

Items:
"""


def llm_scores(items):
    if not ANTHROPIC_KEY or not items:
        return {}
    payload = [{"id": i["id"], "source": i["source"], "title": i["title"], "summary": i["summary"][:250]} for i in items]
    try:
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": ANTHROPIC_MODEL,
                "max_tokens": 4000,
                "messages": [{"role": "user", "content": LLM_PROMPT + json.dumps(payload)}],
            },
            timeout=60,
        )
        r.raise_for_status()
        text = "".join(b.get("text", "") for b in r.json().get("content", []))
        text = text[text.find("["): text.rfind("]") + 1]
        return {d["id"]: (int(d["score"]), d.get("why", "")) for d in json.loads(text)}
    except Exception as e:  # noqa: BLE001
        print(f"[warn] LLM scoring failed, using keywords: {e}", file=sys.stderr)
        return {}


def score_items(items):
    llm = llm_scores(items)
    for i in items:
        floor = rule_floor(i)
        if i["id"] in llm:
            s, why = llm[i["id"]]
        else:
            s, why = keyword_score(i), "keyword match"
        i["score"] = max(floor, s)
        i["why"] = why
    return items

# ---------- push ----------
def push(title, message, priority=3, tags=None, click=None):
    if DRY_RUN or not NTFY_TOPIC:
        print(f"[push p{priority}] {title}\n{message}\n")
        return
    body = {"topic": NTFY_TOPIC, "title": title[:250], "message": message[:3800], "priority": priority}
    if tags:
        body["tags"] = tags
    if click:
        body["click"] = click
    requests.post(NTFY_SERVER, json=body, timeout=20).raise_for_status()

# ---------- main ----------
def main():
    if not NTFY_TOPIC and not DRY_RUN:
        print("NTFY_TOPIC secret not set yet - skipping run.")
        return
    state = load_state()
    now = dt.datetime.utcnow()
    items, ok_feeds = fetch_all()

    # Feed health
    if ok_feeds == 0:
        state["fail_streak"] = state.get("fail_streak", 0) + 1
        if state["fail_streak"] == FAIL_ALERT_AFTER:
            push("Alert bot can't reach any feeds",
                 f"{FAIL_ALERT_AFTER} runs in a row with no feeds. Could be a wider internet problem. "
                 "Check radio (BBC Radio London 94.9 FM) and phone Emergency Alerts.",
                 priority=4, tags=["warning"])
    else:
        state["fail_streak"] = 0

    new = [i for i in items if i["id"] not in state["seen"]]
    for i in new:
        state["seen"][i["id"]] = now.isoformat()

    # First run: learn what's already out there, don't flood the phone.
    if not state.get("bootstrapped"):
        state["bootstrapped"] = True
        push("Alert bot is live",
             f"Watching {len(FEEDS)} feeds. Urgent items push immediately; "
             f"the rest arrive in a {DIGEST_HOUR_UTC:02d}:00 UTC digest.",
             priority=3, tags=["white_check_mark"])
        save_state(state)
        return

    for i in score_items(new):
        if i["score"] >= URGENT_THRESHOLD:
            push(f"[{i['score']}] {i['title']}",
                 f"{i['source']} - {i['why']}\n\n{i['summary']}",
                 priority=5, tags=["rotating_light"], click=i["link"] or None)
        elif i["score"] >= DIGEST_THRESHOLD:
            state["digest"].append({k: i[k] for k in ("score", "title", "source", "link")})

    # Daily digest + heartbeat
    today = now.date().isoformat()
    if now.hour >= DIGEST_HOUR_UTC and state.get("last_digest") != today:
        d = sorted(state["digest"], key=lambda x: -x["score"])[:15]
        if d:
            lines = [f"[{x['score']}] {x['title']} ({x['source']})" for x in d]
            msg = "\n".join(lines)
        else:
            msg = "Nothing above the digest threshold in the last 24h."
        push(f"Daily digest - bot alive ({ok_feeds}/{len(FEEDS)} feeds OK)", msg, priority=2, tags=["newspaper"])
        state["digest"] = []
        state["last_digest"] = today

    save_state(state)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
comment_bot.py — Replies to unanswered Facebook comments using Claude AI.

Runs hourly via cron. Checks recent posts on each active page, finds
comments with no page reply, and generates a personality-matched response.

State is saved to comment_bot_state.json to avoid duplicate replies.
"""

import json
import logging
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import anthropic
import requests

TOOLS_DIR      = Path(__file__).parent
CONFIG_FILE    = TOOLS_DIR / "review_bot_config.json"
STATE_FILE     = TOOLS_DIR / "comment_bot_state.json"
LOG_FILE       = TOOLS_DIR / "comment_bot_log.txt"
FB_BASE        = "https://graph.facebook.com/v19.0"
LOOKBACK_HOURS = 48   # look back 48 hours on every run

SERVICE_REPLY_GUIDANCE = (
    "Shared Lyra-Sha AI service guidance for all page voices: "
    "If and only if the commenter clearly asks about the AI service, automation, "
    "review replies, comment replies, setup, pricing, or how to get this for their "
    "own business/page, answer helpfully and invite them to visit "
    "https://reviews.lyrashaai.com/ or email help@lyrashaai.com. "
    "Keep it soft and human — no hard sell, no pressure, and no promise that anything "
    "is instant or guaranteed. If they ask about setup timing, say setup depends on "
    "platform access and is handled after onboarding. If the comment is not asking "
    "about buying or using Lyra-Sha AI, do not mention Lyra-Sha AI, pricing, onboarding, "
    "or the service at all."
)

SERVICE_INTEREST_TERMS = (
    "ai", "automation", "automate", "bot", "comment bot", "review bot", "reply bot",
    "service", "setup", "onboarding", "pricing", "price", "cost", "buy", "purchase",
    "sign up", "signup", "hire", "get this", "how do i get", "for my business",
    "for my page", "reviews", "comments", "lyra", "lyra-sha", "lyrasha",
)

_handlers = [logging.StreamHandler()]
try:
    _handlers.append(logging.FileHandler(LOG_FILE))
except OSError:
    pass
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(message)s",
    handlers=_handlers,
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# ── Page personalities ────────────────────────────────────────────────────────
# skip=True = page is inactive, bot will not touch it

PAGES = {
    "1201443929714730": {
        "name": "Watch Sage & Sparrow",
        "skip": False,
        "personality": (
            "You manage Watch Sage & Sparrow, a financial education page built around two "
            "main characters. Sage is the older, steadier guide: experienced, grounded, "
            "practical, and caring. Sparrow is younger, energetic, joyful, and still learning "
            "how to use money tools in real life — sometimes playful or nonchalant about "
            "spending, while Sage gently brings the conversation back to priorities, bills, "
            "planning, and long-term peace. Replies should feel like Sage and Sparrow are "
            "helping everyday people learn financial basics without shame. Be warm, clear, "
            "slightly witty when appropriate, and encouraging. Keep responses to 1-3 sentences. "
            "IMPORTANT: provide general financial education only. Do not give personalized "
            "investment, legal, tax, credit repair, or debt settlement advice. Never tell "
            "someone exactly what to buy, sell, invest in, or stop paying. When needed, say "
            "that the page shares education and tools, not professional financial advice."
        ),
    },
    "1140032179190964": {
        "name": "Lulls To Sleep",
        "skip": False,
        "personality": (
            "You manage Lulls To Sleep — a sleep sounds app and YouTube channel "
            "with soothing 8-hour sleep tracks. Your tone is calm, gentle, and peaceful — "
            "like a warm blanket in text form. Responses should feel restful and helpful. "
            "Never pushy. Keep it to 1-3 sentences."
        ),
    },
    "967325753139998": {
        "name": "H O P E Foundation of Wyoming",
        "skip": True,
        "personality": "",
    },
    "1060639113791712": {
        "name": "Light the Way Home",
        "skip": False,
        "personality": (
            "You manage Light the Way Home (lightthewayhome.us), a sanctuary for families "
            "of missing and murdered loved ones. Be deeply compassionate, hope-forward, "
            "and faith-rooted. Every response should feel like a warm embrace — never clinical, "
            "never dismissive. Empowering, practical, always pointing toward hope and community. "
            "2-4 sentences max. Never minimize anyone's pain."
        ),
    },
    "850416141495992": {
        "name": "Bible for Everyone",
        "skip": False,
        "personality": (
            "You manage Bible for Everyone — animated Bible stories for all ages (4 to 99+). "
            "Be reverent but warm, joyful, and wonder-filled. Make everyone feel welcome, "
            "especially people who never went to church or who were hurt by religion. "
            "Your goal is to make people fall in love with God's Word. "
            "Short and encouraging, 1-3 sentences."
        ),
    },
    "849598628245170": {
        "name": "The Bible Relatable N Easy to Understand",
        "skip": False,
        "personality": (
            "You manage The Bible Relatable N Easy to Understand — a Christian page featuring "
            "books that make scripture accessible and practical for everyday life. "
            "The books are available at lyrashaai.com/ebook-store. "
            "Be friendly, faith-forward, and approachable — like a knowledgeable friend, not a preacher. "
            "When it fits naturally, mention the books at lyrashaai.com/ebook-store. "
            "Warm and brief, 1-3 sentences."
        ),
    },
    "813874968486490": {
        "name": "Trending Products",
        "skip": False,
        "personality": (
            "You manage Trending Products, a page that shares great product deals AND "
            "live crypto, world-market, and travel DATA. Be enthusiastic, helpful, and "
            "genuinely friendly — brief and upbeat, 1-2 sentences. "
            "ABSOLUTE RULE — ZERO financial advice: NEVER tell anyone what to buy, sell, "
            "hold, or invest in; never predict prices or say what they 'should' do; never "
            "endorse any coin, stock, or trade. If a comment asks whether to buy/sell/invest, "
            "what will go up, or for any prediction or recommendation, warmly reply that we "
            "share DATA ONLY so they can decide for themselves, that we are NOT financial or "
            "investment advisors, and invite them to view the live data on the page. Do not "
            "give medical, legal, or travel-booking advice either — information only."
        ),
    },
    "819162777943683": {
        "name": "Journals N Joy Creations",
        "skip": True,
        "personality": "",
    },
    "565654433307015": {
        "name": "Geriatric Teen Mom Oh No",
        "skip": False,
        "personality": (
            "You ARE the Geriatric Teen Mom — a hilarious, loving Abuela from New York City "
            "and Guayanilla, Puerto Rico, in your 50s raising a teenager while also being a grandmother. "
            "Humor is your superpower. You're nostalgic about the 80s and 90s, have warm "
            "Caribbean-NYC Bronx energy, and find life genuinely funny. Think 'ghetto fabulous' "
            "grandmother who has seen EVERYTHING and still shows up with coffee or a small glass of wine. "
            "Funny, real, and short (1-3 sentences). Occasionally end with "
            "'Wise Words — Buckle Up Buttercup!' Keep it human, never robotic."
        ),
    },
    "100269639321135": {
        "name": "IKS Property Investments LLC",
        "skip": True,
        "personality": "",
    },
    "103346532273217": {
        "name": "Solar Angels - True Crime Tribe",
        "skip": False,
        "personality": (
            "You help manage Solar Angels - True Crime Tribe, a community for true crime enthusiasts. "
            "Be curious, engaged, and community-minded. Acknowledge comments warmly and invite "
            "continued conversation. Never speculate irresponsibly about real cases or real people. "
            "Community-first tone, 1-2 sentences."
        ),
    },
}


# ── Helpers ───────────────────────────────────────────────────────────────────

def load_state():
    if STATE_FILE.exists():
        with open(STATE_FILE) as f:
            return json.load(f)
    return {"replied_comment_ids": []}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def get_recent_posts(page_id, page_token, since_ts):
    r = requests.get(
        f"{FB_BASE}/{page_id}/posts",
        params={
            "access_token": page_token,
            "fields": "id,message,created_time",
            "since": int(since_ts.timestamp()),
            "limit": 25,
        },
    )
    if not r.ok:
        log.warning(f"  Posts fetch failed for {page_id}: {r.text[:120]}")
        return []
    return r.json().get("data", [])


def get_comments(post_id, page_token):
    r = requests.get(
        f"{FB_BASE}/{post_id}/comments",
        params={
            "access_token": page_token,
            "fields": "id,from,message,created_time,comments{from,id,message}",
            "limit": 50,
        },
    )
    if not r.ok:
        log.warning(f"  Comments fetch failed for {post_id}: {r.text[:120]}")
        return []
    return r.json().get("data", [])


def page_already_replied(comment, page_id):
    """True if the page itself has already replied to this comment."""
    replies = comment.get("comments", {}).get("data", [])
    return any(rep.get("from", {}).get("id") == page_id for rep in replies)


def looks_like_service_interest(comment_text):
    text = comment_text.lower()
    return any(term in text for term in SERVICE_INTEREST_TERMS)


def generate_reply(claude, personality, page_name, comment_text):
    service_interest = looks_like_service_interest(comment_text)
    service_note = (
        "This comment appears to ask about the Lyra-Sha AI service. You may include "
        "the service link/email if useful."
        if service_interest else
        "This comment does not appear to ask about buying or using Lyra-Sha AI. "
        "Do not mention Lyra-Sha AI, pricing, onboarding, or the service."
    )
    try:
        resp = claude.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=150,
            system=f"{personality}\n\n{SERVICE_REPLY_GUIDANCE}",
            messages=[{
                "role": "user",
                "content": (
                    f"A follower left this comment on the {page_name} Facebook page:\n"
                    f"\"{comment_text}\"\n\n"
                    f"{service_note}\n\n"
                    "Write a short, genuine reply in the voice described. "
                    "No hashtags. Sound like a real person, not a bot. "
                    "Don't start with 'Hi' or 'Hello' every single time — vary it."
                ),
            }],
        )
        return resp.content[0].text.strip()
    except Exception as e:
        log.warning(f"  Claude error: {e}")
        return None


def post_reply(comment_id, page_token, message):
    r = requests.post(
        f"{FB_BASE}/{comment_id}/replies",
        params={"access_token": page_token},
        data={"message": message},
    )
    return r.ok, r.text


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    with open(CONFIG_FILE) as f:
        config = json.load(f)

    claude_key = config.get("claude_api_key")
    if not claude_key:
        log.error("❌  No Claude API key in config.")
        return

    claude  = anthropic.Anthropic(api_key=claude_key)
    state   = load_state()
    replied = set(state.get("replied_comment_ids", []))
    since   = datetime.now(timezone.utc) - timedelta(hours=LOOKBACK_HOURS)

    total = 0

    for client in config.get("facebook_clients", []):
        page_id    = client["page_id"]
        page_token = client["page_access_token"]
        page_cfg   = PAGES.get(page_id)

        if not page_cfg or page_cfg["skip"]:
            continue

        page_name   = page_cfg["name"]
        personality = page_cfg["personality"]
        log.info(f"\n💬  {page_name}")

        posts = get_recent_posts(page_id, page_token, since)
        if not posts:
            log.info("   No recent posts.")
            continue

        page_replied_count = 0

        for post in posts:
            comments = get_comments(post["id"], page_token)

            for comment in comments:
                comment_id   = comment["id"]
                comment_text = comment.get("message", "").strip()
                commenter    = comment.get("from", {}).get("name", "someone")

                # Skip: already handled, empty, too short, or page already replied
                if comment_id in replied or len(comment_text) < 4:
                    continue
                if page_already_replied(comment, page_id):
                    replied.add(comment_id)
                    continue

                reply = generate_reply(claude, personality, page_name, comment_text)
                if not reply:
                    continue

                ok, resp_text = post_reply(comment_id, page_token, reply)
                if ok:
                    log.info(f"   ✅  {commenter}: {reply[:70]}...")
                    replied.add(comment_id)
                    total += 1
                    page_replied_count += 1
                else:
                    log.warning(f"   ❌  Reply failed ({comment_id}): {resp_text[:100]}")

                time.sleep(1)  # Respect API rate limits

        if page_replied_count == 0:
            log.info("   ✓  All comments answered.")

    # Keep state file lean — last 5,000 IDs only
    state["replied_comment_ids"] = list(replied)[-5000:]
    save_state(state)

    log.info(f"\n🎯  Done — {total} comment reply/replies sent.")


if __name__ == "__main__":
    main()

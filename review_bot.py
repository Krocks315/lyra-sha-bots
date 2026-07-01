#!/usr/bin/env python3
"""
Review Bot — Auto-responds to Google Business AND Facebook reviews using Claude AI
Run manually:   python3 review_bot.py
Run on schedule: crontab -e → add: 0 * * * * /usr/bin/python3 /Users/cnp/Downloads/tools/review_bot.py
"""

import json
import logging
import sys
from pathlib import Path
from datetime import datetime

import anthropic
import requests
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials

# ─────────────────────────────────────────────
# Paths & constants
# ─────────────────────────────────────────────
TOOLS_DIR    = Path(__file__).parent
CONFIG_FILE  = TOOLS_DIR / "review_bot_config.json"
CREDS_FILE   = TOOLS_DIR / "google_credentials.json"
TOKEN_FILE   = TOOLS_DIR / "google_token.json"
LOG_FILE     = TOOLS_DIR / "review_bot_log.txt"

SCOPES      = ["https://www.googleapis.com/auth/business.manage"]
ACCT_BASE   = "https://mybusinessaccountmanagement.googleapis.com/v1"
INFO_BASE   = "https://mybusinessbusinessinformation.googleapis.com/v1"
REVIEW_BASE = "https://mybusiness.googleapis.com/v4"
FB_BASE     = "https://graph.facebook.com/v19.0"

_handlers = [logging.StreamHandler(sys.stdout)]
try:
    _handlers.append(logging.FileHandler(LOG_FILE))
except OSError:
    pass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(message)s",
    handlers=_handlers,
)
log = logging.getLogger(__name__)


# ─────────────────────────────────────────────
# Config & Auth
# ─────────────────────────────────────────────

class GoogleAuthUnavailable(RuntimeError):
    """Google review access is not configured for unattended Railway use."""


def load_config():
    if not CONFIG_FILE.exists():
        sys.exit(f"❌  Config not found: {CONFIG_FILE}")
    with open(CONFIG_FILE) as f:
        return json.load(f)


def get_google_creds():
    creds = None
    if TOKEN_FILE.exists():
        try:
            creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)
        except Exception as exc:
            raise GoogleAuthUnavailable(f"saved Google token is invalid: {exc}") from exc
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
            except Exception as exc:
                raise GoogleAuthUnavailable(f"saved Google token could not refresh: {exc}") from exc
        else:
            raise GoogleAuthUnavailable(
                "Google reviews are not authorized. Add a previously-authorized "
                "GOOGLE_TOKEN_JSON Railway variable; interactive Google login cannot run in Railway."
            )
        with open(TOKEN_FILE, "w") as f:
            f.write(creds.to_json())
    return creds


# ─────────────────────────────────────────────
# Google API helpers
# ─────────────────────────────────────────────

def api_get(base, path, token, params=None):
    r = requests.get(
        f"{base}/{path}",
        params=params,
        headers={"Authorization": f"Bearer {token}"}
    )
    if not r.ok:
        print(f"❌ Google API Error {r.status_code}:", r.text)
    r.raise_for_status()
    return r.json()


def api_put(base, path, body, token):
    r = requests.put(
        f"{base}/{path}",
        json=body,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    r.raise_for_status()
    return r.json()


def get_accounts(token):
    return api_get(ACCT_BASE, "accounts", token).get("accounts", [])


def get_locations(account_name, token):
    return api_get(
        INFO_BASE, f"{account_name}/locations",
        token, params={"readMask": "name,title"}
    ).get("locations", [])


def get_unanswered_google_reviews(location_name, token):
    reviews = api_get(REVIEW_BASE, f"{location_name}/reviews", token).get("reviews", [])
    return [r for r in reviews if "reviewReply" not in r]


# ─────────────────────────────────────────────
# Facebook API helpers
# ─────────────────────────────────────────────

def fb_get(path, page_token, params=None):
    p = {"access_token": page_token}
    if params:
        p.update(params)
    r = requests.get(f"{FB_BASE}/{path}", params=p)
    if not r.ok:
        print(f"❌ Facebook API Error {r.status_code}:", r.text)
    r.raise_for_status()
    return r.json()


def fb_post(path, page_token, data):
    r = requests.post(
        f"{FB_BASE}/{path}",
        params={"access_token": page_token},
        json=data
    )
    if not r.ok:
        print(f"❌ Facebook API Error {r.status_code}:", r.text)
    r.raise_for_status()
    return r.json()


def get_unanswered_fb_recommendations(page_id, page_token):
    """Return recommendations that the page hasn't commented on yet."""
    data = fb_get(
        f"{page_id}/ratings",
        page_token,
        params={"fields": "recommendation_type,review_text,reviewer,created_time,comments{from,message}"}
    )
    recs = data.get("data", [])
    unanswered = []
    for rec in recs:
        if not rec.get("review_text", "").strip():
            continue  # skip if no text
        existing = rec.get("comments", {}).get("data", [])
        already_replied = any(c.get("from", {}).get("id") == str(page_id) for c in existing)
        if not already_replied:
            unanswered.append(rec)
    return unanswered


def post_fb_reply(rec_id, reply_text, page_token):
    fb_post(f"{rec_id}/comments", page_token, {"message": reply_text})


def warn_expiring_tokens(fb_clients):
    for client in fb_clients:
        expires_str = client.get("token_expires")
        if not expires_str:
            continue
        expires = datetime.strptime(expires_str, "%Y-%m-%d")
        days_left = (expires - datetime.now()).days
        if days_left < 14:
            log.warning(f"⚠️  {client['name']} Facebook token expires in {days_left} days — renew it!")


# ─────────────────────────────────────────────
# Claude response generation
# ─────────────────────────────────────────────

RATING_MAP = {"ONE": 1, "TWO": 2, "THREE": 3, "FOUR": 4, "FIVE": 5}


def generate_google_reply(review, business_name, claude_client):
    rating_num = RATING_MAP.get(review.get("starRating", "FIVE"), 5)
    comment    = review.get("comment", "").strip() or "(no written comment)"
    reviewer   = review.get("reviewer", {}).get("displayName", "this customer")

    prompt = f"""You are the owner of {business_name} writing a Google review response.

Reviewer: {reviewer}
Rating: {rating_num}/5
Review: "{comment}"

Rules:
- 4-5 stars: thank them by first name, reference something specific they said
- 1-3 stars: sincerely acknowledge, apologize without excuses, invite them to reach out directly to resolve it
- Under 150 words
- Sound like a real human owner who cares — not a template
- Do NOT start with "Thank you for your review"
- Do NOT mention Lyra-Sha AI, automation, bots, onboarding, pricing, or any third-party service

Output ONLY the response text. Nothing else."""

    msg = claude_client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=300,
        messages=[{"role": "user", "content": prompt}],
    )
    return msg.content[0].text.strip()


def generate_facebook_reply(rec, business_name, claude_client):
    rec_type    = rec.get("recommendation_type", "positive")
    review_text = rec.get("review_text", "").strip() or "(no comment)"
    reviewer    = rec.get("reviewer", {}).get("name", "this customer")
    sentiment   = "positive" if rec_type == "positive" else "negative"

    prompt = f"""You are the owner of {business_name} responding to a Facebook recommendation.

Reviewer: {reviewer}
Sentiment: {sentiment}
Their comment: "{review_text}"

Rules:
- Positive: thank them genuinely, reference something specific they said
- Negative: sincerely acknowledge their experience, apologize, invite them to contact you directly
- Under 150 words
- Sound like a real human owner — warm and personal
- Do NOT start with "Thank you for your review"
- Do NOT mention Lyra-Sha AI, automation, bots, onboarding, pricing, or any third-party service

Output ONLY the response text. Nothing else."""

    msg = claude_client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=300,
        messages=[{"role": "user", "content": prompt}],
    )
    return msg.content[0].text.strip()


# ─────────────────────────────────────────────
# Google runner
# ─────────────────────────────────────────────

def run_google(claude, config):
    log.info("\n📍  GOOGLE BUSINESS REVIEWS")
    try:
        creds = get_google_creds()
    except GoogleAuthUnavailable as e:
        log.warning(f"  ⏭️  Google reviews skipped: {e}")
        return 0

    token    = creds.token
    accounts = get_accounts(token)

    if not accounts:
        log.info("  No Google Business accounts found.")
        return 0

    total = 0
    for account in accounts:
        locations = get_locations(account["name"], token)
        for loc in locations:
            biz_name  = loc.get("title", loc["name"])
            unanswered = get_unanswered_google_reviews(loc["name"], token)

            if not unanswered:
                log.info(f"  ✓  {biz_name} — all reviews answered")
                continue

            log.info(f"  📬  {biz_name} — {len(unanswered)} unanswered")
            for review in unanswered:
                reviewer = review.get("reviewer", {}).get("displayName", "anonymous")
                try:
                    reply = generate_google_reply(review, biz_name, claude)
                    api_put(REVIEW_BASE, f"{review['name']}/reply", {"comment": reply}, token)
                    total += 1
                    log.info(f"     ✅  Replied to {reviewer}")
                except Exception as e:
                    log.error(f"     ❌  Failed for {reviewer}: {e}")
    return total


# ─────────────────────────────────────────────
# Facebook runner
# ─────────────────────────────────────────────

def run_facebook(claude, config):
    log.info("\n📘  FACEBOOK RECOMMENDATIONS")
    fb_clients = config.get("facebook_clients", [])

    if not fb_clients:
        log.info("  No Facebook clients configured yet.")
        log.info("  → To add a client, see: /Users/cnp/Downloads/tools/review_bot_outreach.md")
        return 0

    warn_expiring_tokens(fb_clients)
    total = 0

    for client in fb_clients:
        biz_name   = client["name"]
        page_id    = client["page_id"]
        page_token = client["page_access_token"]

        try:
            unanswered = get_unanswered_fb_recommendations(page_id, page_token)

            if not unanswered:
                log.info(f"  ✓  {biz_name} — all recommendations answered")
                continue

            log.info(f"  📬  {biz_name} — {len(unanswered)} unanswered")
            for rec in unanswered:
                reviewer = rec.get("reviewer", {}).get("name", "anonymous")
                try:
                    reply = generate_facebook_reply(rec, biz_name, claude)
                    post_fb_reply(rec["id"], reply, page_token)
                    total += 1
                    log.info(f"     ✅  Replied to {reviewer}")
                except Exception as e:
                    log.error(f"     ❌  Failed for {reviewer}: {e}")
        except Exception as e:
            log.error(f"  ❌  {biz_name} Facebook error: {e}")

    return total


# ─────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────

def run():
    config = load_config()
    claude = anthropic.Anthropic(api_key=config["claude_api_key"])

    google_total = 0
    fb_total     = 0

    try:
        google_total = run_google(claude, config)
    except Exception as e:
        log.error(f"Google section failed: {e}")

    try:
        fb_total = run_facebook(claude, config)
    except Exception as e:
        log.error(f"Facebook section failed: {e}")

    log.info(f"\n🎯  Done — Google: {google_total} | Facebook: {fb_total} replies sent")
    log.info(f"    Full log: {LOG_FILE}")


if __name__ == "__main__":
    run()

# ─────────────────────────────────────────────
# CONFIG STRUCTURE — add Facebook clients like this:
#
# {
#   "claude_api_key": "sk-ant-...",
#   "facebook_clients": [
#     {
#       "name": "Juan's Bodega",
#       "page_id": "123456789",
#       "page_access_token": "EAAxxxxx...",
#       "token_expires": "2026-07-27"
#     }
#   ]
# }
#
# HOW TO GET A CLIENT'S PAGE ACCESS TOKEN:
# 1. Client adds rhismygirl@gmail.com as Admin on their Facebook Page
# 2. Go to: developers.facebook.com/tools/explorer
# 3. Select your app → Generate User Token
# 4. Add permissions: pages_read_user_content, pages_manage_engagement
# 5. Click "Get Page Access Token" → select their page
# 6. Copy the token → paste into config above
# 7. Set token_expires to today's date + 60 days
# ─────────────────────────────────────────────

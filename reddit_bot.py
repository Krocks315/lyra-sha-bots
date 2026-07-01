#!/usr/bin/env python3
"""
reddit_bot.py — Monitors Reddit for mentions of a client's business and
generates Claude-written replies.

Two modes (per config "post_mode"):
  • "draft" (DEFAULT, SAFE) — suggested replies are saved to the drafts store
    (/drafts page) for human review. Nothing is posted. Reddit aggressively
    bans promotional bots, so this is the safe default.
  • "live" — replies are posted directly via the Reddit API. Only enable this
    once the Reddit account is established and warmed up.

Config block (in review_bot_config.json):
{
  "reddit": {
    "enabled": true,
    "post_mode": "draft",
    "client_id": "...",            # from reddit.com/prefs/apps (script app)
    "client_secret": "...",
    "user_agent": "lyra-sha-review-bot/1.0 by u/yourname",
    "username": "",               # only needed for post_mode=live
    "password": "",               # only needed for post_mode=live
    "watches": [
      {
        "client": "Acme Salon",
        "subreddits": ["Denver", "femalefashionadvice"],   # [] = search all of Reddit
        "keywords": ["Acme Salon", "acmesalon.com"],
        "persona": "You represent Acme Salon, a friendly upscale hair salon in Denver. Helpful, warm, never salesy."
      }
    ]
  }
}

Run on its own; main.py schedules it. Requires: praw, anthropic.
"""

import json
import logging
import sys
import time
from pathlib import Path

import anthropic

try:
    import praw
except ImportError:
    praw = None

import drafts_store

HERE        = Path(__file__).parent
CONFIG_FILE = HERE / "review_bot_config.json"
LOG_FILE    = HERE / "reddit_bot_log.txt"
SEARCH_LIMIT = 25          # results per subreddit/keyword per run
MAX_AGE_DAYS = 7           # ignore items older than this

_handlers = [logging.StreamHandler(sys.stdout)]
try:
    _handlers.append(logging.FileHandler(LOG_FILE))
except OSError:
    pass
logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s",
                    handlers=_handlers, datefmt="%Y-%m-%d %H:%M:%S")
log = logging.getLogger(__name__)


def generate_reply(claude, persona, business, original_text):
    try:
        resp = claude.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=180,
            system=(
                persona + "\n\n"
                "You are replying to a Reddit post or comment. Reddit HATES obvious "
                "marketing. Be genuinely helpful first, human, and humble. Only mention "
                "the business if it's truly relevant and natural. No hashtags, no emojis "
                "spam, no 'As an AI'. 1-3 sentences. If the post is negative about the "
                "business, be gracious and offer to make it right — never defensive."
            ),
            messages=[{
                "role": "user",
                "content": (
                    f"Business: {business}\n"
                    f"Reddit content to reply to:\n\"{original_text}\"\n\n"
                    "Write a natural, helpful reply."
                ),
            }],
        )
        return resp.content[0].text.strip()
    except Exception as e:
        log.warning(f"  Claude error: {e}")
        return None


def build_reddit(rcfg, need_post):
    """Create a praw.Reddit client. Read-only if we only need to draft."""
    kwargs = {
        "client_id":     rcfg.get("client_id", ""),
        "client_secret": rcfg.get("client_secret", ""),
        "user_agent":    rcfg.get("user_agent", "lyra-sha-review-bot/1.0"),
    }
    if need_post:
        kwargs["username"] = rcfg.get("username", "")
        kwargs["password"] = rcfg.get("password", "")
    reddit = praw.Reddit(**kwargs)
    if not need_post:
        reddit.read_only = True
    return reddit


def iter_matches(reddit, subreddits, keyword):
    """Yield (id, author, text, url) for submissions matching keyword."""
    targets = subreddits if subreddits else ["all"]
    for sub in targets:
        try:
            for s in reddit.subreddit(sub).search(keyword, sort="new",
                                                   time_filter="week", limit=SEARCH_LIMIT):
                text = f"{s.title}\n{(s.selftext or '')}".strip()
                yield (f"t3_{s.id}", str(s.author), text,
                       f"https://reddit.com{s.permalink}", s)
        except Exception as e:
            log.warning(f"  search failed in r/{sub} for '{keyword}': {e}")


def main():
    if not CONFIG_FILE.exists():
        log.error("❌  Config not found.")
        return
    cfg = json.loads(CONFIG_FILE.read_text())
    rcfg = cfg.get("reddit")

    if not rcfg or not rcfg.get("enabled"):
        log.info("Reddit bot disabled (no 'reddit' config or enabled=false) — skipping.")
        return
    if praw is None:
        log.error("❌  praw not installed — cannot run Reddit bot.")
        return
    if not rcfg.get("client_id") or not rcfg.get("client_secret"):
        log.error("❌  Reddit client_id/client_secret missing in config.")
        return

    post_mode = rcfg.get("post_mode", "draft").lower()
    live = post_mode == "live"
    if live and not (rcfg.get("username") and rcfg.get("password")):
        log.warning("post_mode=live but no username/password — falling back to draft mode.")
        live = False

    claude = anthropic.Anthropic(api_key=cfg["claude_api_key"])
    drafts_store.init_drafts_db()

    try:
        reddit = build_reddit(rcfg, need_post=live)
    except Exception as e:
        log.error(f"❌  Could not start Reddit client: {e}")
        return

    log.info(f"👽  Reddit bot running — mode={'LIVE post' if live else 'DRAFT only'}")
    drafted = posted = 0

    for watch in rcfg.get("watches", []):
        business    = watch.get("client", "the business")
        persona     = watch.get("persona", f"You represent {business}.")
        subreddits  = watch.get("subreddits", [])
        keywords    = watch.get("keywords", [])
        log.info(f"\n🔎  {business} — {len(keywords)} keyword(s), "
                 f"{('r/' + ', r/'.join(subreddits)) if subreddits else 'all of Reddit'}")

        for keyword in keywords:
            for source_id, author, text, url, submission in iter_matches(reddit, subreddits, keyword):
                if not text or len(text) < 8:
                    continue
                if drafts_store.already_drafted("reddit", source_id):
                    continue

                reply = generate_reply(claude, persona, business, text[:1500])
                if not reply:
                    continue

                if live:
                    try:
                        submission.reply(reply)
                        posted += 1
                        log.info(f"   ✅  posted on {url}")
                    except Exception as e:
                        log.warning(f"   ❌  post failed ({url}): {e} — saving as draft")
                        drafts_store.save_draft("reddit", business, source_id, reply,
                                                url, author, text[:500])
                        drafted += 1
                else:
                    drafts_store.save_draft("reddit", business, source_id, reply,
                                            url, author, text[:500])
                    drafted += 1
                    log.info(f"   📝  draft saved for {url}")

                time.sleep(2)  # be gentle with Reddit

    log.info(f"\n🎯  Reddit done — {posted} posted, {drafted} drafts saved.")


if __name__ == "__main__":
    main()

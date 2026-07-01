#!/usr/bin/env python3
"""
missed_call_bot.py — Missed Call Text-Back for local businesses.

Flow:
  1. Customer calls a Twilio number
  2. Twilio fires a webhook → this server
  3. Server answers with TwiML (plays brief message / or just hangs up)
  4. Immediately sends personalized SMS text-back to the caller
  5. Notifies the business owner of the missed call
  6. Tracks all missed calls in SQLite

Setup per client:
  - Client gets their own Twilio number (or forwards their existing number)
  - That number's voice webhook → /webhook/missed-call
  - Client info stored in missed_call_clients table
"""

import json
import logging
import os
import sqlite3
from datetime import datetime
from pathlib import Path

import anthropic
from flask import request
from twilio.rest import Client as TwilioClient
from twilio.twiml.voice_response import VoiceResponse

HERE       = Path(__file__).parent
DB_PATH    = HERE / "missed_calls.db"
CONFIG_FILE = HERE / "review_bot_config.json"

TWILIO_ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN  = os.environ.get("TWILIO_AUTH_TOKEN", "")
TWILIO_NUMBER      = os.environ.get("TWILIO_PHONE_NUMBER", "")
CLAUDE_KEY         = os.environ.get("CLAUDE_API_KEY", "")

if not CLAUDE_KEY and CONFIG_FILE.exists():
    with open(CONFIG_FILE) as f:
        cfg = json.load(f)
    CLAUDE_KEY        = cfg.get("claude_api_key", "")
    TWILIO_AUTH_TOKEN = TWILIO_AUTH_TOKEN or cfg.get("twilio_auth_token", "")

log = logging.getLogger(__name__)

claude = anthropic.Anthropic(api_key=CLAUDE_KEY) if CLAUDE_KEY else None
twilio = TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN) if TWILIO_AUTH_TOKEN else None


# ── Database ──────────────────────────────────────────────────────────────────

def init_missed_call_db():
    conn = sqlite3.connect(str(DB_PATH))
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS missed_call_clients (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            business_name   TEXT NOT NULL,
            twilio_number   TEXT NOT NULL UNIQUE,
            owner_phone     TEXT DEFAULT '',
            owner_name      TEXT DEFAULT '',
            booking_url     TEXT DEFAULT '',
            custom_message  TEXT DEFAULT '',
            active          INTEGER DEFAULT 1,
            created_at      TEXT DEFAULT (datetime('now','localtime'))
        );
        CREATE TABLE IF NOT EXISTS missed_calls (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            caller_number   TEXT NOT NULL,
            called_number   TEXT NOT NULL,
            business_name   TEXT DEFAULT '',
            text_sent       INTEGER DEFAULT 0,
            owner_notified  INTEGER DEFAULT 0,
            caller_replied  INTEGER DEFAULT 0,
            reply_text      TEXT DEFAULT '',
            created_at      TEXT DEFAULT (datetime('now','localtime'))
        );
    """)

    # Seed demo client (Ingrid's test number — replace with real clients)
    existing = conn.execute(
        "SELECT id FROM missed_call_clients WHERE twilio_number=?",
        (TWILIO_NUMBER,)
    ).fetchone()
    if not existing and TWILIO_NUMBER:
        conn.execute(
            "INSERT INTO missed_call_clients (business_name, twilio_number, owner_phone, owner_name) VALUES (?,?,?,?)",
            ("Lyra-Sha AI Demo", TWILIO_NUMBER, TWILIO_NUMBER, "Ingrid")
        )
    conn.commit()
    conn.close()
    log.info("Missed call DB initialized.")


def get_client_by_number(twilio_number: str):
    with sqlite3.connect(str(DB_PATH)) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute(
            "SELECT * FROM missed_call_clients WHERE twilio_number=? AND active=1",
            (twilio_number,)
        ).fetchone()


def log_missed_call(caller, called, business_name, text_sent=0, owner_notified=0):
    with sqlite3.connect(str(DB_PATH)) as conn:
        cur = conn.execute(
            "INSERT INTO missed_calls (caller_number, called_number, business_name, text_sent, owner_notified) VALUES (?,?,?,?,?)",
            (caller, called, business_name, text_sent, owner_notified)
        )
        return cur.lastrowid


# ── Claude message generation ─────────────────────────────────────────────────

def generate_textback(business_name: str, owner_name: str, booking_url: str, custom_message: str) -> str:
    """Generate a warm missed-call text-back message."""
    if custom_message:
        return custom_message

    if not claude:
        booking_line = f" Book online: {booking_url}" if booking_url else ""
        return (
            f"Hi! Sorry we missed your call at {business_name}. "
            f"We'll get back to you shortly — or reply here and we'll help you right away! 😊"
            f"{booking_line}"
        )

    try:
        prompt = (
            f"Write a warm, friendly missed-call text-back SMS for {business_name}. "
            f"Owner's name is {owner_name}. "
            + (f"Include their booking link: {booking_url}" if booking_url else "") +
            " Keep it under 160 characters. Sound human and helpful. "
            "End with an invitation to reply or book. One emoji max."
        )
        resp = claude.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=100,
            messages=[{"role": "user", "content": prompt}]
        )
        return resp.content[0].text.strip()
    except Exception as e:
        log.warning(f"Claude error: {e}")
        return (
            f"Hi! Sorry we missed your call at {business_name}. "
            f"Reply here or call back and we'll take great care of you! 😊"
        )


def generate_owner_alert(caller_number: str, business_name: str) -> str:
    """Alert text to send the business owner."""
    return (
        f"📞 Missed call at {business_name}\n"
        f"Caller: {caller_number}\n"
        f"Auto text-back sent ✅\n"
        f"— Lyra-Sha AI"
    )


# ── SMS helpers ───────────────────────────────────────────────────────────────

def send_sms(to_number: str, body: str, from_number: str = None):
    if not twilio:
        log.warning(f"Twilio not configured — would send to {to_number}: {body[:50]}")
        return False
    try:
        twilio.messages.create(
            body=body,
            from_=from_number or TWILIO_NUMBER,
            to=to_number
        )
        log.info(f"✅ SMS sent to {to_number}: {body[:60]}...")
        return True
    except Exception as e:
        log.error(f"❌ SMS failed to {to_number}: {e}")
        return False


# ── Webhook handler (imported by review_request_bot.py) ──────────────────────

def handle_missed_call():
    """
    Twilio voice webhook handler.
    Configure in Twilio console:
    Phone Numbers → your number → Voice & Fax → A CALL COMES IN → Webhook
    URL: https://lyra-sha-bots-production.up.railway.app/webhook/missed-call
    Method: HTTP POST
    """
    caller_number = request.form.get("From", "")
    called_number = request.form.get("To", "")
    call_status   = request.form.get("CallStatus", "")

    log.info(f"📞 Call from {caller_number} to {called_number} — status: {call_status}")

    # Build TwiML response (brief message then hangup)
    response = VoiceResponse()
    response.say(
        "Thanks for calling! We missed you but we'll text you right back.",
        voice="alice"
    )
    response.hangup()

    # Look up client
    client = get_client_by_number(called_number)
    business_name = client["business_name"] if client else "us"
    owner_name    = client["owner_name"] if client else ""
    owner_phone   = client["owner_phone"] if client else ""
    booking_url   = client["booking_url"] if client else ""
    custom_msg    = client["custom_message"] if client else ""

    # Send text-back to caller
    text_sent = False
    if caller_number and caller_number != called_number:
        msg = generate_textback(business_name, owner_name, booking_url, custom_msg)
        text_sent = send_sms(caller_number, msg, called_number)

    # Alert business owner
    owner_notified = False
    if owner_phone and owner_phone != caller_number:
        alert = generate_owner_alert(caller_number, business_name)
        owner_notified = send_sms(owner_phone, alert, called_number)

    # Log it
    log_missed_call(caller_number, called_number, business_name,
                    int(text_sent), int(owner_notified))

    log.info(f"  Text-back sent: {text_sent} | Owner notified: {owner_notified}")

    return str(response), 200, {"Content-Type": "text/xml"}

#!/usr/bin/env python3
"""
Mama's Fish House reservation watcher (SevenRooms).

Checks for open dinner tables for 2 between Dec 5 and Dec 12, 2026,
and alerts you with a Mac pop-up (with sound), an email, and by opening
the booking page in your browser.

GitHub version: runs once per scheduled run (every ~5 minutes) and emails
new openings. Email settings come from GitHub secrets, not this file.
"""

import argparse
import json
import os
import random
import smtplib
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime
from email.message import EmailMessage
from pathlib import Path

# ----------------------------- SETTINGS ---------------------------------

VENUE = "mamasfishhouserestaurantinn"
PARTY_SIZE = 2
START_DATE = date(2026, 12, 5)
END_DATE = date(2026, 12, 12)          # inclusive
EARLIEST = "16:30"                     # earliest dinner time that counts
LATEST = "20:30"                       # latest dinner time that counts

CHECK_EVERY_MINUTES = 5                # a little random jitter is added
ON_MAC = sys.platform == "darwin"
OPEN_BROWSER_ON_FIND = ON_MAC

# Email (Gmail). Leave GMAIL_APP_PASSWORD empty to skip email.
# Create an app password at https://myaccount.google.com/apppasswords
# These are read from GitHub secrets so nothing private sits in this file.
GMAIL_ADDRESS = os.environ.get("GMAIL_ADDRESS", "")
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "").replace(" ", "")
EMAIL_TO = os.environ.get("EMAIL_TO") or GMAIL_ADDRESS

# ------------------------------------------------------------------------

BOOKING_URL = f"https://www.sevenrooms.com/explore/{VENUE}/reservations/create/search"
API_URL = "https://www.sevenrooms.com/api-yoa/availability/widget/range"
STATE_FILE = Path(__file__).with_name("mamas_watch_seen.json")
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36",
    "Accept": "application/json",
    "Referer": BOOKING_URL,
}


def fetch_one(day, center_time):
    """One request: one day, times around center_time (about 75 minutes either side)."""
    params = {
        "venue": VENUE,
        "time_slot": center_time,
        "party_size": PARTY_SIZE,
        "halo_size_interval": 5,
        "start_date": day.strftime("%m-%d-%Y"),
        "num_days": 1,
        "channel": "SEVENROOMS_WIDGET",
    }
    url = f"{API_URL}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers=HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:500]
        raise RuntimeError(f"HTTP {e.code} for {day} {center_time}. Server said: {detail}\nURL: {url}")


def fetch_availability():
    """Ask about each night separately, centered on 5:30 and 7:30, and merge the results."""
    merged = {}
    day = START_DATE
    while day <= END_DATE:
        for center in ("17:30", "19:30"):
            payload = fetch_one(day, center)
            avail = (payload.get("data") or {}).get("availability") or {}
            for d, shifts in avail.items():
                merged.setdefault(d, []).extend(shifts or [])
            time.sleep(1)
        day = date.fromordinal(day.toordinal() + 1)
    return {"data": {"availability": merged}}


def find_slots(payload):
    """Return a sorted list of (date_str, 'HH:MM', label) for bookable dinner slots."""
    availability = (payload.get("data") or {}).get("availability") or {}
    found = set()

    for day_str, shifts in availability.items():
        for shift in shifts or []:
            for slot in shift.get("times") or []:
                if not isinstance(slot, dict):
                    continue
                # Bookable slots are type "book"; "request" means waitlist/request only.
                if str(slot.get("type", "")).lower() != "book":
                    continue
                iso = slot.get("time_iso") or ""
                try:
                    dt = datetime.strptime(iso[:16], "%Y-%m-%d %H:%M")
                except ValueError:
                    continue
                if not (START_DATE <= dt.date() <= END_DATE):
                    continue
                hhmm = dt.strftime("%H:%M")
                if not (EARLIEST <= hhmm <= LATEST):
                    continue
                label = dt.strftime("%a %b %-d at %-I:%M %p")
                found.add((dt.strftime("%Y-%m-%d"), hhmm, label))
    return sorted(found)


def load_seen():
    try:
        return set(json.loads(STATE_FILE.read_text()))
    except Exception:
        return set()


def save_seen(seen):
    STATE_FILE.write_text(json.dumps(sorted(seen)))


def mac_notify(title, message):
    if not ON_MAC:
        return
    script = (f'display notification {json.dumps(message)} '
              f'with title {json.dumps(title)} sound name "Glass"')
    subprocess.run(["osascript", "-e", script], check=False)


def send_email(subject, body):
    if not (GMAIL_APP_PASSWORD and GMAIL_ADDRESS):
        print("  (Email not set up, skipping.)")
        return
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = GMAIL_ADDRESS
    msg["To"] = EMAIL_TO
    msg.set_content(body)
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ssl.create_default_context()) as s:
        s.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
        s.send_message(msg)


def alert(new_slots):
    lines = [label for _, _, label in new_slots]
    title = f"Mama's Fish House: {len(lines)} new opening(s)"
    body = "Open dinner tables for 2:\n\n" + "\n".join(lines) + f"\n\nBook here: {BOOKING_URL}\n"
    mac_notify(title, ", ".join(lines[:3]) + (" and more" if len(lines) > 3 else ""))
    try:
        send_email(title, body)
    except Exception as e:
        print(f"  Email failed: {e}")
    if OPEN_BROWSER_ON_FIND:
        subprocess.run(["open", BOOKING_URL], check=False)


def check_once(seen, verbose=False):
    payload = fetch_availability()
    if verbose:
        Path("mamas_watch_raw.json").write_text(json.dumps(payload, indent=2))
        print("Saved the raw response to mamas_watch_raw.json")
    slots = find_slots(payload)
    current = {f"{d} {t}" for d, t, _ in slots}
    new = [s for s in slots if f"{s[0]} {s[1]}" not in seen]
    stamp = datetime.now().strftime("%H:%M:%S")
    if slots:
        print(f"[{stamp}] Open now: " + "; ".join(l for _, _, l in slots))
    else:
        print(f"[{stamp}] Nothing open yet.")
    if new:
        alert(new)
    # Remember only slots that are open right now, so if one disappears
    # and comes back later you get alerted again.
    return current


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--test", action="store_true",
                    help="send a test email and show what is open right now")
    args = ap.parse_args()

    if args.test:
        send_email("Mama's watcher test", "Email alerts from GitHub are working.\n\n" + BOOKING_URL)
        print("Test email sent (if email is set up).")
        check_once(set(), verbose=False)
        return

    if date.today() > END_DATE:
        print("Date window has passed. Turn off the workflow in GitHub.")
        return

    # Count failures in a row so a broken check fails the GitHub run once
    # (GitHub then emails you) instead of every 5 minutes.
    fail_file = STATE_FILE.with_name("mamas_watch_failures.txt")
    try:
        fails = int(fail_file.read_text())
    except Exception:
        fails = 0
    try:
        seen = check_once(load_seen())
        save_seen(seen)
        fail_file.write_text("0")
    except Exception as e:
        fails += 1
        fail_file.write_text(str(fails))
        print(f"Check failed ({fails} in a row): {e}")
        if fails == 3:
            sys.exit(1)


if __name__ == "__main__":
    main()

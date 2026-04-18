#!/usr/bin/env python3
"""
Playtomic Court Availability Monitor
=====================================
Polls the Playtomic API for court availability at specific clubs and time slots.
Sends a Telegram notification when a new slot becomes available (e.g. cancellation).

Setup:
  1. Create a Telegram bot via @BotFather → get your BOT_TOKEN
  2. Send a message to your bot, then visit:
     https://api.telegram.org/bot<BOT_TOKEN>/getUpdates
     to find your CHAT_ID
  3. Find your club's tenant_id:
     - Go to https://playtomic.io and navigate to your club
     - The URL looks like: https://playtomic.io/club-name/TENANT_ID
     - Or open DevTools → Network tab → filter "availability" to see the tenant_id
  4. Configure the CLUBS list below
  5. Run: python3 playtomic_monitor.py

Requirements:
  pip install requests
"""

import requests
import json
import time
import os
import logging
from datetime import datetime, timedelta
from pathlib import Path

# ============================================================================
# CONFIGURATION — Edit these values
# ============================================================================

# Telegram Bot credentials
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

# Polling interval in seconds (be respectful — 5 min is a good default)
POLL_INTERVAL_SECONDS = 300  # 5 minutes

# How many days ahead to check
LOOKAHEAD_DAYS = 7

# How many days ahead to check for open matches (0 = today only, 1 = today + tomorrow)
OPEN_MATCH_LOOKAHEAD_DAYS = 1

# Sport: "PADEL", "TENNIS", "BADMINTON", etc.
SPORT_ID = "PADEL"

# Clubs are loaded from clubs.json — edit that file to add/remove clubs
CLUBS_FILE = Path(__file__).parent / "clubs.json"

def load_clubs():
    """Load clubs config from clubs.json."""
    if CLUBS_FILE.exists():
        with open(CLUBS_FILE) as f:
            data = json.load(f)
        # Support both old flat array and new object format
        clubs = data["clubs"] if isinstance(data, dict) else data
        # Convert hour lists to tuples
        for club in clubs:
            club["desired_hours"] = [tuple(h) for h in club["desired_hours"]]
            club.setdefault("weekend_hours", [])
            club["weekend_hours"] = [tuple(h) for h in club["weekend_hours"]]
            club.setdefault("weekend_days", [])
            # Combine all active days for day-level iteration
            club["all_days"] = club["desired_days"] + club["weekend_days"]
        return clubs
    return []


def load_radius_config() -> dict:
    """Load the open_match_radius_search config from clubs.json."""
    if CLUBS_FILE.exists():
        with open(CLUBS_FILE) as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data.get("open_match_radius_search", {})
    return {}

# ============================================================================
# END CONFIGURATION
# ============================================================================

API_BASE = "https://api.playtomic.io/v1"
STATE_FILE = Path(__file__).parent / ".playtomic_state.json"
MATCHES_STATE_FILE = Path(__file__).parent / ".playtomic_matches_state.json"
RADIUS_MATCHES_STATE_FILE = Path(__file__).parent / ".playtomic_radius_matches_state.json"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("playtomic")

# Counter file to track checks for "nothing new" throttling
CHECK_COUNTER_FILE = Path(__file__).parent / ".playtomic_counter"


def send_telegram(message: str):
    """Send a message via Telegram bot."""
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    try:
        resp = requests.post(url, json=payload, timeout=10)
        if resp.status_code != 200:
            log.error(f"Telegram error {resp.status_code}: {resp.text}")
    except Exception as e:
        log.error(f"Telegram send failed: {e}")


def fetch_availability(tenant_id: str, date: datetime) -> list:
    """
    Fetch court availability for a given tenant and date.
    The API only allows a 25h window per request.
    """
    start_min = date.strftime("%Y-%m-%dT00:00:00")
    start_max = date.strftime("%Y-%m-%dT23:59:59")

    params = {
        "sport_id": SPORT_ID,
        "tenant_id": tenant_id,
        "start_min": start_min,
        "start_max": start_max,
    }
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; CourtMonitor/1.0)",
        "Accept": "application/json",
    }

    try:
        resp = requests.get(
            f"{API_BASE}/availability",
            params=params,
            headers=headers,
            timeout=15,
        )
        if resp.status_code == 200:
            return resp.json()
        else:
            log.warning(f"API returned {resp.status_code} for tenant {tenant_id} on {date.date()}")
            return []
    except Exception as e:
        log.error(f"API request failed: {e}")
        return []


def time_in_range(start_str: str, end_str: str, check_time: str) -> bool:
    """Check if check_time (HH:MM) falls within [start_str, end_str)."""
    return start_str <= check_time < end_str


def get_hours_for_day(club: dict, weekday: int) -> list:
    """Return the desired hours for a given day of the week."""
    if weekday in club.get("weekend_days", []):
        return club.get("weekend_hours", [])
    if weekday in club["desired_days"]:
        return club["desired_hours"]
    return []


def extract_slots(availability_data: list, club: dict) -> set:
    """
    Parse API response and return a set of slot identifiers that match
    the desired time windows and days.

    API returns: start_date as "YYYY-MM-DD", start_time as "HH:MM:SS"
    Returns a set of datetime strings: "YYYY-MM-DDTHH:MM:SS"
    Deduplicates by time (multiple courts at the same hour count as one).
    """
    matching_slots = set()

    indoor_only = club.get("indoor_only", False)

    for resource in availability_data:
        start_date = resource.get("start_date", "")
        slots = resource.get("slots", [])

        if indoor_only:
            resource_name = resource.get("resource_name", "").lower()
            if "indoor" not in resource_name:
                continue

        for slot in slots:
            start_time = slot.get("start_time", "")
            if not start_time or not start_date:
                continue

            # Build full datetime from date + time (API returns local Madrid time)
            full_dt_str = f"{start_date}T{start_time}"
            try:
                dt = datetime.fromisoformat(full_dt_str)
            except ValueError:
                continue

            # Get hours for this day of the week
            hours = get_hours_for_day(club, dt.weekday())
            if not hours:
                continue

            # Check time window
            time_str = dt.strftime("%H:%M")
            in_window = any(
                time_in_range(h_start, h_end, time_str)
                for h_start, h_end in hours
            )
            if not in_window:
                continue

            matching_slots.add(full_dt_str)

    return matching_slots


def load_state() -> dict:
    """Load previous known slots from disk."""
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            return {}
    return {}


def save_state(state: dict):
    """Persist known slots to disk."""
    STATE_FILE.write_text(json.dumps(state, indent=2))


def format_slot_message(club_name: str, start_time: str) -> str:
    """Format a human-readable notification for a new slot."""
    try:
        dt = datetime.fromisoformat(start_time)
        display_dt = dt + timedelta(hours=1)
        day_str = display_dt.strftime("%A %d %B")
        time_str = display_dt.strftime("%H:%M")
    except Exception:
        day_str = "?"
        time_str = start_time

    return f"📍 {club_name} — {day_str} {time_str}"


def fetch_open_matches(tenant_id: str) -> list:
    """Fetch open matches for a given tenant."""
    params = {
        "sport_id": SPORT_ID,
        "tenant_id": tenant_id,
    }
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; CourtMonitor/1.0)",
        "Accept": "application/json",
    }

    try:
        resp = requests.get(
            f"{API_BASE}/matches",
            params=params,
            headers=headers,
            timeout=15,
        )
        if resp.status_code == 200:
            return resp.json()
        else:
            log.warning(f"Matches API returned {resp.status_code} for tenant {tenant_id}")
            return []
    except Exception as e:
        log.error(f"Matches API request failed: {e}")
        return []


def extract_open_matches(matches: list, club: dict) -> set:
    """
    Parse matches API response and return a set of match identifiers
    that have open spots and match desired time windows/days.

    Each match key is: "match_id|YYYY-MM-DDTHH:MM:SS|players/max"
    """
    today = datetime.now()
    max_date = (today + timedelta(days=OPEN_MATCH_LOOKAHEAD_DAYS)).date()
    matching = set()

    for match in matches:
        match_id = match.get("match_id", "unknown")
        start_date = match.get("start_date", "")
        if not start_date:
            continue

        try:
            dt = datetime.fromisoformat(start_date.replace("Z", ""))
        except ValueError:
            continue

        # Must be in the future and within lookahead (today + tomorrow, any hour)
        if dt < today or dt.date() > max_date:
            continue

        # Count players vs max
        teams = match.get("teams", [])
        total_players = sum(len(team.get("players", [])) for team in teams)
        max_players = match.get("max_players", 4)

        if total_players >= max_players:
            continue

        match_key = f"{match_id}|{dt.strftime('%Y-%m-%dT%H:%M:%S')}|{total_players}/{max_players}"
        matching.add(match_key)

    return matching


def load_matches_state() -> dict:
    """Load previous known matches from disk."""
    if MATCHES_STATE_FILE.exists():
        try:
            return json.loads(MATCHES_STATE_FILE.read_text())
        except Exception:
            return {}
    return {}


def save_matches_state(state: dict):
    """Persist known matches to disk."""
    MATCHES_STATE_FILE.write_text(json.dumps(state, indent=2))


def check_open_matches():
    """Check for open matches (partidos abiertos) at all clubs."""
    state = load_matches_state()
    new_state = {}
    notifications = []
    clubs = load_clubs()

    for club in clubs:
        club_name = club["name"]
        tenant_id = club["tenant_id"]

        log.info(f"Checking open matches at {club_name}...")

        matches = fetch_open_matches(tenant_id)
        current_matches = extract_open_matches(matches, club)

        # Use match_id|datetime as the comparison key (strip player count for diffing)
        current_keys = {m.rsplit("|", 1)[0] for m in current_matches}
        previous_keys = set(state.get(tenant_id, []))

        new_keys = current_keys - previous_keys

        if new_keys:
            log.info(f"  → {len(new_keys)} new open match(es) at {club_name}!")
            # Find full match info for new keys
            for match_str in sorted(current_matches):
                key = match_str.rsplit("|", 1)[0]
                if key in new_keys:
                    _mid, dt_str, players_str = match_str.split("|")
                    try:
                        dt = datetime.fromisoformat(dt_str)
                        display_dt = dt + timedelta(hours=1)
                        day_str = display_dt.strftime("%A %d %B")
                        time_str = display_dt.strftime("%H:%M")
                    except Exception:
                        day_str = "?"
                        time_str = dt_str
                    notifications.append(
                        f"🏓 Open match at {club_name} — {day_str} {time_str} ({players_str} players)"
                    )
        else:
            log.info(f"  → No new open matches at {club_name}")

        new_state[tenant_id] = list(current_keys)
        time.sleep(1)

    # Send notifications
    if notifications:
        if len(notifications) <= 3:
            for msg in notifications:
                send_telegram(msg)
                time.sleep(0.5)
        else:
            header = f"🏓 <b>{len(notifications)} new open matches found!</b>\n\n"
            combined = header + "\n---\n".join(notifications)
            if len(combined) > 4000:
                for msg in notifications:
                    send_telegram(msg)
                    time.sleep(0.5)
            else:
                send_telegram(combined)

    save_matches_state(new_state)


def fetch_tenants_in_radius(lat: float, lon: float, radius_m: int) -> list:
    """Discover all active clubs within a geographic radius via /v1/tenants."""
    params = {
        "user_id": "me",
        "playtomic_status": "ACTIVE",
        "coordinate": f"{lat},{lon}",
        "sport_id": SPORT_ID,
        "radius": str(radius_m),
        "size": "40",
    }
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; CourtMonitor/1.0)",
        "Accept": "application/json",
    }
    try:
        resp = requests.get(f"{API_BASE}/tenants", params=params, headers=headers, timeout=15)
        if resp.status_code == 200:
            tenants = resp.json()
            return [
                {"tenant_id": t.get("tenant_id"), "name": t.get("tenant_name", "Unknown")}
                for t in tenants
                if t.get("tenant_id")
            ]
        else:
            log.warning(f"Tenants API returned {resp.status_code}")
            return []
    except Exception as e:
        log.error(f"Tenants API request failed: {e}")
        return []


def load_radius_matches_state() -> dict:
    """Load previous known radius-search matches from disk."""
    if RADIUS_MATCHES_STATE_FILE.exists():
        try:
            return json.loads(RADIUS_MATCHES_STATE_FILE.read_text())
        except Exception:
            return {}
    return {}


def save_radius_matches_state(state: dict):
    """Persist known radius-search matches to disk."""
    RADIUS_MATCHES_STATE_FILE.write_text(json.dumps(state, indent=2))


def check_open_matches_radius():
    """Check for open matches within a geographic radius."""
    config = load_radius_config()
    if not config or not config.get("enabled"):
        return

    lat = config["latitude"]
    lon = config["longitude"]
    radius_m = config["radius_m"]
    excluded = set(config.get("excluded_tenant_ids", []))

    log.info(f"Discovering clubs within {radius_m}m of ({lat:.4f}, {lon:.4f})...")
    tenants = fetch_tenants_in_radius(lat, lon, radius_m)
    tenants = [t for t in tenants if t["tenant_id"] not in excluded]
    log.info(f"  → Found {len(tenants)} club(s) in radius")

    state = load_radius_matches_state()
    new_state = {}
    notifications = []

    for tenant in tenants:
        tid = tenant["tenant_id"]
        name = tenant["name"]

        log.info(f"  Checking open matches at {name} (radius)...")
        matches = fetch_open_matches(tid)
        current_matches = extract_open_matches(matches, {})

        current_keys = {m.rsplit("|", 1)[0] for m in current_matches}
        previous_keys = set(state.get(tid, []))

        new_keys = current_keys - previous_keys

        if new_keys:
            log.info(f"    → {len(new_keys)} new open match(es) at {name}!")
            for match_str in sorted(current_matches):
                key = match_str.rsplit("|", 1)[0]
                if key in new_keys:
                    _mid, dt_str, players_str = match_str.split("|")
                    try:
                        dt = datetime.fromisoformat(dt_str)
                        display_dt = dt + timedelta(hours=1)
                        day_str = display_dt.strftime("%A %d %B")
                        time_str = display_dt.strftime("%H:%M")
                    except Exception:
                        day_str = "?"
                        time_str = dt_str
                    notifications.append(
                        f"🏓 Open match at {name} — {day_str} {time_str} ({players_str} players)"
                    )
        else:
            log.info(f"    → No new open matches at {name}")

        new_state[tid] = list(current_keys)
        time.sleep(1)

    if notifications:
        if len(notifications) <= 3:
            for msg in notifications:
                send_telegram(msg)
                time.sleep(0.5)
        else:
            header = f"🏓 <b>{len(notifications)} new open matches found nearby!</b>\n\n"
            combined = header + "\n---\n".join(notifications)
            if len(combined) > 4000:
                for msg in notifications:
                    send_telegram(msg)
                    time.sleep(0.5)
            else:
                send_telegram(combined)

    save_radius_matches_state(new_state)


def check_all_clubs():
    """Main check loop: fetch availability for all clubs, diff against known state, notify."""
    state = load_state()
    new_state = {}
    notifications = []
    clubs = load_clubs()

    for club in clubs:
        club_name = club["name"]
        tenant_id = club["tenant_id"]
        club_key = tenant_id

        log.info(f"Checking {club_name}...")

        all_matching_slots = set()

        # Check each day in the lookahead window
        today = datetime.now()
        for day_offset in range(LOOKAHEAD_DAYS):
            target_date = today + timedelta(days=day_offset)

            # Skip days we don't care about
            if target_date.weekday() not in club["all_days"]:
                continue

            availability = fetch_availability(tenant_id, target_date)
            slots = extract_slots(availability, club)
            all_matching_slots.update(slots)

            # Small delay between requests to be polite
            time.sleep(1)

        current_slots = all_matching_slots
        first_run = club_key not in state
        previous_slots = set(state.get(club_key, []))

        # New slots = currently available but weren't before (cancellations!)
        new_slots = current_slots - previous_slots

        if new_slots and first_run:
            log.info(f"  → First run for {club_name}, seeding state with {len(current_slots)} slot(s), no notification.")
        elif new_slots:
            log.info(f"  → {len(new_slots)} new slot(s) found at {club_name}!")
            for slot_str in sorted(new_slots):
                msg = format_slot_message(club_name, slot_str)
                notifications.append(msg)
        else:
            log.info(f"  → No new slots at {club_name}")

        new_state[club_key] = list(current_slots)

    # Send notifications
    if notifications:
        if len(notifications) > 7:
            log.info(f"  → Skipping {len(notifications)} notifications (likely stale state, not a real burst)")
            save_state(new_state)
            return
        # Group into a single message if few, or send individually
        if len(notifications) <= 3:
            for msg in notifications:
                send_telegram(msg)
                time.sleep(0.5)
        else:
            # Batch into one message
            header = f"🎾 <b>{len(notifications)} new court slots found!</b>\n\n"
            combined = header + "\n---\n".join(notifications)
            # Telegram has a 4096 char limit
            if len(combined) > 4000:
                for msg in notifications:
                    send_telegram(msg)
                    time.sleep(0.5)
            else:
                send_telegram(combined)

    # Save state for next run
    save_state(new_state)

    check_open_matches_radius()

    log.info(f"State saved. Next check in {POLL_INTERVAL_SECONDS}s.")


def find_tenant_id(club_name_query: str, latitude: float = 40.4168, longitude: float = -3.7038):
    """
    Helper: search for clubs near a coordinate to find their tenant_id.
    Default coordinates are central Madrid.
    """
    params = {
        "user_id": "me",
        "playtomic_status": "ACTIVE",
        "coordinate": f"{latitude},{longitude}",
        "sport_id": SPORT_ID,
        "radius": "50000",
        "size": "40",
        "q": club_name_query,
    }
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; CourtMonitor/1.0)",
        "Accept": "application/json",
    }

    try:
        resp = requests.get(f"{API_BASE}/tenants", params=params, headers=headers, timeout=15)
        if resp.status_code == 200:
            clubs = resp.json()
            print(f"\nFound {len(clubs)} club(s) matching '{club_name_query}':\n")
            for c in clubs:
                name = c.get("tenant_name", "Unknown")
                tid = c.get("tenant_id", "N/A")
                addr = c.get("address", {})
                street = addr.get("street", "")
                city = addr.get("city", "")
                print(f"  📍 {name}")
                print(f"     ID: {tid}")
                print(f"     Address: {street}, {city}")
                print()
            return clubs
        else:
            print(f"Search failed with status {resp.status_code}")
            return []
    except Exception as e:
        print(f"Search error: {e}")
        return []


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "search":
        # Usage: python3 playtomic_monitor.py search "club name"
        query = " ".join(sys.argv[2:]) if len(sys.argv) > 2 else ""
        if not query:
            print("Usage: python3 playtomic_monitor.py search <club name>")
            sys.exit(1)
        find_tenant_id(query)
    elif len(sys.argv) > 1 and sys.argv[1] == "once":
        # Run a single check (useful for cron)
        log.info("Running single check...")
        check_all_clubs()
    else:
        # Continuous polling mode
        log.info("Starting Playtomic Court Monitor (continuous mode)")
        log.info(f"Monitoring {len(load_clubs())} club(s), polling every {POLL_INTERVAL_SECONDS}s")

        # Send startup notification
        send_telegram("🟢 Playtomic monitor started! Watching for court cancellations...")

        while True:
            try:
                check_all_clubs()
            except Exception as e:
                log.error(f"Unexpected error: {e}")
            time.sleep(POLL_INTERVAL_SECONDS)

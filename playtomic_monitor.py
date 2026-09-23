#!/usr/bin/env python3
"""
Playtomic Court Availability Monitor
=====================================
Polls the Playtomic API for court availability at specific clubs and time slots.
Sends an ntfy notification when a new slot becomes available (e.g. cancellation).

Setup:
  1. Install the ntfy app (https://ntfy.sh) on your phone and subscribe
     to a random topic name (e.g. "padel-x7k2q" — hard to guess so strangers can't spam you)
  2. Set the NTFY_TOPIC env var to that topic name
  3. Set PLAYTOMIC_EMAIL and PLAYTOMIC_PASSWORD (a normal Playtomic account —
     the API now requires a Bearer token obtained by logging in)
  4. Find your club's tenant_id:
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
import random
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

def _load_dotenv():
    """Load KEY=VALUE pairs from .env next to the script (local convenience)."""
    env_file = Path(__file__).parent / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip())


_load_dotenv()

# ============================================================================
# CONFIGURATION — Edit these values
# ============================================================================

# ntfy notification settings (env vars, loaded from .env if present)
NTFY_SERVER = os.environ.get("NTFY_SERVER", "https://ntfy.sh")
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "")
NTFY_TOKEN = os.environ.get("NTFY_TOKEN", "")  # optional, only for protected topics

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

API_BASE = os.environ.get("PLAYTOMIC_API_BASE", "https://api.app.playtomic.io/v1")

# Playtomic account credentials (API requires a Bearer token since 2026)
PLAYTOMIC_EMAIL = os.environ.get("PLAYTOMIC_EMAIL", "")
PLAYTOMIC_PASSWORD = os.environ.get("PLAYTOMIC_PASSWORD", "")
# Optional: a pre-minted refresh token (`python3 playtomic_monitor.py seed`).
# Needed on hosts whose IPs are WAF-blocked for /auth/login (e.g. GitHub Actions).
PLAYTOMIC_REFRESH_TOKEN = os.environ.get("PLAYTOMIC_REFRESH_TOKEN", "")
LOGIN_PATH = "/v3/auth/login"
REFRESH_PATH = "/v3/auth/token"
TOKEN_FILE = Path(__file__).parent / ".playtomic_token.json"
STATE_FILE = Path(__file__).parent / ".playtomic_state.json"
MATCHES_STATE_FILE = Path(__file__).parent / ".playtomic_matches_state.json"
RADIUS_MATCHES_STATE_FILE = Path(__file__).parent / ".playtomic_radius_matches_state.json"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("playtomic")

API_HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36",
    "Accept": "application/json",
}


def _load_token() -> dict:
    if TOKEN_FILE.exists():
        try:
            return json.loads(TOKEN_FILE.read_text())
        except Exception:
            return {}
    return {}


def _save_token(token: dict):
    TOKEN_FILE.write_text(json.dumps(token, indent=2))
    try:
        TOKEN_FILE.chmod(0o600)  # contains session credentials
    except Exception:
        pass


def _authenticate(path: str, payload: dict) -> dict:
    """Exchange credentials or a refresh token for a new token pair."""
    resp = requests.post(
        f"{API_BASE.rsplit('/v1', 1)[0]}{path}",
        json={**payload, "requested_user_roles": ["ROLE_CUSTOMER"]},
        headers=API_HEADERS,
        timeout=15,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"Playtomic auth failed ({resp.status_code}): {resp.text[:200]}")
    return resp.json()


def _parse_expiry(value) -> float:
    """Token expiry may be epoch seconds, epoch ms, or an ISO timestamp string."""
    if isinstance(value, (int, float)):
        v = float(value)
        return v / 1000.0 if v > 1e12 else v
    if isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value)
            if dt.tzinfo is None:
                # API returns naive UTC timestamps
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.timestamp()
        except ValueError:
            return 0.0
    return 0.0


def get_bearer(force_refresh: bool = False) -> str:
    """Return a valid access token, logging in / refreshing as needed."""
    if not (PLAYTOMIC_EMAIL and PLAYTOMIC_PASSWORD):
        raise RuntimeError("PLAYTOMIC_EMAIL and PLAYTOMIC_PASSWORD must be set")

    token = _load_token()
    if not force_refresh and token.get("access_token") and _parse_expiry(token.get("access_token_expiration")) > time.time() + 60:
        return token["access_token"]

    try:
        token = _authenticate(REFRESH_PATH, {"refresh_token": token["refresh_token"]})
    except Exception:
        if PLAYTOMIC_REFRESH_TOKEN and token.get("refresh_token") != PLAYTOMIC_REFRESH_TOKEN:
            token = _authenticate(REFRESH_PATH, {"refresh_token": PLAYTOMIC_REFRESH_TOKEN})
        else:
            token = _authenticate(LOGIN_PATH, {"email": PLAYTOMIC_EMAIL, "password": PLAYTOMIC_PASSWORD})
    _save_token(token)
    return token["access_token"]


def api_get(path: str, params: dict) -> list:
    """
    Authenticated GET against the Playtomic API.
    Retries once on 401 (re-auth) and once on 429 (honouring Retry-After).
    """
    for attempt in (1, 2):
        try:
            bearer = get_bearer(force_refresh=(attempt == 2))
        except Exception as e:
            log.error(f"Authentication error: {e}")
            return []
        headers = {**API_HEADERS, "Authorization": f"Bearer {bearer}"}
        try:
            resp = requests.get(f"{API_BASE}{path}", params=params, headers=headers, timeout=15)
            if resp.status_code == 200:
                return resp.json()
            if resp.status_code == 401 and attempt == 1:
                log.warning("Got 401, forcing re-authentication...")
                continue
            if resp.status_code == 429 and attempt == 1:
                retry_after = int(resp.headers.get("Retry-After", "30"))
                log.warning(f"Rate limited (429), waiting {retry_after}s before retrying...")
                time.sleep(min(retry_after, 120))
                continue
            if resp.status_code >= 500 and attempt == 1:
                time.sleep(10)  # server hiccup — brief backoff, then one retry
                continue
            log.warning(f"API {path} returned {resp.status_code}")
            return []
        except Exception as e:
            log.error(f"API request failed: {e}")
            return []
    return []

# Counter file to track checks for "nothing new" throttling
CHECK_COUNTER_FILE = Path(__file__).parent / ".playtomic_counter"


def send_ntfy(message: str, title: str = "Playtomic monitor"):
    """Send a push notification via ntfy."""
    if not NTFY_TOPIC:
        log.error("NTFY_TOPIC is not set — cannot send notification")
        return
    url = f"{NTFY_SERVER.rstrip('/')}/{NTFY_TOPIC}"
    headers = {
        "Title": title,
        "Priority": "high",
        "Tags": "tennis",
        "Markdown": "yes",
    }
    if NTFY_TOKEN:
        headers["Authorization"] = f"Bearer {NTFY_TOKEN}"
    try:
        resp = requests.post(url, data=message.encode("utf-8"), headers=headers, timeout=10)
        if resp.status_code >= 400:
            log.error(f"ntfy error {resp.status_code}: {resp.text}")
    except Exception as e:
        log.error(f"ntfy send failed: {e}")


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
    try:
        return api_get("/availability", params)
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
    try:
        return api_get("/matches", params)
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
                send_ntfy(msg, title="New open match 🏓")
        else:
            send_ntfy("\n---\n".join(notifications), title=f"{len(notifications)} new open matches found! 🏓")

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
    try:
        tenants = api_get("/tenants", params)
        return [
            {"tenant_id": t.get("tenant_id"), "name": t.get("tenant_name", "Unknown")}
            for t in tenants
            if t.get("tenant_id")
        ]
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
                send_ntfy(msg, title="New open match nearby 🏓")
        else:
            send_ntfy("\n---\n".join(notifications), title=f"{len(notifications)} new open matches nearby! 🏓")

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
                send_ntfy(msg, title="New court slot available 🎾")
        else:
            send_ntfy("\n---\n".join(notifications), title=f"{len(notifications)} new court slots found! 🎾")

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
    try:
        clubs = api_get("/tenants", params)
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
    elif len(sys.argv) > 1 and sys.argv[1] == "seed":
        # Fresh login -> print a refresh token to paste into the
        # PLAYTOMIC_REFRESH_TOKEN GitHub secret (for WAF-blocked hosts).
        if not (PLAYTOMIC_EMAIL and PLAYTOMIC_PASSWORD):
            print("Set PLAYTOMIC_EMAIL and PLAYTOMIC_PASSWORD first.")
            sys.exit(1)
        token = _authenticate(LOGIN_PATH, {"email": PLAYTOMIC_EMAIL, "password": PLAYTOMIC_PASSWORD})
        _save_token(token)
        print("\nPaste this as the PLAYTOMIC_REFRESH_TOKEN GitHub secret:\n")
        print(token["refresh_token"])
        print("\n(Note: single-use — the monitor rotates it and keeps the new one")
        print("in the Actions cache. Re-run `seed` if the chain ever breaks.)")
    elif len(sys.argv) > 1 and sys.argv[1] == "test":
        # Send a fake notification through the full ntfy path
        log.info("Sending test notification via ntfy...")
        send_ntfy("🧪 This is a test notification from your Playtomic monitor.", title="Test notification")
        log.info("Done — check your ntfy app.")
    elif len(sys.argv) > 1 and sys.argv[1] == "once":
        # Run a single check (useful for cron)
        log.info("Running single check...")
        check_all_clubs()
    else:
        # Continuous polling mode
        log.info("Starting Playtomic Court Monitor (continuous mode)")
        log.info(f"Monitoring {len(load_clubs())} club(s), polling every {POLL_INTERVAL_SECONDS}s")

        # Send startup notification
        send_ntfy("🟢 Playtomic monitor started! Watching for court cancellations...", title="Monitor started")

        while True:
            try:
                check_all_clubs()
            except Exception as e:
                log.error(f"Unexpected error: {e}")
            # Random 0-30s jitter so requests don't land at perfectly regular times
            time.sleep(POLL_INTERVAL_SECONDS + random.uniform(0, 30))

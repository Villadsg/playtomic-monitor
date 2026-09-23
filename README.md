# Playtomic Court Cancellation Monitor

Get push notifications (via [ntfy](https://ntfy.sh)) when courts become available at your favourite padel/tennis clubs on Playtomic — perfect for catching cancellations.

## Quick Start

### 1. Set Up ntfy Notifications

1. Install the **ntfy** app on your phone ([Android](https://play.google.com/store/apps/details?id=io.heckel.ntfy) / [iOS](https://apps.apple.com/us/app/ntfy/id1625396347))
2. In the app, subscribe to a topic with a name that's hard to guess (e.g. `padel-x7k2q`) — anyone who knows the topic name can send you messages, so don't make it simple
3. That topic name is your `NTFY_TOPIC`

> **Optional:** self-hosting ntfy or using a protected topic? Set `NTFY_SERVER` (default `https://ntfy.sh`) and `NTFY_TOKEN` (access token for restricted topics). For everyday use, the public server with an unguessable topic is fine.

### 2. Get a Playtomic Account

The API now requires authentication. Use your normal Playtomic login:

```bash
export PLAYTOMIC_EMAIL=you@example.com
export PLAYTOMIC_PASSWORD=yourpassword
```

(Or put both in a `.env` file next to the script for local runs.)

### 3. Find Your Club's Tenant ID

```bash
pip install requests
python3 playtomic_monitor.py search "your club name"
```

This searches clubs near Madrid by default. You'll get output like:

```
📍 Club Padel Madrid Centro
   ID: a1b2c3d4-e5f6-7890-abcd-ef1234567890
   Address: Calle Example 42, Madrid
```

### 4. Configure the Script

Edit `clubs.json` to add your clubs with tenant IDs and desired time windows. Then provide the ntfy topic as an env var:

```bash
export NTFY_TOPIC=padel-x7k2q
```

### 5. Run It

**Option A: Locally (continuous)**
```bash
python3 playtomic_monitor.py
```

**Option B: Locally via cron (single check)**
```bash
# Add to crontab -e:
*/5 * * * * cd /path/to/project && python3 playtomic_monitor.py once
```

**Option C: Locally via systemd**
Copy `playtomic-monitor.service` to `/etc/systemd/system/`, put `NTFY_TOPIC=...` (and optional `NTFY_SERVER`/`NTFY_TOKEN`) in a `.env` file next to the project, then:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now playtomic-monitor
```

**Option D: GitHub Actions (free, recommended)**
1. Create a private GitHub repo
2. Copy `playtomic_monitor.py`, `clubs.json` and `.github/workflows/monitor.yml`
3. Go to repo Settings → Secrets and variables → Actions and add:
   - `NTFY_TOPIC` (required)
   - `PLAYTOMIC_REFRESH_TOKEN` (required — see below)
   - `PLAYTOMIC_EMAIL`, `PLAYTOMIC_PASSWORD` (fallback auth)
   - `NTFY_SERVER`, `NTFY_TOKEN` (optional)

**GitHub Actions auth:** Playtomic's WAF blocks login requests from datacenter
IPs like GitHub's runners (403), so Actions can't log in with email/password
directly. Instead, mint a refresh token locally and give it to Actions:

```bash
python3 playtomic_monitor.py seed
# paste the printed token as the PLAYTOMIC_REFRESH_TOKEN secret
```

The workflow caches `.playtomic_token.json` between runs, so the rotated
refresh token survives. Refresh tokens are single-use — if the chain ever
breaks (cache loss, token revoked), just re-run `seed` and update the secret.
Running the monitor locally at the same time is fine: the phone app, web, and
scripts each keep independent sessions.
4. Push — the workflow runs hourly 8am–11pm Madrid time, looping every 5 minutes

> Bonus: GitHub Actions rotates IP addresses on each run, reducing the chance of rate limiting.

## How It Works

1. Polls `https://api.app.playtomic.io/v1/availability` for each configured club + date (authenticated with a Bearer token from your Playtomic account)
2. Filters slots by your desired time windows and days of week
3. Compares against previously seen slots (stored in `.playtomic_state.json`)
4. New slots = cancellations → sends an ntfy push notification (high priority)
5. The API allows a max 25h window per request

## Configuration Examples

**Weekday evenings only** (`clubs.json`):
```json
{
    "clubs": [
        {
            "name": "My Club",
            "tenant_id": "xxx-xxx-xxx",
            "desired_hours": [["18:00", "22:00"]],
            "desired_days": [0, 1, 2, 3, 4]
        }
    ]
}
```

**Weekend mornings + evenings:**
```json
{
    "name": "Weekend Club",
    "tenant_id": "yyy-yyy-yyy",
    "desired_hours": [["09:00", "12:00"], ["17:00", "21:00"]],
    "desired_days": [5, 6],
    "weekend_days": [5, 6]
}
```

## Notes

- First run will show all currently available slots as "new" — after that, only actual changes trigger notifications
- The API has a max 25h window per request, so each day is queried separately
- Be respectful with polling frequency; 5 minutes is a reasonable default
- Sport options: `PADEL`, `TENNIS`, `BADMINTON`
- ntfy notifications are sent with **high priority** so they sound/vibrate even if your phone is in Do Not Disturb-lite modes

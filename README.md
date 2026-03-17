# Playtomic Court Cancellation Monitor

Get Telegram notifications when courts become available at your favourite padel/tennis clubs on Playtomic — perfect for catching cancellations.

## Quick Start

### 1. Create a Telegram Bot

1. Open Telegram and search for **@BotFather**
2. Send `/newbot` and follow the prompts
3. Copy your **bot token** (looks like `123456:ABC-DEF...`)
4. Send any message to your new bot
5. Visit `https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates` and find your **chat_id**

### 2. Find Your Club's Tenant ID

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

### 3. Configure the Script

Edit `playtomic_monitor.py` and fill in:

- `TELEGRAM_BOT_TOKEN` — your bot token (or set as env var)
- `TELEGRAM_CHAT_ID` — your chat ID (or set as env var)
- `CLUBS` list — add your clubs with tenant IDs and desired time windows

### 4. Run It

**Option A: Locally (continuous)**
```bash
python3 playtomic_monitor.py
```

**Option B: Locally via cron (single check)**
```bash
# Add to crontab -e:
*/5 * * * * cd /path/to/project && python3 playtomic_monitor.py once
```

**Option C: GitHub Actions (free, recommended)**
1. Create a private GitHub repo
2. Copy `playtomic_monitor.py` and `.github/workflows/monitor.yml` (rename `github_actions_workflow.yml`)
3. Go to repo Settings → Secrets → Actions and add:
   - `TELEGRAM_BOT_TOKEN`
   - `TELEGRAM_CHAT_ID`
4. Push — the workflow runs every 5 minutes automatically

> Bonus: GitHub Actions rotates IP addresses on each run, reducing the chance of rate limiting.

## How It Works

1. Polls `https://playtomic.io/api/v1/availability` for each configured club + date
2. Filters slots by your desired time windows and days of week
3. Compares against previously seen slots (stored in `.playtomic_state.json`)
4. New slots = cancellations → sends Telegram notification
5. The API is unauthenticated and allows a max 25h window per request

## Configuration Examples

**Weekday evenings only:**
```python
{
    "name": "My Club",
    "tenant_id": "xxx-xxx-xxx",
    "desired_hours": [("18:00", "22:00")],
    "desired_days": [0, 1, 2, 3, 4],  # Mon-Fri
}
```

**Weekend mornings + evenings:**
```python
{
    "name": "Weekend Club",
    "tenant_id": "yyy-yyy-yyy",
    "desired_hours": [("09:00", "12:00"), ("17:00", "21:00")],
    "desired_days": [5, 6],  # Sat-Sun
}
```

## Notes

- First run will show all currently available slots as "new" — after that, only actual changes trigger notifications
- The API has a max 25h window per request, so each day is queried separately
- Be respectful with polling frequency; 5 minutes is a reasonable default
- Sport options: `PADEL`, `TENNIS`, `BADMINTON`

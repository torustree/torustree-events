# Torus Tree — Live Bookwhen Events

Auto-updates `events.json` from the Bookwhen API every 30 minutes.
Website pages fetch that JSON to show live dates (see live-dates-snippet.html).

## One-time setup (5 minutes)
1. Create a new PUBLIC repo called `torustree-events` and upload these files
   (keep the `.github/workflows/` folder structure).
2. Repo → Settings → Secrets and variables → Actions → New repository secret:
   Name: `BOOKWHEN_API_KEY`  ·  Value: your Bookwhen API key
3. Actions tab → "Update Bookwhen events" → Run workflow (first manual run).
   Confirm `events.json` appears in the repo with your events.
4. Done. The site snippet reads:
   https://raw.githubusercontent.com/YOUR_GITHUB_USERNAME/torustree-events/main/events.json

## Notes
- No attendee/space counts are published — deliberate ("spaces are limited" policy).
- The bot commits only when events actually change.

#!/usr/bin/env python3
"""
Torus Tree — Bookwhen events fetcher
Pulls upcoming events from the Bookwhen API and writes events.json.
Run by GitHub Actions on a schedule. Requires env var BOOKWHEN_API_KEY.
"""
import json, os, sys, urllib.request, base64, datetime

API_KEY = os.environ.get("BOOKWHEN_API_KEY", "")
if not API_KEY:
    sys.exit("BOOKWHEN_API_KEY not set")

BASE = "https://api.bookwhen.com/v2"
today = datetime.date.today().strftime("%Y%m%d")

def get(url):
    req = urllib.request.Request(url)
    token = base64.b64encode(f"{API_KEY}:".encode()).decode()
    req.add_header("Authorization", f"Basic {token}")
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)

# Fetch upcoming events with locations included; paginate defensively
events, included, url = [], {}, f"{BASE}/events?filter[from]={today}&include=location&page[size]=100"
while url:
    data = get(url)
    events.extend(data.get("data", []))
    for inc in data.get("included", []):
        included[(inc["type"], inc["id"])] = inc
    url = data.get("links", {}).get("next")

out = []
for ev in events:
    a = ev.get("attributes", {})
    loc = ev.get("relationships", {}).get("location", {}).get("data") or {}
    loc_obj = included.get(("locations", loc.get("id")), {})
    venue = (loc_obj.get("attributes", {}) or {}).get("address_text", "") or ""
    # Event page URL: bookwhen event ids are like "ev-xxxx-20260905..."
    out.append({
        "id": ev.get("id", ""),
        "title": a.get("title", ""),
        "start": a.get("start_at", ""),
        "end": a.get("end_at", ""),
        "venue": venue.split("\n")[0][:80],
        "tags": [t.get("title", "").lower() for t in a.get("tags", []) if isinstance(t, dict)] or a.get("tags", []),
        "url": f"https://bookwhen.com/torustree/e/{ev.get('id','')}"
    })

# NOTE: deliberately NO attendee/space counts — "spaces are limited" policy
out.sort(key=lambda e: e["start"])
with open("events.json", "w") as f:
    json.dump({"updated": datetime.datetime.utcnow().isoformat() + "Z", "events": out}, f, indent=1)
print(f"Wrote {len(out)} events")

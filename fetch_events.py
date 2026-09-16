#!/usr/bin/env python3
"""
Torus Tree — Bookwhen events fetcher + posting stats

1. Pulls upcoming events from the Bookwhen API and writes events.json.
2. Pulls Facebook-group posting history from Supabase and writes
   posting-stats.json — aggregate only, no group names, so it is safe
   to keep in the public repo and readable from any Claude chat at
   https://raw.githubusercontent.com/torustree/torustree-events/main/posting-stats.json

Run by GitHub Actions on a schedule.
Requires env vars BOOKWHEN_API_KEY and SUPABASE_KEY.
"""
import json, os, sys, urllib.request, urllib.error, base64, datetime, collections

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


# ---------------------------------------------------------------- events.json

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
    json.dump({"updated": datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z"), "events": out}, f, indent=1)
print(f"Wrote {len(out)} events")


# --------------------------------------------------------- posting-stats.json

SB_URL = "https://reyqickmgvgehbywprtl.supabase.co/rest/v1"
SB_KEY = os.environ.get("SUPABASE_KEY", "")

# Set to True to include a named top-groups list. OFF by default: group names
# are the one part of this data a competitor could actually use.
INCLUDE_GROUP_NAMES = False


def sb(path):
    """Read-only Supabase query. Doubles as the free-tier keep-alive ping."""
    req = urllib.request.Request(
        f"{SB_URL}/{path}",
        headers={"apikey": SB_KEY, "Authorization": "Bearer " + SB_KEY},
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def post_times(row):
    """campaigns.posts is a jsonb array of {"ts": "...Z"} — newest last."""
    stamps = []
    for p in row.get("posts") or []:
        ts = (p or {}).get("ts") if isinstance(p, dict) else None
        if ts:
            stamps.append(ts)
    return sorted(stamps)


def rate(part, whole):
    return round(part / whole, 3) if whole else None


def summarise(rows):
    c = collections.Counter(r.get("status") or "unknown" for r in rows)
    n = len(rows)
    return {
        "posts": n,
        "live": c.get("live", 0),
        "pending": c.get("pending", 0),
        "declined": c.get("declined", 0),
        "other": n - c.get("live", 0) - c.get("pending", 0) - c.get("declined", 0),
        "live_rate": rate(c.get("live", 0), n),
        "booking_link_rate": rate(sum(1 for r in rows if r.get("link_added")), n),
    }


try:
    campaigns = sb("campaigns?select=id,session_id,group_id,status,link_added,posts,updated_at&limit=5000")
    groups = sb("groups?select=id,name,locs,post_as,archived,blocked_reason&limit=5000")

    gmap = {g["id"]: g for g in groups}
    now = datetime.datetime.now(datetime.timezone.utc)
    cutoff = now - datetime.timedelta(days=30)

    posted, latest_seen = [], ""
    for r in campaigns:
        stamps = post_times(r)
        if not stamps:
            continue          # a row that was never actually posted
        r["_last"] = stamps[-1]
        r["_first"] = stamps[0]
        latest_seen = max(latest_seen, stamps[-1], r.get("updated_at") or "")
        posted.append(r)

    def within_30(r):
        try:
            t = datetime.datetime.fromisoformat(r["_last"].replace("Z", "+00:00"))
        except Exception:
            return False
        return t >= cutoff

    recent = [r for r in posted if within_30(r)]

    # by location — a group's primary location is the first entry in locs
    by_loc = collections.defaultdict(list)
    for r in posted:
        g = gmap.get(r.get("group_id")) or {}
        locs = g.get("locs") or []
        by_loc[(locs[0] if locs else "Unassigned")].append(r)

    # by profile — which account the post went out as
    by_profile = collections.defaultdict(list)
    for r in posted:
        g = gmap.get(r.get("group_id")) or {}
        by_profile[g.get("post_as") or "Unknown"].append(r)

    # by week — last 12 ISO weeks, keyed by the Monday
    by_week = collections.defaultdict(list)
    for r in posted:
        try:
            d = datetime.datetime.fromisoformat(r["_last"].replace("Z", "+00:00")).date()
        except Exception:
            continue
        by_week[(d - datetime.timedelta(days=d.weekday())).isoformat()].append(r)

    stats = {
        "generated": latest_seen or now.isoformat(),
        "note": "Aggregate Facebook group posting performance. No group names, no customer data.",
        "groups": {
            "total": len(groups),
            "active": sum(1 for g in groups if not g.get("archived") and not g.get("blocked_reason")),
            "archived": sum(1 for g in groups if g.get("archived")),
            "blocked": sum(1 for g in groups if g.get("blocked_reason")),
        },
        "all_time": summarise(posted),
        "last_30_days": summarise(recent),
        "by_location": {k: summarise(v) for k, v in sorted(by_loc.items())},
        "by_profile": {k: summarise(v) for k, v in sorted(by_profile.items())},
        "by_week": [
            {"week_starting": w, **summarise(by_week[w])}
            for w in sorted(by_week)[-12:]
        ],
    }

    if INCLUDE_GROUP_NAMES:
        per_group = collections.defaultdict(list)
        for r in posted:
            per_group[r.get("group_id")].append(r)
        ranked = sorted(per_group.items(), key=lambda kv: -len(kv[1]))[:15]
        stats["top_groups"] = [
            {"name": (gmap.get(gid) or {}).get("name", "?"), **summarise(rows)}
            for gid, rows in ranked
        ]

    with open("posting-stats.json", "w") as f:
        json.dump(stats, f, indent=1)
    print(f"Wrote posting-stats.json — {stats['all_time']['posts']} posts across {stats['groups']['total']} groups")

except Exception as e:
    # Never fail the events job because the stats add-on had a bad day
    print("Posting stats skipped:", e)

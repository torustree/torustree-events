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

# Fetch upcoming events with locations included.
# Bookwhen paginates at a fixed 20 per page and offers no page[size] override;
# its `links` object carries only `self`, never `next`, so pagination has to be
# driven by an explicit page[offset] walk. A short page means we are done.
PAGE = 20
events, included = [], {}
offset = 0
while True:
    data = get(f"{BASE}/events?filter[from]={today}&include=location&page[offset]={offset}")
    batch = data.get("data", [])
    events.extend(batch)
    for inc in data.get("included", []):
        included[(inc["type"], inc["id"])] = inc
    if len(batch) < PAGE:
        break
    offset += PAGE
    if offset >= 2000:
        print(f"Warning: stopped paginating at offset {offset}", file=sys.stderr)
        break

out = []
for ev in events:
    a = ev.get("attributes", {})
    loc = ev.get("relationships", {}).get("location", {}).get("data") or {}
    # Trust the type the relationship declares ("location", singular) rather
    # than hardcoding it — a hardcoded "locations" silently missed every match.
    loc_obj = included.get((loc.get("type"), loc.get("id")), {})
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

# Below this many posts a rate is printed but flagged as too thin to act on.
MIN_N = 15


def sb(path):
    """Read-only Supabase query. Doubles as the free-tier keep-alive ping."""
    req = urllib.request.Request(
        f"{SB_URL}/{path}",
        headers={"apikey": SB_KEY, "Authorization": "Bearer " + SB_KEY},
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)


def sb_optional(path):
    """Same as sb() but a missing table/column is not fatal."""
    try:
        return sb(path)
    except Exception as e:
        print(f"  optional query skipped ({path.split('?')[0]}): {e}")
        return None


def post_entries(row):
    """campaigns.posts is a jsonb array of {"ts": "...Z", ...} — newest last."""
    return [p for p in (row.get("posts") or []) if isinstance(p, dict) and p.get("ts")]


def rate(part, whole):
    return round(part / whole, 3) if whole else None


def num(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def summarise(rows):
    c = collections.Counter(r.get("status") or "unknown" for r in rows)
    n = len(rows)
    out = {
        "posts": n,
        "live": c.get("live", 0),
        "pending": c.get("pending", 0),
        "declined": c.get("declined", 0),
        "other": n - c.get("live", 0) - c.get("pending", 0) - c.get("declined", 0),
        "booking_links": sum(1 for r in rows if r.get("link_added")),
        "live_rate": rate(c.get("live", 0), n),
        "booking_link_rate": rate(sum(1 for r in rows if r.get("link_added")), n),
        "thin_sample": n < MIN_N,
    }
    # Comment counts only if the tracker records them on the row.
    comments = [num(r.get(COMMENT_FIELD)) for r in rows] if COMMENT_FIELD else []
    comments = [x for x in comments if x is not None]
    if comments:
        out["comments_total"] = sum(comments)
        out["comments_per_post"] = round(sum(comments) / len(comments), 2)
        out["comments_n"] = len(comments)
    kw = [num(r.get(KEYWORD_FIELD)) for r in rows] if KEYWORD_FIELD else []
    kw = [x for x in kw if x is not None]
    if kw:
        out["keyword_comments_total"] = sum(kw)
        out["keyword_comments_n"] = len(kw)
    return out


def first_key(keys, candidates):
    for c in candidates:
        if c in keys:
            return c
    return None


# Group type is not stored in the tracker, so it is inferred from the group
# NAME here. Names never leave this script — only the bucket counts do.
GROUP_TYPE_RULES = [
    ("buy_sell", ["for sale", "selling", "buy", "sell ", "swap", "marketplace", "items for sale", "facebay", "free advertising"]),
    ("billboard", ["billboard", "notice board", "noticeboard", "message board", "blether"]),
    ("business", ["business", "networking", "directory", "high street", "trades", "local business"]),
    ("whats_on_events", ["what's on", "whats on", "what's going on", "whats going on", "events", "things to do"]),
    ("wellbeing", ["wellbeing", "well-being", "wellness", "mental health", "holistic", "spiritual", "yoga", "mums", "moms"]),
]


def group_type(name):
    n = (name or "").lower()
    for bucket, words in GROUP_TYPE_RULES:
        if any(w in n for w in words):
            return bucket
    return "community"


COMMENT_FIELD = None
KEYWORD_FIELD = None

stats_error = None
try:
    campaigns = sb("campaigns?select=*&limit=5000")
    groups = sb("groups?select=*&limit=5000")
    templates = sb_optional("templates?select=*&limit=2000") or []
    sessions = sb_optional("sessions?select=*&limit=2000") or []
    # session_images holds multi-MB base64 — never select=* on it.
    images = sb_optional("session_images?select=session_id,orientation,width,height,filename,updated_at&limit=2000")
    if images is None:
        images = sb_optional("session_images?select=session_id,updated_at&limit=2000") or []

    camp_cols = sorted({k for r in campaigns for k in r.keys()})
    post_keys = sorted({k for r in campaigns for p in post_entries(r) for k in p.keys()})
    COMMENT_FIELD = first_key(camp_cols, ["comments", "comment_count", "comments_count"])
    KEYWORD_FIELD = first_key(camp_cols, ["keyword_comments", "breathe_count", "breathe_comments"])
    tpl_col = first_key(camp_cols, ["template_id", "template", "template_name", "tpl"])
    tpl_post_key = first_key(post_keys, ["template_id", "template", "tpl", "template_name"])
    img_col = first_key(camp_cols, ["image_id", "image", "image_name", "photo"])
    img_post_key = first_key(post_keys, ["image_id", "image", "photo", "image_name"])

    gmap = {g["id"]: g for g in groups}
    tmap = {t.get("id"): t for t in templates}
    smap = {s.get("id"): s for s in sessions}
    imap = {i.get("session_id"): i for i in images}

    now = datetime.datetime.now(datetime.timezone.utc)
    cutoff = now - datetime.timedelta(days=30)

    posted, latest_seen = [], ""
    for r in campaigns:
        entries = post_entries(r)
        if not entries:
            continue          # a row that was never actually posted
        stamps = sorted(p["ts"] for p in entries)
        r["_last"] = stamps[-1]
        r["_first"] = stamps[0]
        r["_last_entry"] = max(entries, key=lambda p: p["ts"])
        latest_seen = max(latest_seen, stamps[-1], r.get("updated_at") or "")
        posted.append(r)

    def within_30(r):
        try:
            t = datetime.datetime.fromisoformat(r["_last"].replace("Z", "+00:00"))
        except Exception:
            return False
        return t >= cutoff

    recent = [r for r in posted if within_30(r)]

    def bucket(rows, keyfn):
        d = collections.defaultdict(list)
        for r in rows:
            d[keyfn(r)].append(r)
        return {k: summarise(v) for k, v in sorted(d.items(), key=lambda kv: (-len(kv[1]), str(kv[0])))}

    def loc_of(r):
        locs = (gmap.get(r.get("group_id")) or {}).get("locs") or []
        return locs[0] if locs else "Unassigned"

    def profile_of(r):
        return (gmap.get(r.get("group_id")) or {}).get("post_as") or "Unknown"

    def template_of(r):
        tid = r.get(tpl_col) if tpl_col else None
        if tid in (None, "") and tpl_post_key:
            tid = r["_last_entry"].get(tpl_post_key)
        if tid in (None, ""):
            return "(not recorded)"
        t = tmap.get(tid) or {}
        return t.get("name") or t.get("title") or str(tid)

    def session_of(r):
        s = smap.get(r.get("session_id")) or {}
        label = s.get("name") or s.get("title") or s.get("loc") or ""
        date = s.get("date") or s.get("event_date") or ""
        return (f"{label} {date}".strip()) or str(r.get("session_id") or "(none)")

    def image_of(r):
        iid = r.get(img_col) if img_col else None
        if iid in (None, "") and img_post_key:
            iid = r["_last_entry"].get(img_post_key)
        if iid not in (None, ""):
            return str(iid)
        # Fallback: the tracker stores ONE image per session card, overwritten
        # when it changes. So this is "the session's current image", a proxy.
        return "session:" + session_of(r)

    def orientation_of(r):
        img = imap.get(r.get("session_id")) or {}
        if img.get("orientation"):
            return img["orientation"]
        w, h = num(img.get("width")), num(img.get("height"))
        if w and h:
            return "portrait" if h > w else "landscape" if w > h else "square"
        return "unknown"

    by_image = {}
    for k, v in bucket(posted, image_of).items():
        sample = next(r for r in posted if image_of(r) == k)
        by_image[k] = {"orientation": orientation_of(sample), **v}

    by_week = collections.defaultdict(list)
    for r in posted:
        try:
            d = datetime.datetime.fromisoformat(r["_last"].replace("Z", "+00:00")).date()
        except Exception:
            continue
        by_week[(d - datetime.timedelta(days=d.weekday())).isoformat()].append(r)

    stats = {
        "generated": now.isoformat().replace("+00:00", "Z"),
        "latest_post_at": latest_seen or None,
        "note": "Aggregate Facebook group posting performance. No group names, no customer data. "
                "'posts' counts group x session rows; a repost into the same group for the same session is one row.",
        "min_n_for_rates": MIN_N,
        "groups": {
            "total": len(groups),
            "active": sum(1 for g in groups if not g.get("archived") and not g.get("blocked_reason")),
            "archived": sum(1 for g in groups if g.get("archived")),
            "blocked": sum(1 for g in groups if g.get("blocked_reason")),
            "by_type": dict(collections.Counter(group_type(g.get("name")) for g in groups)),
        },
        "all_time": summarise(posted),
        "last_30_days": summarise(recent),
        "by_location": bucket(posted, loc_of),
        "by_profile": bucket(posted, profile_of),
        "by_template": bucket(posted, template_of),
        "by_session": bucket(posted, session_of),
        "by_image": by_image,
        "by_orientation": bucket(posted, orientation_of),
        "by_group_type": bucket(posted, lambda r: group_type((gmap.get(r.get("group_id")) or {}).get("name"))),
        "by_week": [
            {"week_starting": w, **summarise(by_week[w])}
            for w in sorted(by_week)[-12:]
        ],
        "data_sources": {
            "template": f"campaigns.{tpl_col}" if tpl_col else (f"campaigns.posts[].{tpl_post_key}" if tpl_post_key else "NOT RECORDED by tracker"),
            "image": f"campaigns.{img_col}" if img_col else (f"campaigns.posts[].{img_post_key}" if img_post_key else "session card image (one per session, overwritten on change) — proxy only"),
            "group_type": "inferred from group name keywords (" + ", ".join(b for b, _ in GROUP_TYPE_RULES) + ", else community)",
            "comments": f"campaigns.{COMMENT_FIELD}" if COMMENT_FIELD else "NOT RECORDED by tracker",
            "keyword_comments": f"campaigns.{KEYWORD_FIELD}" if KEYWORD_FIELD else "NOT RECORDED by tracker",
        },
        "schema": {
            "campaigns": camp_cols,
            "campaigns_posts_entry": post_keys,
            "groups": sorted({k for g in groups for k in g.keys()}),
            "templates": sorted({k for t in templates for k in t.keys()}),
            "sessions": sorted({k for s in sessions for k in s.keys()}),
        },
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
    stats_error = e
    print("POSTING STATS FAILED:", e, file=sys.stderr)

# events.json is already written above. Exit non-zero AFTER that so the commit
# step can still run (if: always()) but the run shows red and GitHub emails.
if stats_error:
    sys.exit(1)

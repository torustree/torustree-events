#!/usr/bin/env python3
"""Nightly snapshot of live torustree.com pages into site-archive/."""

import os, time, urllib.request

os.makedirs("site-archive", exist_ok=True)

with open("pages.txt") as f:
    urls = [l.strip() for l in f if l.strip() and not l.startswith("#")]

ok = fail = 0

for url in urls:
    slug = url.rstrip("/").split("/")[-1] or "homepage"
    try:
        req = urllib.request.Request(url + ("?" if "?" not in url else "&") + "v=snapshot",
                                     headers={"User-Agent": "TorusTree-Archive-Bot/1.0"})
        with urllib.request.urlopen(req, timeout=30) as r:
            html = r.read().decode("utf-8", errors="replace")
        with open(f"site-archive/{slug}.html", "w") as f:
            f.write(html)
        ok += 1
    except Exception as e:
        print(f"FAIL {slug}: {e}")
        fail += 1
    time.sleep(1)  # be polite to Zoho

print(f"Snapshot complete: {ok} saved, {fail} failed")
if ok == 0:
    raise SystemExit("All fetches failed — aborting so we don't commit an empty archive")

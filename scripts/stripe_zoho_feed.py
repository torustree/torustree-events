#!/usr/bin/env python3
"""
Torus Tree - Stripe to Zoho CRM booking feed.

Polls Stripe for recent charges and writes PROVISIONAL booking rows into the
Zoho CRM Bookings module. The Monday sync remains canonical and adopts these
rows into per-attendee rows, deleting the provisional row as it goes.

DESIGN RULES (do not relax without re-reading the spec):

  1. NO CUSTOMER DATA IS EVER LOGGED. This runs in a PUBLIC GitHub repo, so
     every log line is world-readable. Payloads, names, emails, addresses and
     booking slugs never reach stdout. Exceptions are reduced to a type name
     before printing - a raw traceback can contain a whole charge object.

  2. CONTACTS ARE MATCHED, NEVER CREATED OR UPDATED. Two workflow rules fire on
     Contact create/edit. Unmatched bookings are written with the email stored
     and the Contact link empty, to be swept later.

  3. FIELDS ARE VERIFIED BEFORE ANY WRITE. Zoho silently accepts writes to
     fields that do not exist - HTTP 200, data dropped. This cost ~240 records
     their EventIDs in August 2026. If a target field is missing, we abort.

  4. IDEMPOTENT BY CONSTRAINT, NOT BY CURSOR. We re-scan a lookback window every
     run and let the unique constraint on Bookwhen_Booking_ID reject repeats.
     There is no cursor file to corrupt or fall behind.

Environment:
  STRIPE_RESTRICTED_KEY   Stripe restricted key, read-only on Charges
  ZOHO_CLIENT_ID          Zoho OAuth client id
  ZOHO_CLIENT_SECRET      Zoho OAuth client secret
  ZOHO_REFRESH_TOKEN      Zoho OAuth refresh token
  DRY_RUN                 "true" to plan writes without making them (default true)
  LOOKBACK_DAYS           How far back to scan Stripe (default 7)
"""

from __future__ import annotations

import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any

# Zoho EU data centre. This CRM is NOT on .com - using .com fails auth.
ZOHO_ACCOUNTS = "https://accounts.zoho.eu"
ZOHO_API = "https://www.zohoapis.eu/crm/v8"
STRIPE_API = "https://api.stripe.com/v1"
EVENTS_FEED = (
    "https://raw.githubusercontent.com/torustree/torustree-events/main/events.json"
)

STRIPE_ACCOUNT_TAG = "torustree"

# Every field this script writes. Verified present before the first write.
REQUIRED_BOOKING_FIELDS = [
    "Name",
    "Bookwhen_Booking_ID",
    "Stripe_Charge_ID",
    "Email",
    "Attendee_Email",
    "Amount_Paid",
    "Booked_On",
    "Event_Title",
    "Event_Date",
    "Venue",
    "Location",
    "Ticket_Count",
    "Attendee_Names",
    "Payment_Method",
    "Refunded_Amount",
    "Booking_Status",
    "Bookwhen_Event_ID",
    "Event_Code",
    "Source_System",
    "Contact",
    "Session",
]

# Title venue token -> Venue picklist value. The picklist is regional; the exact
# hall goes in Location. Confirmed against live data 24 Aug 2026.
VENUE_MAP = {
    "DUNKELD": "Dunkeld",
    "PERTH": "Perth",
    "DUNDEE": "Dundee",
    "STIRLING": "Stirling",
    "GRANGEMOUTH": "Falkirk",
    "FALKIRK": "Falkirk",
    "DUNFERMLINE": "Fife",
    "CUPAR": "Fife",
    "INVERKEITHING": "Fife",
    "FIFE": "Fife",
    "FORFAR": "Forfar",
    "FOFAR": "Forfar",  # misspelled at source in Bookwhen; 24 records affected
    "PITLOCHRY": "Pitlochry",
    "ONLINE": "Online",
}

PAYMENT_METHOD_MAP = {"card": "Card", "klarna": "Klarna", "link": "Link"}

# "Letting Go" and "Letting Go and Moving On" are one journey. Explicit table,
# never fuzzy matching.
THEME_ALIASES = {
    "letting go and moving on": "Letting Go",
    "letting go": "Letting Go",
}

DRY_RUN = os.environ.get("DRY_RUN", "true").lower() != "false"
LOOKBACK_DAYS = int(os.environ.get("LOOKBACK_DAYS", "7"))

stats: Counter = Counter()


# --------------------------------------------------------------------------
# Logging - counts and categories only, never content
# --------------------------------------------------------------------------


def log(message: str) -> None:
    """Print a log line. Callers must never pass customer data."""
    print(f"[feed] {message}", flush=True)


# An error identifier, and nothing else: no spaces, no @, no punctuation beyond
# these, hard length cap. A name, email or address cannot satisfy this pattern.
SAFE_CODE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")


class SafeHTTPError(RuntimeError):
    """An HTTP failure reduced to parts that are safe on a public log.

    Carries only host, status and a machine-readable error identifier. The
    response body it came from is discarded beyond that one field.
    """

    def __init__(self, host: str, status: int | str, code: str | None = None) -> None:
        self.host = host
        self.status = status
        self.code = code
        detail = f" ({code})" if code else ""
        super().__init__(f"HTTP {status} from {host}{detail}")


def error_code(body: Any) -> str | None:
    """Pull ONLY a short error identifier out of an error body.

    Zoho and Stripe both return a machine-readable code next to a human
    message. The message, and every other field, may echo request detail, so
    a single whitelisted key is read and then pattern-checked before it is
    allowed anywhere near stdout.
    """
    if not isinstance(body, dict):
        return None
    for key in ("error", "code"):
        value = body.get(key)
        if isinstance(value, dict):  # Stripe nests: {"error": {"code": ...}}
            value = value.get("code") or value.get("type")
        if isinstance(value, str) and SAFE_CODE.match(value):
            return value
    return None


def safe_error(exc: BaseException) -> str:
    """Reduce an exception to something safe to publish.

    Exception *messages* can contain response bodies, which contain customer
    records, so only the class name is emitted - except for SafeHTTPError,
    which is built from whitelisted fields and carries no free text.
    """
    if isinstance(exc, SafeHTTPError):
        return f"{type(exc).__name__}: {exc}"
    return type(exc).__name__


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------


def http_request(
    url: str,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    body: bytes | None = None,
    retries: int = 3,
) -> Any:
    """JSON HTTP request with retries. Never logs response bodies."""
    last_status = None
    for attempt in range(retries):
        req = urllib.request.Request(
            url, method=method, headers=headers or {}, data=body
        )
        try:
            with urllib.request.urlopen(req, timeout=45) as resp:
                raw = resp.read()
                # 204 is COQL's documented "no rows matched" and is the ONLY
                # empty response we accept. Treating any empty body as {} would
                # extend that trust to the write path, where an empty response
                # is anomalous and must fail loudly - the same silent-success
                # trap that cost ~240 records their EventIDs in August 2026.
                if resp.status == 204:
                    return {}
                return json.loads(raw.decode("utf-8"))
        except urllib.error.HTTPError as exc:
            last_status = exc.code
            # 4xx other than rate limiting will not improve on retry.
            if exc.code == 429 or exc.code >= 500:
                time.sleep(2 ** attempt)
                continue
            try:
                detail = error_code(json.loads(exc.read().decode("utf-8")))
            except Exception:  # noqa: BLE001 - a body we cannot parse tells us nothing
                detail = None
            raise SafeHTTPError(urllib.parse.urlparse(url).netloc, exc.code, detail)
        except (urllib.error.URLError, TimeoutError) as exc:
            if attempt == retries - 1:
                raise RuntimeError(f"network failure: {safe_error(exc)}")
            time.sleep(2 ** attempt)
    raise RuntimeError(f"request failed after {retries} attempts (last status {last_status})")


# --------------------------------------------------------------------------
# Auth
# --------------------------------------------------------------------------


def zoho_access_token() -> str:
    payload = urllib.parse.urlencode(
        {
            "refresh_token": os.environ["ZOHO_REFRESH_TOKEN"],
            "client_id": os.environ["ZOHO_CLIENT_ID"],
            "client_secret": os.environ["ZOHO_CLIENT_SECRET"],
            "grant_type": "refresh_token",
        }
    ).encode()
    data = http_request(
        f"{ZOHO_ACCOUNTS}/oauth/v2/token",
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        body=payload,
    )
    token = data.get("access_token")
    if not token:
        # Zoho answers a bad refresh with HTTP 200 and an error code in the
        # body, so this is the normal auth-failure path, not an edge case. The
        # body may echo request detail, so only the code field is surfaced.
        raise SafeHTTPError(
            urllib.parse.urlparse(ZOHO_ACCOUNTS).netloc,
            "200/no-token",
            error_code(data),
        )
    return token


# --------------------------------------------------------------------------
# Pre-flight
# --------------------------------------------------------------------------


def verify_fields(token: str) -> None:
    """Abort if any target field is missing.

    Zoho accepts writes to unknown fields with HTTP 200 and silently drops the
    data. Field names are structural, not customer data, so they are safe to log.
    """
    data = http_request(
        f"{ZOHO_API}/settings/fields?module=Bookings",
        headers={"Authorization": f"Zoho-oauthtoken {token}"},
    )
    present = {f["api_name"] for f in data.get("fields", [])}
    missing = [f for f in REQUIRED_BOOKING_FIELDS if f not in present]
    if missing:
        raise SystemExit(
            "ABORT - missing fields on Bookings: " + ", ".join(sorted(missing))
        )
    log(f"field check passed ({len(REQUIRED_BOOKING_FIELDS)} fields present)")


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------


def normalise_title(raw: str) -> str:
    """Unescape entities and collapse whitespace.

    Stripe sends 'Calm &amp; Reset' where events.json has 'Calm & Reset', and at
    least one live event title carries a double space before a pipe.
    """
    return re.sub(r"\s+", " ", html.unescape(raw or "")).strip()


def title_key(raw: str) -> str:
    """Comparison key for joining a Stripe title to an events.json title."""
    return normalise_title(raw).casefold()


def parse_title(title: str) -> tuple[str | None, str | None, str | None]:
    """Split 'TYPE | Theme | VENUE' into its parts.

    Returns (product_type, theme, venue_token). Any None means the title did not
    have the expected shape - the caller flags rather than guesses.
    """
    parts = [p.strip() for p in normalise_title(title).split("|")]
    if len(parts) != 3:
        return None, None, None
    product_type, theme, venue_token = parts
    theme = THEME_ALIASES.get(theme.casefold(), theme)
    return product_type, theme, venue_token


def slug_from_booking_url(url: str) -> str | None:
    match = re.search(r"/bookings/([a-z0-9]+)", url or "", re.IGNORECASE)
    return match.group(1) if match else None


def count_attendees(attendees: str) -> int:
    """Attendee names arrive hyphen-separated: 'colleen scott - Angela Spence'."""
    if not attendees:
        return 1
    names = [n.strip() for n in attendees.split(" - ") if n.strip()]
    return max(len(names), 1)


# --------------------------------------------------------------------------
# Sources
# --------------------------------------------------------------------------


def fetch_events() -> dict[str, dict[str, str]]:
    """Load events.json and key it by normalised title.

    Event ids look like 'ev-s32e1-20260826183000' - code and timestamp in one.
    Where a title recurs we keep every occurrence so the caller can pick the
    earliest event after the charge date.
    """
    data = http_request(EVENTS_FEED)
    by_title: dict[str, list[dict[str, str]]] = {}
    for event in data.get("events", []):
        key = title_key(event.get("title", ""))
        if not key:
            continue
        by_title.setdefault(key, []).append(event)
    log(f"events feed loaded ({sum(len(v) for v in by_title.values())} events)")
    return by_title


def fetch_charges(key: str) -> list[dict]:
    """All Torus Tree charges created within the lookback window."""
    since = int((datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)).timestamp())
    charges: list[dict] = []
    starting_after = None
    while True:
        params = {"limit": 100, "created[gte]": since}
        if starting_after:
            params["starting_after"] = starting_after
        data = http_request(
            f"{STRIPE_API}/charges?{urllib.parse.urlencode(params)}",
            headers={"Authorization": f"Bearer {key}"},
        )
        batch = data.get("data", [])
        charges.extend(batch)
        if not data.get("has_more") or not batch:
            break
        starting_after = batch[-1]["id"]
    torus = [
        c
        for c in charges
        if (c.get("metadata") or {}).get("account") == STRIPE_ACCOUNT_TAG
        and c.get("status") == "succeeded"
    ]
    log(f"stripe: {len(charges)} charges in window, {len(torus)} Torus Tree")
    return torus


# --------------------------------------------------------------------------
# Zoho reads
# --------------------------------------------------------------------------


def coql(token: str, query: str) -> list[dict]:
    """Run a COQL query.

    No failure is swallowed here. "No rows matched" arrives as a 204, which
    http_request turns into {}; anything else is a genuine failure and must
    surface. The handler that used to catch RuntimeError and return [] made an
    erroring query indistinguishable from an empty one - exactly the silent
    success spec section 8 warns about.
    """
    body = json.dumps({"select_query": query}).encode()
    data = http_request(
        f"{ZOHO_API}/coql",
        method="POST",
        headers={
            "Authorization": f"Zoho-oauthtoken {token}",
            "Content-Type": "application/json",
        },
        body=body,
    )
    return data.get("data", []) or []


def find_contact_id(token: str, email: str) -> str | None:
    """Match an existing Contact. NEVER creates or edits one."""
    safe = email.replace("'", "")
    rows = coql(
        token,
        "select id from Contacts where Email = '{0}' limit 1".format(safe),
    )
    if rows:
        return rows[0]["id"]
    rows = coql(
        token,
        "select id from Contacts where Secondary_Email = '{0}' limit 1".format(safe),
    )
    return rows[0]["id"] if rows else None


def find_session_id(token: str, event_id: str) -> str | None:
    """Find the Session for an events.json event id.

    Sessions holds the two id formats in two different fields, and the names
    are the wrong way round for intuition:

      Bookwhen_Event_ID  bare 12-char slug     'jnayyqp7237z'
      Event_Code         composite id          'ev-spsfw-20260912093000'

    events.json ids are the composite form, so the join is on Event_Code.
    Matching against Bookwhen_Event_ID can never succeed - it produced
    no_session_record on 54 of 54 rows before this was corrected.
    """
    rows = coql(
        token,
        "select id from Sessions where Event_Code = '{0}' limit 1".format(event_id),
    )
    return rows[0]["id"] if rows else None


def existing_slugs(token: str, slugs: list[str]) -> set[str]:
    """Which slugs already have a provisional row.

    Exact match only. A LIKE 'slug%' would also match canonical per-attendee
    rows, whose keys begin with the same slug.
    """
    found: set[str] = set()
    for i in range(0, len(slugs), 50):
        chunk = [s.replace("'", "") for s in slugs[i : i + 50]]
        values = ",".join(f"'{s}'" for s in chunk)
        rows = coql(
            token,
            "select Bookwhen_Booking_ID from Bookings "
            "where Bookwhen_Booking_ID in ({0}) limit 200".format(values),
        )
        found.update(r["Bookwhen_Booking_ID"] for r in rows if r.get("Bookwhen_Booking_ID"))
    return found


# --------------------------------------------------------------------------
# Transform
# --------------------------------------------------------------------------


def build_row(charge: dict, events: dict, token: str) -> dict | None:
    """Turn a Stripe charge into a provisional Bookings row."""
    meta = charge.get("metadata") or {}
    billing = charge.get("billing_details") or {}

    slug = slug_from_booking_url(meta.get("booking", ""))
    if not slug:
        stats["skipped_no_slug"] += 1
        return None

    email = (billing.get("email") or "").strip().lower()
    if not email:
        stats["skipped_no_email"] += 1
        return None

    raw_title = meta.get("events", "")
    product_type, theme, venue_token = parse_title(raw_title)
    if venue_token is None:
        stats["title_unparsed"] += 1

    booked_on = datetime.fromtimestamp(charge["created"], tz=timezone.utc)

    row: dict[str, Any] = {
        "Name": (meta.get("ref") or "").lower(),
        # Provisional rows carry the BARE slug. Canonical rows written by the
        # Monday sync are 'slug|email|date', so the two never collide, and the
        # unique constraint stops us re-writing the same booking on later polls.
        "Bookwhen_Booking_ID": slug,
        "Stripe_Charge_ID": charge["id"],
        "Email": email,
        "Attendee_Email": email,
        "Amount_Paid": round(charge.get("amount", 0) / 100, 2),
        "Refunded_Amount": round(charge.get("amount_refunded", 0) / 100, 2),
        "Booked_On": booked_on.astimezone().strftime("%Y-%m-%d"),
        "Event_Title": normalise_title(raw_title),
        "Ticket_Count": count_attendees(meta.get("attendees", "")),
        "Attendee_Names": (meta.get("attendees") or "")[:255],
        "Payment_Method": PAYMENT_METHOD_MAP.get(
            ((charge.get("payment_method_details") or {}).get("type") or ""), "Other"
        ),
        "Booking_Status": "Complete",
        "Source_System": "Stripe Feed",
    }

    if venue_token:
        row["Location"] = venue_token[:100]
        row["Venue"] = VENUE_MAP.get(venue_token.upper(), "Other")
        if row["Venue"] == "Other":
            stats["venue_unmapped"] += 1

    # Join the event. Past events drop out of the feed, so misses are expected
    # on late runs and are left null for adoption to fill.
    matches = events.get(title_key(raw_title), [])
    future = sorted(
        (e for e in matches if e.get("start", "") >= booked_on.strftime("%Y-%m-%d")),
        key=lambda e: e.get("start", ""),
    )
    chosen = future[0] if future else None
    if chosen:
        event_id = chosen["id"]
        row["Bookwhen_Event_ID"] = event_id
        row["Event_Code"] = "-".join(event_id.split("-")[:2])
        row["Event_Date"] = chosen["start"][:10]
        session_id = find_session_id(token, event_id)
        if session_id:
            row["Session"] = {"id": session_id}
        else:
            stats["no_session_record"] += 1
        stats["event_matched"] += 1
    else:
        stats["event_unmatched"] += 1

    contact_id = find_contact_id(token, email)
    if contact_id:
        row["Contact"] = {"id": contact_id}
        stats["contact_matched"] += 1
    else:
        stats["contact_unmatched"] += 1

    return row


# --------------------------------------------------------------------------
# Write
# --------------------------------------------------------------------------


def write_rows(token: str, rows: list[dict]) -> None:
    for i in range(0, len(rows), 100):
        batch = rows[i : i + 100]
        body = json.dumps({"data": batch, "trigger": []}).encode()
        data = http_request(
            f"{ZOHO_API}/Bookings",
            method="POST",
            headers={
                "Authorization": f"Zoho-oauthtoken {token}",
                "Content-Type": "application/json",
            },
            body=body,
        )
        for result in data.get("data", []):
            code = result.get("code")
            if code == "SUCCESS":
                stats["written"] += 1
            elif code == "DUPLICATE_DATA":
                # Expected: the unique constraint doing its job.
                stats["duplicate_skipped"] += 1
            else:
                stats[f"write_error_{code}"] += 1


def verify_written(token: str, slugs: list[str]) -> None:
    """Read back rather than trusting the write response."""
    found = existing_slugs(token, slugs)
    stats["verified_present"] = len(found)
    if len(found) != len(slugs):
        stats["verify_missing"] = len(slugs) - len(found)


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------


def main() -> int:
    log(f"start - dry_run={DRY_RUN} lookback_days={LOOKBACK_DAYS}")

    stripe_key = os.environ["STRIPE_RESTRICTED_KEY"]
    token = zoho_access_token()
    # Marks which side of the auth boundary a later failure fell on.
    log("zoho auth ok")
    verify_fields(token)

    events = fetch_events()
    charges = fetch_charges(stripe_key)
    if not charges:
        log("nothing to do")
        return 0

    rows: list[dict] = []
    for charge in charges:
        try:
            row = build_row(charge, events, token)
        except Exception as exc:  # noqa: BLE001 - message may contain a payload
            stats[f"build_error_{safe_error(exc)}"] += 1
            continue
        if row:
            rows.append(row)

    # Drop anything already present so the log distinguishes "new" from
    # "already had it" rather than reporting a wall of duplicate errors.
    slugs = [r["Bookwhen_Booking_ID"] for r in rows]
    already = existing_slugs(token, slugs)
    fresh = [r for r in rows if r["Bookwhen_Booking_ID"] not in already]
    stats["already_present"] = len(rows) - len(fresh)

    # Refunds on bookings already adopted by the Monday sync have no provisional
    # row to update. The feed does not write money onto rows it does not own, and
    # splitting a partial refund across attendees would be a guess.
    for row in rows:
        if row["Refunded_Amount"] > 0 and row["Bookwhen_Booking_ID"] in already:
            stats["refund_needs_review"] += 1

    if DRY_RUN:
        log(f"DRY RUN - would write {len(fresh)} rows")
    else:
        write_rows(token, fresh)
        if fresh:
            verify_written(token, [r["Bookwhen_Booking_ID"] for r in fresh])

    log("--- run summary ---")
    for key in sorted(stats):
        log(f"{key}: {stats[key]}")

    failures = sum(v for k, v in stats.items() if k.startswith(("write_error", "build_error")))
    if stats.get("verify_missing"):
        log("WARNING: some rows reported success but were not found on read-back")
        return 1
    return 1 if failures else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001
        # A raw traceback here would be published to a public build log and can
        # contain a full charge object. Only the exception type escapes.
        log(f"FATAL: {safe_error(exc)}")
        sys.exit(1)

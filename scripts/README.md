# Stripe → Zoho booking feed — runbook

Operational notes for `stripe_zoho_feed.py` and `.github/workflows/stripe-feed.yml`.
Design rules live in the script's module docstring; this file is the state of
play and the things that have bitten us.

## Current status

| | |
|---|---|
| Write path | Proven live. 3 rows 2026-08-24, 10 rows 2026-08-27, all verified on read-back |
| Scheduled runs | **Dry-run**, deliberately. `stripe-feed.yml:53` not yet flipped |
| Zoho token scopes | `custom.CREATE`, `custom.READ`, `Contacts.READ`, `settings.fields.READ`, `coql.READ` |

### Why the schedule is still in dry-run

Not because of the feed — that side is proven. Because of **adoption**. The
Monday sync's delete-on-adopt step has never run, and it issues DELETE against
a module holding ~1,128 records. Until one adoption cycle has been watched and
confirmed to remove provisional rows with nothing canonical going with them,
scheduled writes would accumulate rows that nothing has yet demonstrated it can
clean up.

Plan: watch a Monday cycle deliberately, verify, then flip line 53.

## Scheduled cloud routines

**Email reply drafter** — `trig_018trkTG3Sp1BBE9Do69P4ft`
<https://claude.ai/code/routines/trig_018trkTG3Sp1BBE9Do69P4ft>

Runs `0 6,17 * * *`, model `claude-opus-5`, no repo attached, Zoho-CRM
connector only. Reads Notes titled `Email reply received` from the last 24
hours and writes a `DRAFT REPLY` Note on the parent Contact.

**Currently PAUSED.** See the tool-restriction finding below.

### The toolset restriction is NOT enforced by the harness

An earlier version of this file claimed it was. That was wrong, and both
configuration routes were tested against live runs:

| Attempted restriction | Result |
|---|---|
| `session_context.allowed_tools`, seven `mcp__Zoho-CRM__*` names | Did not restrict. `ToolSearch` still returned `updateRelatedRecords` and `deleteNotesModule`, and the agent also called `Bash` and `PushNotification`, none of which were listed |
| `mcp_connections[].permitted_tools`, seven bare tool names | Did not restrict either. `ToolSearch` still returned `deleteNotesModule` and `updateNotesModule` |

Both were accepted by the API and echoed back in the routine config, so the
settings look applied while changing nothing observable about what the agent
can see.

What is still untested is whether *calling* an excluded tool would be denied.
Discovery is clearly not blocked; invocation may or may not be. Nothing has
tried, because the prompt forbids it, and deliberately provoking a write on
live CRM data is not a test worth running.

**So the HARD RULES in the prompt are the only thing keeping this agent to a
single write.** They have held on every run so far, and the agent reports
restrictions it believes it is under rather than working around them. But
treat the protection as instruction-level, not structural, and write the
prompt accordingly. Do not assume a future config flag has closed this
without re-reading a run log to confirm.

### ACTION REQUIRED 25 October 2026 — BST ends

Routine cron expressions are **UTC and do not follow British Summer Time**.
`0 6,17` is 07:00 and 18:00 local while BST is in effect. When the clocks go
back on **Sun 25 Oct 2026** those runs become **06:00 and 17:00 local** and
stay there until BST resumes in March.

To hold 07:00/18:00 local through winter, change the cron to `0 7,18 * * *`
on or after 25 October, and change it back in spring. This is not a reminder
sitting with anyone — it is written down here because nothing will prompt it.

The same applies to any future routine. Anything scheduled in UTC drifts by
an hour twice a year relative to the working day.

## Known gaps

**`Ticket_Type` is null on every Stripe Feed row.** Stripe metadata does not
carry it. Harmless while provisional rows are transient, but it means the
group-use ticket split bug — 2-for-1 tickets writing full price per attendee —
is detectable only from canonical rows, never from provisional ones. Do not
expect a provisional row to reveal it.

**Multi-attendee path: exercised 2026-08-27.** Previously untested. Of the 13
Stripe Feed rows now in the CRM, 7 carry `Ticket_Count = 2`, and they behave as
designed: the full charge amount lands on one provisional row, with the split
left to the sync. Worth knowing what the amounts look like, because they are not
uniform — at `ev-szyyz` a single ticket is £25 and a pair is also £25 (2-for-1),
while at `ev-swxhw` a single is £40 and pairs appear at £80, £72 and £20. The
provisional row is correct in every case; the per-attendee split is the sync's
problem, and this is exactly where the group-use bug hides.

**One row written with no event linkage.** `Booked_On 2026-08-25`, £120,
`Event_Code`/`Venue`/`Location` all null — an unparseable title plus no event
match. The script flags rather than guesses, which is correct, but such rows
need the sweep.

## Token scopes are frozen at grant time

The refresh token can create and read; it cannot **update** or **delete**. A row
written with a wrong value cannot be corrected by this script. Widening scope
means a fresh Self Client grant code and a new refresh token — the old one does
not gain permissions.

The refund path is deferred for this reason: `main()` counts
`refund_needs_review` but does not write, because posting a refund onto an
adopted row is an UPDATE.

## Writing secrets from PowerShell — the BOM trap

**Two separate BOM incidents in one day, both from PowerShell encoding
defaults. Assume this will happen again.**

1. `gh secret set` via `Process.StandardInput` prefixed every value with a UTF-8
   BOM (`efbbbf`). .NET sets `AutoFlush` when the property is first accessed,
   which emits the encoding preamble before any of your bytes. All three Zoho
   secrets were silently corrupted; Zoho answered `general_error` and it cost
   two debugging rounds, because the values tested fine locally and only the
   stored copies were wrong.
2. `Set-Content -Encoding utf8` put a BOM on a git commit subject line.

Rules:

- Before writing a secret through stdin, set
  `[Console]::InputEncoding = New-Object Text.UTF8Encoding($false)` **before**
  starting the process. Console encoding does not persist between sessions.
- **Byte-verify the write path, not the value.** Push the value through the same
  writer into `python -c "import sys;sys.stdout.write(sys.stdin.buffer.read().hex())"`
  and compare against the expected ASCII hex. A secret cannot be read back out of
  GitHub, so the write path is the only thing you can actually check.
- Use the `Write` tool for commit-message files, not `Set-Content`.
- Do not "fix" the trailing-newline problem by switching to a raw stdin writer
  without checking the leading bytes — that trade is how incident 1 happened.

## Zoho gotchas worth remembering

- **EU data centre.** `accounts.zoho.eu` / `www.zohoapis.eu`. `.com` fails auth.
- **Token refresh takes body params, not query string.** Query string returns
  `invalid_client`. Verified both ways.
- **A bad refresh returns HTTP 200** with an error code in the body, not a 4xx.
- **COQL answers "no rows matched" with 204 No Content**, so `json.loads("")`
  raises. Handled in `http_request`; keyed on the 204 status specifically, never
  on "body is empty", because an empty body from `POST /Bookings` is anomalous
  and must fail loudly.
- **Timestamps.** `events.json` ids spell the composite code in local time;
  Bookings and Sessions both store it in **UTC**. Under BST they differ by an
  hour. See `session_event_code`.
- **Field names read backwards.** In both Bookings and Sessions,
  `Bookwhen_Event_ID` is the bare 12-char slug and `Event_Code` is the full
  composite. `events.json` carries no bare slug at all — it comes from the
  Session record.

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

Runs `0 6,17 * * *`, model `claude-opus-5`, no repo attached. Reads Notes
titled `Email reply received` from the last 24 hours and writes a
`DRAFT REPLY` note on the parent Contact. **Enabled.**

It was created through the HTTP API, so it can only be changed there. It does
not appear in claude.ai's Scheduled tasks UI and cannot be edited from a
Cowork session.

### The CRM restriction: enforced, by OAuth scope

The connector is **Torus Tree — Notes Drafter (narrow)**, a custom server
built in the Zoho MCP console (`mcp.zoho.eu`, server name
`Torus-Tree-Notes-Drafter-narrow`). Creating a server there means selecting
its tools, and the OAuth scope string is derived from that selection, so
Zoho refuses anything outside it server-side. Same principle as the Stripe
feed's Self Client token: the write fails at the far end rather than relying
on the agent's restraint.

Its four tools, namespaced by the connector as
`mcp__Torus-Tree-Notes-Drafter-narrow__ZohoCRM_*`:

    executeCOQLQuery   getRecord   getNotes   createNotes

Verified 27 Aug against a live run: a broad `ToolSearch` for "zoho"
(max_results 25) returned exactly those four and nothing else, and a
deliberately hostile query, "update delete record modify remove", surfaced
only `getRecord` from Zoho. `updateRecord`, `deleteRecord`, `createRecords`
and the note mutation tools are genuinely absent.

The connect URL carries an API key and is a credential. **It is not recorded
here** — this repo is public. Get it from the Connect tab of that server in
the Zoho MCP console.

The broad `Zoho-CRM` connector stays connected for interactive chat work and
is deliberately NOT attached to this routine. The connectors page may show it
as "You started connecting to Zoho CRM but didn't finish"; that label is
stale. A live COQL read through it succeeded on 27 Aug. Do not re-authorise
it on the strength of that message.

### The sandbox restriction: NOT possible, and not for want of trying

Do not believe `allowed_tools` or `permitted_tools`. Both are accepted by the
API and echoed back in the routine config, so they look applied, and neither
restricts anything. Three live tests:

| Attempted | Result |
|---|---|
| `session_context.allowed_tools` = seven `mcp__Zoho-CRM__*` names | No effect. `ToolSearch` still returned `updateRelatedRecords`, `deleteNotesModule`; agent called `Bash` and `PushNotification`, neither listed |
| `mcp_connections[].permitted_tools` = seven bare names | No effect. `ToolSearch` still returned `deleteNotesModule`, `updateNotesModule` |
| `allowed_tools` = ToolSearch, TodoWrite, PushNotification + the 4 Zoho tools | No effect. `ToolSearch` still returned `WebFetch`, `WebSearch`, `CronCreate`, `CronDelete`, `CronList`, `Task*`, `NotebookEdit`. A direct `Bash: echo audit-probe` **ran and returned `audit-probe`** |

That last test settles it in both directions: not just discovery, invocation
too. Omitting the field entirely causes the server to substitute
`["preset:default", ...]`, so it is plainly read and plainly not applied.

**So the agent still has `Bash`, `WebFetch`, `WebSearch`, `Write` and the
Cron and Task families**, and no configuration available to us removes them.
The narrow connector scopes what it can do to the CRM; it does not scope the
agent. Those two things are easy to conflate and are not the same.

This matters here more than on most tasks: the agent's entire input is email
text written by people outside the business. A shell and an outbound HTTP
call are a poor pairing with untrusted input. The prompt therefore carries a
TOOL DISCIPLINE section forbidding those tools and an UNTRUSTED INPUT section
telling it to treat note contents strictly as data and to report, rather than
act on, anything resembling an instruction. **That is instruction-level
protection and should be trusted accordingly.** The `allowed_tools` list is
left in place as a statement of intent, not as a control.

If a future run log shows the agent using a shell or fetching a URL, that is
not a surprise to be debugged, it is this limitation showing.

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

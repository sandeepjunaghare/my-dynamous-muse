# `app/cadence/` — the three-touch state machine

Portal 244766495 is Sales Hub Starter: no sequences, and workflows capped at ~10 actions with no
branching (Spike 3). There is nowhere in HubSpot to keep a cadence's state, so this slice keeps it
(D4) — and keeps **only** that.

**We own the schedule; HubSpot owns the outcomes.** `cadence_state` holds which touch is due, which
cycle we are in, and whether the prospect is live or parked. Whether a touch happened and what was
said stay in HubSpot, and are re-read on every sync — never copied here. One home per fact (M4).

## The cadence

```
(1, call) → (1, voicemail) → (1, email) ──4 days──▶ (2, call) → (2, voicemail) → (2, email)
          ──4 days──▶ (3, call) → (3, voicemail) → (3, email) → parked
```

| Touch | HubSpot task | Due |
|---|---|---|
| first call | `CALL` | end of the enrolment day |
| voicemail | `CALL` | end of the day the call was done — same day |
| email | `EMAIL` | end of the day the voicemail was done — same day |
| next cycle's call | `CALL` | end of the day four days after the email |

"End of the day" is **23:59 America/Chicago** (`machine.CADENCE_TZ`). DFW is the only metro, so
this is a constant, not config; "same day" means the Dallas day, not the UTC one.

Every live prospect points at **exactly one open task** — the current touch — and the database
enforces it (`ck_cadence_state_live_has_task`). A touch is **overdue** when it is live and its due
date has passed.

**Nothing sends.** Each touch is a task: a request to a human. The email touch is an `EMAIL`-typed
task to *draft and send by hand* — creating it sends nothing, and no email leaves any domain from
this system until Spike 4 closes. No outreach copy is generated: who rewrites the opener is open
(PRD §9, E16), so the task body carries only the constraints from
`.claude/references/outreach-messaging.md`. No calling, no knocking — ever.

**Task creation is ungated** by the provenance write-gate, by design: a task is not a claim about a
prospect. That is what lets T13 adopt records with zero citable fields.

## The done-signal (D5)

A touch is done when **its task is complete, or a matching activity was logged on the contact after
the touch became current.** Ties break toward done.

| Touch | Matching activity |
|---|---|
| call | call · meeting · note |
| voicemail | call · note |
| email | email · note |

Notes count for every touch because that is how touches are logged today (E14: the 22 outreach
touches are NOTE objects; E17: no logged calls or emails).

Each row carries an **anchor** — `(anchor_at, anchor_ref)` — "evidence for the current touch
starts after here". Activities are totally ordered by `(hs_timestamp, "<type>:<id>")` and one counts
only when it sorts after the anchor. That gives three properties:

- **D5 is covered.** The anchor is the enrolment instant or the signal that closed the previous
  touch; both precede the current task's creation.
- **Ties toward done.** An activity at exactly `anchor_at` counts when the anchor came from a task
  completion or enrolment (`anchor_ref` is `NULL`, which sorts below every ref).
- **Each activity closes at most one touch.** The anchor moves to the activity it credits.

When a task is completed **and** a matching activity is logged, they are one act recorded twice —
the activity is consumed, so ticking the task and logging the call do not close two touches.

**Already done by hand advances rather than re-firing.** After a touch closes, the next touch is
checked against the same activities *before* any task is created for it; the walk continues until a
touch is not done, which gets one task, or the cadence ends and parks.

The anchor is the one column that looks like an outcome. It is not: it records no disposition and
no content, only the cursor that tells the policy which activity is new.

## Sync

`POST /cadence/sync` or `uv run lpe cadence sync`. Triggered **externally** — launchd now, cron on
the VPS later, T10's weekly run when it lands. No in-process scheduler, no Celery, no Redis.

Per live prospect: one batch read of all current tasks (100 per call), then the association walk and
a batch read per engagement type that has any. The plan is computed purely (`sync.plan_advance`),
then the one remote write — the next task — happens, then the row is updated and committed. So:

- **Idempotent by construction.** A task is created only when the position advances; a second sync
  with nothing new in HubSpot creates nothing.
- **A failed create changes nothing.** It is reported under `failures` and retried next sync. One
  prospect failing does not stop the others.
- **A deleted task is reported** under `missing_tasks`, not recreated. A logged activity can still
  close its touch.
- **A superseded open task** — its touch closed by a logged activity — is listed under
  `open_tasks_superseded` and **left alone**. This slice never writes to the founder's tasks.

`GET /cadence/overdue` / `uv run lpe cadence overdue` read only our own schedule and need no token.
Between syncs they can list a touch that has since been done; the next sync notices.

## Enrolment — the seam for T9 and T13

There is **no enrol route or CLI command**. `CadenceService.enrol()` is called by code:

- **T9** hands over a freshly promoted prospect with the defaults — cycle one, the call, due today,
  evidence counted from now.
- **T13** adopts the 22 hand-worked prospects by passing the position it **reconstructed** from
  logged activity (`start`), the anchor (`anchor_at`) and the due date (`due_at`). Cycle position is
  never reset, which is why a bare "enrol this contact" command would be wrong: it would start a
  prospect touched twice by hand back at touch one. `OutcomeReader` and `find_signal` are the pieces
  T13 replays history through.

A contact is enrolled at most once, live or parked (`uq_cadence_state_contact`).

## Decided out, for now

- **No exit on a reached conversation.** The machine runs three cycles; outcome-based branching was
  not in T11.
- **No escalation when a human touch is skipped** — it stays open and shows as overdue. PRD §9's
  *Autonomy, revisited* is still open.
- **No door touch** in the machine; the ticket's cadence is call, voicemail, email.

## Testing

Every HubSpot call in `tests/cadence/` goes through `FakeHubSpot`, a stateful fake built on T3's
`MockPortal`, which raises on any request it does not model. A hand-moved clock walks twelve days of
cadence in milliseconds. The transport reads are pinned against recorded fixtures in
`tests/promotion/test_reads.py`.

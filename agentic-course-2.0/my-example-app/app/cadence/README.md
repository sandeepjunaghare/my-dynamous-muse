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

**A tick pairs only with activity logged at or before it** (a tie counts, toward done). Anything
logged after the tick belongs to the next touch: tick the call at 10:00, log the voicemail at 10:05
and the email at 10:30, and all three touches close — nothing re-fires.

**A late tick does not bury earlier work.** A tick with nothing to pair with moves the anchor up to
the tick, but never past an activity nobody has credited yet. Voicemail ticked on Thursday, email
logged on Monday: the email still closes the email touch.

**Future-dated activity waits.** A meeting's `hs_timestamp` is when it starts, so one booked today
for next week closes nothing until next week. Any activity dated after the sync's clock is ignored
until it is in the past.

**The residual race, accepted.** Tick a task, then log the *same* act with a timestamp after the
tick, and that activity closes the next touch too — one touch early. That is the direction D5
breaks ties in, and the alternative (pairing across the tick) credited the wrong touch.

**Already done by hand advances rather than re-firing.** After a touch closes, the next touch is
checked against the same activities *before* any task is created for it; the walk continues until a
touch is not done, which gets one task, or the cadence ends and parks.

The anchor is the one column that looks like an outcome. It is not: it records no disposition and
no content, only the cursor that tells the policy which activity is new.

## Sync

**Run it daily, from an external launchd job** — separately from T10's weekly sourcing run, which
does not trigger it. Voicemail and email are same-day touches, and each one's task only exists once
a sync has seen the previous touch close; a weekly sync would create them up to a week overdue and
stretch a twelve-day cadence to about nine weeks. More often than daily is fine and cheap (about
eight reads per live prospect). No in-process scheduler, no Celery, no Redis.

A task is never due before the end of the day the sync creates it. A daily sync usually learns about
yesterday's call this morning; without that floor the voicemail task would be due yesterday — overdue
the moment it exists. The floor only ever moves a date later, so a sync that runs on time gets the
exact same-day / four-day schedule.

The command a launchd job runs — no server needed, from any working directory:

```
<path-to-uv> run --directory <path-to>/my-example-app lpe cadence sync
```

launchd starts with a bare `PATH`, so give `uv` by its absolute path (`which uv`), and put the
command in the plist's `ProgramArguments` with a `StartCalendarInterval`. It exits `0` on success
and when it skipped because another sync was running, and `1` when any prospect failed. With the
API up, `curl -fsS -X POST http://localhost:8000/cadence/sync` is the same operation.

Per live prospect: one batch read of all current tasks (100 per call), then the association walk and
a batch read per engagement type that has any. The plan is computed purely (`sync.plan_advance`),
then applied:

- **Nothing new, nothing created.** A task is created only when the position advances; a second
  sync with nothing new in HubSpot creates nothing.
- **An interrupted create is found, not repeated.** A create HubSpot executed but answered with a
  5xx or a timeout, or one whose commit never happened, is ambiguous, and the gateway never retries
  it. So the advance is committed *before* the create, as a pending intent (`pending_task_key`)
  carrying a deterministic key, `lpe-cadence:<contact>:<cycle>-<touch>`, which also ends the task
  body. The next sync looks for a task carrying that key among the contact's associated tasks (the
  associations read, not search, which lags) and adopts it (`pending_tasks_adopted`). Only if none
  exists does it create the task, once. Keep the `Ref:` line in the task body: it is the key.
- **One sync at a time.** A Postgres advisory lock (on its own connection, so it survives the
  per-prospect commits) is held for the run. An overlapping sync — the route and launchd at once —
  returns immediately with `skipped: true`.
- **One prospect failing does not stop the others.** A HubSpot, validation or database error rolls
  back what that prospect had not committed, is reported under `failures` with a code, and the sync
  carries on.
- **An associations body of an unknown shape is an error, not silence.** If HubSpot's associations
  answer has results but none carries `toObjectId` or `id`, the gateway logs
  `promotion.hubspot.associations_unrecognised` and the prospect fails with
  `hubspot_response_shape_unrecognised`, rather than every logged touch quietly disappearing.
- **A deleted task is reported** under `missing_tasks`, not recreated. A logged activity can still
  close its touch.
- **A superseded open task** — its touch closed by a logged activity — is listed under
  `open_tasks_superseded` and **left alone**. This slice never writes to the founder's tasks.

### Task owner

`HUBSPOT_DEFAULT_OWNER_ID` is the owner a task is assigned to when `enrol` is given none, so the
tasks land in a founder's "My tasks". Unset, tasks are created unassigned — nobody's queue, which is
how E15's 22 tasks went unworked — and every enrol and sync warns (`warnings` in the sync report).

### Metric M5

**M5 counts touches the cadence machine closed**, under D5 — a ticked task *or* a matching logged
activity — not HubSpot task completion. A touch closed by a logged note leaves its task open
(`open_tasks_superseded`), so task status undercounts the work. Each sync reports the number it
closed as `touches_closed`, and logs one `cadence.sync.touch_done` event per touch, with its
`source`; T10's report sums those over its week.

`GET /cadence/overdue` / `uv run lpe cadence overdue` read only our own schedule and need no token.
Between syncs they can list a touch that has since been done; the next sync notices.

### Dry run

`uv run lpe cadence sync --dry-run` makes the same HubSpot reads and the same decisions as a sync,
and applies none of them. It creates no task, writes no row and logs no `touch_done`, so M5 never
counts a rehearsal. For each prospect with something new, it prints the touches that would close and
**what closed each one** ("task ticked", "note 88000005", "task ticked + call …"), then the task it
would create and when it is due, or that it would park. A row with an interrupted create says
whether its task was found (and would be adopted) or would be created. A sync decides first (reads,
then the pure `plan_advance`) and only then applies, and a dry run stops after deciding, so what it
prints is what the next sync does, given the same HubSpot state. It takes no lock, so it can run
while a real sync does. It plans every prospect from **one moment**, the rows and the task batch as
read at its start, so a sync landing mid-run makes its output stale, never false. It also reports
overdue touches without logging `touch_overdue`, so alerts see each real sync once.

## Parking by hand

`uv run lpe cadence park <contact>` finishes a prospect's cadence, for example after a conversation
was reached or the prospect refused.

- **Final.** A contact has one cadence ever (`uq_cadence_state_contact`) and cycle position is never
  reset, so there is no unpark.
- **The open task is left alone.** Its id is printed for you to close in HubSpot; this slice never
  writes to the founder's tasks.
- **No reason is stored.** What happened belongs in HubSpot as a note, and the command says so.
- **Under the sync lock.** It refuses while a sync runs, because a sync that already loaded the row
  could advance a prospect a human just finished.
- **An interrupted create is cleared**, and the command warns that a task carrying its key may exist
  in HubSpot.
- **Never reaches HubSpot**, so it needs no token.

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

## Deferred

- **A per-touch "closed by a note" line in the real sync's output.** The dry run prints it already;
  the sync prints counts.
- **A route for `park`.** CLI only, for now: one user, one terminal.

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

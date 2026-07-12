# Discord IB Volume Kick Bot (v2 — per-member rolling cycles)

Each member's monthly requirement now runs from THEIR OWN join date, independent
of everyone else, instead of a shared calendar-month check for the whole server.

## How it works

- Every time the bot runs (scheduled or `!checkvolumes`), it checks each trading
  member individually.
- It calculates how many full months have passed since their last confirmed
  cycle checkpoint (starts at their Join Date for a brand new member).
- If 0 months have passed: skipped, not due yet. This naturally protects new
  members during their first partial month.
- If 1+ months have passed: they just need the flat minimum lots (e.g. 1 lot)
  traded since their last checkpoint — regardless of how many months have
  actually passed. So even if you only check them once every 3 months, they
  still only need 1 lot in that window, not 3.
  - Met it → their cycle quietly rolls forward, with a fresh baseline starting
    now. Any surplus lots don't carry over into future months.
  - Didn't meet it → kicked immediately, no grace period.
- This works even if you update the CSV data irregularly. Whether you check
  weekly or once every 3 months, the bot always calculates the correct
  required amount for however much time has actually passed for that person.

## Sheet tabs

- **Member Mapping** — you fill this in (Discord ID, Join Date, Exempt, etc.)
- **Monthly Raw Paste** — you paste broker CSV data here whenever you remember
- **Status Overview** — the bot writes this after every run. Shows exactly
  where every member currently stands (cycle start, volume, status). This is
  also how the bot remembers each person's rolling baseline between runs —
  don't edit it manually, or their cycle tracking will be thrown off.
- **Kick Log** — the bot appends a row here every time it kicks someone, for
  your records.
- **Settings** — minimum lots per month.

## If someone rejoins after being kicked

Update their **Join Date** in Member Mapping to the date they rejoined. Since
their previous "OK"/"Not due yet" status row won't exist anymore after a kick,
the bot will treat them as starting a brand new cycle from that new date.

## Setup (same as before)

```bash
pip install -r requirements.txt
cp .env.example .env
# fill in .env with your token, guild ID, sheet name
```

Run once with `DRY_RUN=true` to confirm behavior in the terminal and in the
"Status Overview" tab before going live. Note: even in dry run, "OK" rows will
roll their cycle forward in Status Overview (since that's just bookkeeping,
not an actual kick) — so if you're testing repeatedly, be aware repeated dry
runs will advance non-kicked members' cycles each time you test.

Once confirmed, set `DRY_RUN=false` and restart.

## Discord bot commands

- `!checkvolumes` — run the check right now (requires Kick Members permission)
- Scheduled automatic run — controlled by `SCHEDULED_DAY` / `SCHEDULED_HOUR_UTC`
  in `.env`. Note this just decides WHEN the bot looks at everyone — each
  person is only actually evaluated if their own individual cycle is due.

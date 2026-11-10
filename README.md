# tennis-bot

Telegram bot that books a DeporYA tennis court for a date you pick — supports multiple
people, each with their own DeporYA login.

## Files
- `reserve.py` — Playwright flow that logs in and books a court (`run_reservation(...)`).
- `test_matching.py` — tests for the grid-row court/hour matching in `reserve.py`.
- `bot.py` — multi-user Telegram bot (`/login`, `/reservar`, `/reservarfecha`, `/status`, `/cancelar`, `/socios`) + APScheduler.
- `users.json` — per-chat account data (DeporYA credentials, saved partners, pending bookings). Git-ignored; written by the bot at `STATE_DIR/users.json`.

## How reservations work

`/reservarfecha DD/MM HH:MM cancha partner` schedules a one-time reservation for a
specific future date. It doesn't book anything right away: it does nothing until
**08:00 America/Montevideo on the morning that date opens** (the day before it, since
DeporYA only opens one day of availability ahead at a time), then books it once and
stops. If the date you ask for is already open (e.g. you're booking for tomorrow), it
books immediately instead of waiting.

That wake-up time is persisted in `users.json` and re-armed on every bot restart, so it
survives a Railway redeploy in between — if the open time already passed while the bot
was down, it fires almost immediately on the next startup instead of silently missing
it.

Under the hood, each run: logs into `agenbot.net/deporyatenis` with headless Chromium
using that person's own credentials, opens the target day's schedule grid (clicking
"Siguiente" forward as many times as needed), clicks the row matching the requested
hour + court, searches for the partner by name, and confirms the reservation. Whether
it succeeds or fails, that person gets a Telegram message with the result and a
screenshot.

## Courts and surfaces

The club has two surfaces (ladrillo and cemento) and court numbers are unique across
both, so you book by number alone — `cancha 5` means court 5 wherever it happens to be.
The bot reads the surface dropdown (`#vTIPOCANCHAID`) at run time and searches each
surface in turn until it finds that court at that hour, then reports back which surface
it booked. Nothing is hardcoded to one surface; passing `surface=` to
`run_reservation()` (or `SURFACE=` when running `reserve.py` directly) narrows the
search to a single one, which is only useful for testing.

Row matching is exact on both number and time: `row_courts()` strips times out of the
row text before reading court numbers, so a request for court 5 is never satisfied by
the `5` in `15:00` or by `Cancha 15`, and a request for an hour has to match a real time
token in the row. `test_matching.py` covers those cases — run `python3 test_matching.py`
after touching that logic.

If the requested court is already booked at that hour, the bot books another court at
the **same hour on the same surface**, picking the closest court number (lower wins a
tie), and the Telegram message says which court it took instead. The court's surface is
whichever one lists that court at any hour of the day, so this still works if the site
hides a booked slot entirely. It never switches surface or hour; if nothing else on that
surface is free at that hour, the run fails and says so.

## Multi-user access

Only Telegram chat IDs listed in the `ALLOWED_CHAT_IDS` env var can use the bot at all —
this is the gate for sharing it with someone else. To add a person:
1. Have them message `@userinfobot` on Telegram to get their numeric chat ID.
2. Add that ID to `ALLOWED_CHAT_IDS` in Railway (comma-separated, e.g. `111,222`).
3. They message the bot and run `/login theirDeporYAuser theirDeporYApassword` — the
   bot immediately deletes that message from the chat for privacy and stores the
   credentials under their own chat ID, isolated from everyone else's.
4. They run `/reservar` (or `/reservarfecha DD/MM HH:MM cancha partner`) to schedule their own booking.

Removing someone: delete their ID from `ALLOWED_CHAT_IDS` — their pending bookings stop
firing (their `users.json` entry is skipped once their chat ID isn't in the allowlist).

**Note:** credentials are stored in plaintext JSON on disk. Fine for a small trusted
group of friends; don't share the bot beyond people you'd trust with each other's
DeporYA login.

## Telegram commands
- `/login usuario contraseña` — save your own DeporYA credentials (required once, before `/reservarfecha`).
- `/reservar` — book by tapping buttons: day (next 7 days), hour, court, partner (from your saved list), then confirm. Each step has ‹ Atrás / ✖ Cancelar, and "✏️ Otra/Otro" lets you type an hour, court or partner the buttons don't list. A partner typed there is saved for next time.
- `/probar` — at the next 08:00 opening, log in at 07:58:30 and watch tomorrow's grid until 08:01:30, reporting a timeline (date visible? rows? Reservar buttons?) with and without reloading. Never books. Refuses on a morning that already has a real booking, since a second login could disturb it. `/probar ahora` just times login + navigation once.
- `/reservarfecha DD/MM HH:MM cancha partner` — e.g. `/reservarfecha 15/09 19:00 5 Kevin Monzon`. One-time booking for a specific future date; fires automatically at 08:00 the morning it opens. `cancha` is just the court number, ladrillo or cemento.
- `/status` — lists your pending scheduled bookings.
- `/cancelar DD/MM` — cancels the pending booking for that date.
- `/socios` — list your saved partners.
- `/socios agregar Nombre Apellido` — save a partner for quick reuse.
- `/socios borrar Nombre` — remove a saved partner.
- `/ayuda` (or `/help`, `/start`) — shows this explanation inside Telegram.

## Speed

The site updates in place over AJAX, so `wait_for_load_state()` can't tell when it's done. Instead of fixed sleeps, `act_and_wait()` waits for the visible text to change, capped at the old sleep. Step screenshots are off unless `SCREENSHOTS=all`; the final and failure ones are always taken. Each step's log line starts with seconds elapsed (`[  7.0s] 5️⃣ …`).

## Deploy on Railway
1. Create a Railway account at railway.app.
2. New Project → **Deploy from GitHub repo** → select `mgarcen/tennis-bot`. Railway will build the included `Dockerfile` (based on the official Playwright image, so Chromium is preinstalled).
3. Add environment variables in the Railway service settings:
   - `TELEGRAM_BOT_TOKEN` — from @BotFather.
   - `ALLOWED_CHAT_IDS` — comma-separated Telegram chat IDs allowed to use the bot (start with your own).
   - `BOOKING_HOURS` *(optional)* — hours offered as `/reservar` buttons, as on-the-hour ranges, e.g. `7-22` (default) or `8-12,17-22`.
   - `BOOKING_COURTS` *(optional)* — court numbers offered as buttons, e.g. `1-8` (default) or `1-4,7`.
4. **Recommended:** attach a Railway Volume (e.g. mounted at `/data`) and set `STATE_DIR=/data`, so `users.json` (everyone's saved logins/pending bookings) survives redeploys instead of resetting each time the container rebuilds.
5. Railway keeps the process alive 24/7; `bot.py` runs Telegram long-polling and APScheduler fires each person's pending bookings at their scheduled wake-up time.

## Local testing
```
cp .env.example .env   # fill in values
export $(cat .env | xargs)
python bot.py
```

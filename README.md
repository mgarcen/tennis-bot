# tennis-bot

Telegram bot that books a DeporYA tennis court every day at 08:00 Uruguay time —
supports multiple people, each with their own DeporYA login and their own schedule.

## Files
- `reserve.py` — Playwright flow that logs in and books a court (`run_reservation(...)`).
- `bot.py` — multi-user Telegram bot (`/login`, `/reservar`, `/status`, `/cancelar`, `/socios`) + APScheduler daily trigger at 08:00 `America/Montevideo`.
- `users.json` — per-chat account data (DeporYA credentials, schedule, saved partners). Git-ignored; written by the bot at `STATE_DIR/users.json`.

## How reservations work

Every day at exactly **08:00 America/Montevideo**, APScheduler (running inside `bot.py`
on Railway) loops over every registered, active user and books **tomorrow's** court —
DeporYA only opens one day of availability at a time — using each person's own saved
hour/court/partner.

That config is a standing daily setting, not a one-time booking: once set with
`/reservar`, the same hour/court/partner get booked again automatically every following
day until changed or paused with `/cancelar`.

Under the hood, each run: logs into `agenbot.net/deporyatenis` with headless Chromium
using that person's own credentials, selects the LADRILLO surface, opens tomorrow's
schedule grid, clicks the row matching the configured hour + court, searches for the
partner by name, and confirms the reservation. Whether it succeeds or fails, that
person gets a Telegram message with the result and a screenshot.

## Multi-user access

Only Telegram chat IDs listed in the `ALLOWED_CHAT_IDS` env var can use the bot at all —
this is the gate for sharing it with someone else. To add a person:
1. Have them message `@userinfobot` on Telegram to get their numeric chat ID.
2. Add that ID to `ALLOWED_CHAT_IDS` in Railway (comma-separated, e.g. `111,222`).
3. They message the bot and run `/login theirDeporYAuser theirDeporYApassword` — the
   bot immediately deletes that message from the chat for privacy and stores the
   credentials under their own chat ID, isolated from everyone else's.
4. They run `/reservar HH:MM cancha partner` to activate their own daily booking.

Removing someone: delete their ID from `ALLOWED_CHAT_IDS` — their scheduled runs stop
(their `users.json` entry is skipped once their chat ID isn't in the allowlist).

**Note:** credentials are stored in plaintext JSON on disk (same tradeoff as the
previous single-user env-var setup, just per-person now). Fine for a small trusted
group of friends; don't share the bot beyond people you'd trust with each other's
DeporYA login.

## Telegram commands
- `/login usuario contraseña` — save your own DeporYA credentials (required once, before `/reservar`).
- `/reservar HH:MM cancha partner` — e.g. `/reservar 19:00 5 Kevin Monzon`. Sets your daily booking and arms it.
- `/status` — shows your current config and whether it's armed.
- `/cancelar` — pauses your daily booking.
- `/socios` — list your saved partners.
- `/socios agregar Nombre Apellido` — save a partner for quick reuse.
- `/socios borrar Nombre` — remove a saved partner.
- `/ayuda` (or `/help`, `/start`) — shows this explanation inside Telegram.

## Deploy on Railway
1. Create a Railway account at railway.app.
2. New Project → **Deploy from GitHub repo** → select `mgarcen/tennis-bot`. Railway will build the included `Dockerfile` (based on the official Playwright image, so Chromium is preinstalled).
3. Add environment variables in the Railway service settings:
   - `TELEGRAM_BOT_TOKEN` — from @BotFather.
   - `ALLOWED_CHAT_IDS` — comma-separated Telegram chat IDs allowed to use the bot (start with your own).
4. **Recommended:** attach a Railway Volume (e.g. mounted at `/data`) and set `STATE_DIR=/data`, so `users.json` (everyone's saved logins/schedules) survives redeploys instead of resetting each time the container rebuilds.
5. Railway keeps the process alive 24/7; `bot.py` runs Telegram long-polling and APScheduler fires reservations daily at 08:00 Uruguay time for every active user.

## Local testing
```
cp .env.example .env   # fill in values
export $(cat .env | xargs)
python bot.py
```

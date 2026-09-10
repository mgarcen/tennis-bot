# tennis-bot

Telegram bot that books a DeporYA tennis court every day at 08:00 Uruguay time.

## Files
- `reserve.py` — Playwright flow that logs in and books a court (`run_reservation(...)`).
- `bot.py` — Telegram bot (`/reservar`, `/status`, `/cancelar`) + APScheduler daily trigger at 08:00 `America/Montevideo`.
- `schedule.json` — current booking config, written by `/reservar`/`/cancelar` (git-ignored, lives on the Railway volume/container).

## How reservations work

Every day at exactly **08:00 America/Montevideo**, APScheduler (running inside `bot.py`
on Railway) fires `run_reservation()` from `reserve.py`. It always books **tomorrow's**
court — DeporYA only opens one day of availability at a time — using whatever
hour/court/partner are currently saved in `schedule.json`.

That config isn't a one-time booking, it's a standing daily setting: once you set it
with `/reservar`, the same hour/court/partner get booked again automatically every
following day until you change it with another `/reservar` or pause it with `/cancelar`.

Under the hood, each run: logs into `agenbot.net/deporyatenis` with headless Chromium,
selects the LADRILLO surface, opens tomorrow's schedule grid, clicks the row matching
the configured hour + court, searches for the partner by name, and confirms the
reservation — the same flow originally verified manually. Whether it succeeds or fails,
the bot sends you a Telegram message with the result and a screenshot of the final
page state.

## Telegram commands
- `/reservar HH:MM cancha partner` — e.g. `/reservar 19:00 5 Kevin Monzon`. Sets tomorrow's (and every following day's) booking and arms it.
- `/status` — shows the current config and whether it's armed.
- `/cancelar` — disarms the next scheduled run (pauses the daily booking until you `/reservar` again).
- `/ayuda` (or `/help`, `/start`) — shows this explanation inside Telegram.

## Deploy on Railway
1. Create a Railway account at railway.app.
2. New Project → **Deploy from GitHub repo** → select `mgarcen/tennis-bot`. Railway will build the included `Dockerfile` (based on the official Playwright image, so Chromium is preinstalled).
3. Add environment variables in the Railway service settings:
   - `TENNIS_USER`, `TENNIS_PASS` — DeporYA login.
   - `TELEGRAM_BOT_TOKEN` — from @BotFather.
   - `TELEGRAM_CHAT_ID` — your Telegram user/chat id (only this chat can control the bot).
4. Railway keeps the process alive 24/7; `bot.py` runs Telegram long-polling and APScheduler fires the reservation daily at 08:00 Uruguay time.

## Local testing
```
cp .env.example .env   # fill in values
export $(cat .env | xargs)
python bot.py
```

# tennis-bot

Telegram bot that books a DeporYA tennis court every day at 08:00 Uruguay time.

## Files
- `reserve.py` — Playwright flow that logs in and books a court (`run_reservation(...)`).
- `bot.py` — Telegram bot (`/reservar`, `/status`, `/cancelar`) + APScheduler daily trigger at 08:00 `America/Montevideo`.
- `schedule.json` — current booking config, written by `/reservar`/`/cancelar` (git-ignored, lives on the Railway volume/container).

## Telegram commands
- `/reservar HH:MM cancha partner` — e.g. `/reservar 19:00 5 Kevin Monzon`. Sets tomorrow's (and every following day's) booking and arms it.
- `/status` — shows the current config and whether it's armed.
- `/cancelar` — disarms the next scheduled run.

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

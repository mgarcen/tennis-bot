import json
import logging
import os
from pathlib import Path
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

from reserve import run_reservation

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("tennis-bot")

TZ = ZoneInfo("America/Montevideo")
STATE_FILE = Path(__file__).parent / "schedule.json"

TENNIS_USER = os.environ["TENNIS_USER"]
TENNIS_PASS = os.environ["TENNIS_PASS"]
BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = int(os.environ["TELEGRAM_CHAT_ID"])

DEFAULT_STATE = {
    "enabled": True,
    "hour": "10:00",
    "court": "5",
    "partner": "Kevin Monzon",
    "days_ahead": 1,
}


def load_state():
    if STATE_FILE.exists():
        return {**DEFAULT_STATE, **json.loads(STATE_FILE.read_text())}
    return dict(DEFAULT_STATE)


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2))


state = load_state()


def _authorized(update: Update) -> bool:
    return bool(update.effective_chat) and update.effective_chat.id == CHAT_ID


async def reservar_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _authorized(update):
        return
    args = context.args
    if len(args) < 3:
        await update.message.reply_text(
            "Uso: /reservar HH:MM cancha nombreDelPartner\nEj: /reservar 19:00 5 Kevin Monzon"
        )
        return
    hour, court, *partner_parts = args
    partner = " ".join(partner_parts)
    state.update({"enabled": True, "hour": hour, "court": court, "partner": partner})
    save_state(state)
    await update.message.reply_text(
        f"✅ Programado — todos los días a las 08:00 se reserva:\n"
        f"🏟️ Cancha {court} · 🕗 {hour} · 🤝 {partner}"
    )


async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _authorized(update):
        return
    s = state
    status = "✅ activo" if s["enabled"] else "🚫 cancelado"
    await update.message.reply_text(
        f"📋 Estado: {status}\n"
        f"🏟️ Cancha: {s['court']}\n"
        f"🕗 Hora: {s['hour']}\n"
        f"🤝 Partner: {s['partner']}\n"
        f"📅 Reserva automática todos los días a las 08:00 (Uruguay)"
    )


async def cancelar_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _authorized(update):
        return
    state["enabled"] = False
    save_state(state)
    await update.message.reply_text("🚫 Reserva automática cancelada. Usá /reservar para reactivarla.")


async def run_scheduled_reservation(app: Application):
    if not state.get("enabled"):
        log.info("Skipping scheduled run — currently disabled")
        return
    log.info("Running scheduled reservation: %s", state)
    result = await run_reservation(
        username=TENNIS_USER,
        password=TENNIS_PASS,
        court=state["court"],
        hour=state["hour"],
        days_ahead=state["days_ahead"],
        partner=state["partner"],
    )
    text = (
        f"✅ Reserva confirmada: {result['message']}"
        if result["success"]
        else f"❌ Falló la reserva: {result['message']}"
    )
    await app.bot.send_message(chat_id=CHAT_ID, text=text)
    shot = result.get("screenshot")
    if shot and os.path.exists(shot):
        with open(shot, "rb") as f:
            await app.bot.send_photo(chat_id=CHAT_ID, photo=f)


async def post_init(app: Application):
    scheduler = AsyncIOScheduler(timezone=TZ)
    scheduler.add_job(
        run_scheduled_reservation,
        CronTrigger(hour=8, minute=0, second=0, timezone=TZ),
        args=[app],
        id="daily_reservation",
        misfire_grace_time=120,
    )
    scheduler.start()
    app.bot_data["scheduler"] = scheduler
    log.info("Scheduler started — daily run at 08:00 America/Montevideo")
    try:
        await app.bot.send_message(chat_id=CHAT_ID, text="🎾 Tennis bot online.")
    except Exception:
        log.exception("Could not send startup message (has the chat started a conversation with the bot?)")


def main():
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("reservar", reservar_cmd))
    app.add_handler(CommandHandler("status", status_cmd))
    app.add_handler(CommandHandler("cancelar", cancelar_cmd))
    app.run_polling()


if __name__ == "__main__":
    main()

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
# Point STATE_DIR at a Railway Volume mount (e.g. /data) so accounts survive redeploys.
STATE_FILE = Path(os.environ.get("STATE_DIR", str(Path(__file__).parent))) / "users.json"

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
ALLOWED_CHAT_IDS = {
    int(cid.strip()) for cid in os.environ.get("ALLOWED_CHAT_IDS", "").split(",") if cid.strip()
}

DEFAULT_USER = {
    "tennis_user": None,
    "tennis_pass": None,
    "enabled": False,
    "hour": "10:00",
    "court": "5",
    "partner": "Kevin Monzon",
    "partners": [],
    "days_ahead": 1,
}


def load_users():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {}


def save_users(all_users):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(all_users, indent=2))


users = load_users()


def _authorized(update: Update) -> bool:
    return bool(update.effective_chat) and update.effective_chat.id in ALLOWED_CHAT_IDS


def get_user(chat_id: int) -> dict:
    key = str(chat_id)
    if key not in users:
        users[key] = dict(DEFAULT_USER)
        users[key]["partners"] = []
    return users[key]


async def login_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _authorized(update):
        return
    args = context.args
    chat_id = update.effective_chat.id
    if len(args) != 2:
        await update.message.reply_text("Uso: /login usuarioDeporYA contraseña")
        return
    u = get_user(chat_id)
    u["tennis_user"], u["tennis_pass"] = args
    save_users(users)
    try:
        await context.bot.delete_message(chat_id=chat_id, message_id=update.message.message_id)
    except Exception:
        log.warning("Could not delete /login message containing credentials")
    await context.bot.send_message(
        chat_id=chat_id,
        text="✅ Credenciales guardadas (borré tu mensaje). Usá /reservar para activar tu reserva diaria.",
    )


async def reservar_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _authorized(update):
        return
    chat_id = update.effective_chat.id
    u = get_user(chat_id)
    if not u.get("tennis_user"):
        await update.message.reply_text("Primero iniciá sesión: /login usuarioDeporYA contraseña")
        return
    args = context.args
    if len(args) < 3:
        await update.message.reply_text(
            "Uso: /reservar HH:MM cancha nombreDelPartner\nEj: /reservar 19:00 5 Kevin Monzon"
        )
        return
    hour, court, *partner_parts = args
    partner = " ".join(partner_parts)
    u.update({"enabled": True, "hour": hour, "court": court, "partner": partner})
    if partner not in u["partners"]:
        u["partners"].append(partner)
    save_users(users)
    await update.message.reply_text(
        f"✅ Programado — todos los días a las 08:00 se reserva:\n"
        f"🏟️ Cancha {court} · 🕗 {hour} · 🤝 {partner}"
    )


async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _authorized(update):
        return
    u = get_user(update.effective_chat.id)
    if not u.get("tennis_user"):
        await update.message.reply_text("No iniciaste sesión todavía: /login usuarioDeporYA contraseña")
        return
    status = "✅ activo" if u["enabled"] else "🚫 cancelado"
    await update.message.reply_text(
        f"📋 Estado: {status}\n"
        f"🏟️ Cancha: {u['court']}\n"
        f"🕗 Hora: {u['hour']}\n"
        f"🤝 Partner: {u['partner']}\n"
        f"📅 Reserva automática todos los días a las 08:00 (Uruguay)"
    )


async def cancelar_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _authorized(update):
        return
    u = get_user(update.effective_chat.id)
    u["enabled"] = False
    save_users(users)
    await update.message.reply_text("🚫 Reserva automática cancelada. Usá /reservar para reactivarla.")


async def socios_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _authorized(update):
        return
    u = get_user(update.effective_chat.id)
    args = context.args

    if not args:
        if not u["partners"]:
            await update.message.reply_text("Todavía no guardaste ningún partner. Usá /socios agregar Nombre Apellido")
        else:
            listado = "\n".join(f"• {p}" for p in u["partners"])
            await update.message.reply_text(f"🤝 Partners guardados:\n{listado}")
        return

    action = args[0].lower()
    name = " ".join(args[1:]).strip()

    if action in ("agregar", "add") and name:
        if name not in u["partners"]:
            u["partners"].append(name)
            save_users(users)
        await update.message.reply_text(f"✅ Agregado: {name}")
    elif action in ("borrar", "quitar", "remove", "del") and name:
        match = next((p for p in u["partners"] if p.lower() == name.lower()), None)
        if match:
            u["partners"].remove(match)
            save_users(users)
            await update.message.reply_text(f"🗑️ Borrado: {match}")
        else:
            await update.message.reply_text(f"No encontré '{name}' en la lista.")
    else:
        await update.message.reply_text(
            "Uso:\n/socios — ver lista\n/socios agregar Nombre Apellido\n/socios borrar Nombre"
        )


async def ayuda_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _authorized(update):
        return
    await update.message.reply_text(
        "🎾 *Cómo funciona*\n\n"
        "1️⃣ Iniciá sesión una vez con tu usuario de DeporYA:\n"
        "`/login usuarioDeporYA contraseña`\n"
        "(borro tu mensaje automáticamente por seguridad)\n\n"
        "2️⃣ Programá tu reserva diaria:\n"
        "`/reservar HH:MM cancha partner`\n"
        "Ej: `/reservar 19:00 5 Kevin Monzon`\n\n"
        "Todos los días a las 08:00 (hora Uruguay) reservo automáticamente la cancha "
        "de *mañana* con esos datos, hasta que la cambies o la canceles.\n\n"
        "*/status* — ver tu configuración actual\n"
        "*/cancelar* — pausar la reserva automática\n"
        "*/socios* — ver tus partners guardados\n"
        "*/socios agregar Nombre Apellido* — guardar un partner\n"
        "*/socios borrar Nombre* — borrar un partner\n\n"
        "Te aviso por acá después de cada intento, con una captura de pantalla.\n\n"
        "Cada persona tiene su propia cuenta de DeporYA y su propia programación — "
        "lo que vos configures no afecta a nadie más.",
        parse_mode="Markdown",
    )


async def run_scheduled_reservations(app: Application):
    for chat_id_str, u in list(users.items()):
        chat_id = int(chat_id_str)
        if chat_id not in ALLOWED_CHAT_IDS:
            continue
        if not u.get("enabled") or not u.get("tennis_user"):
            continue
        log.info("Running scheduled reservation for chat %s", chat_id)
        try:
            result = await run_reservation(
                username=u["tennis_user"],
                password=u["tennis_pass"],
                court=u["court"],
                hour=u["hour"],
                days_ahead=u["days_ahead"],
                partner=u["partner"],
                screenshot_dir=f"screenshots/{chat_id}",
            )
        except Exception as e:
            log.exception("Reservation run crashed for chat %s", chat_id)
            result = {"success": False, "message": str(e), "screenshot": None}

        text = (
            f"✅ Reserva confirmada: {result['message']}"
            if result["success"]
            else f"❌ Falló la reserva: {result['message']}"
        )
        try:
            await app.bot.send_message(chat_id=chat_id, text=text)
            shot = result.get("screenshot")
            if shot and os.path.exists(shot):
                with open(shot, "rb") as f:
                    await app.bot.send_photo(chat_id=chat_id, photo=f)
        except Exception:
            log.exception("Could not notify chat %s", chat_id)


async def post_init(app: Application):
    scheduler = AsyncIOScheduler(timezone=TZ)
    scheduler.add_job(
        run_scheduled_reservations,
        CronTrigger(hour=8, minute=0, second=0, timezone=TZ),
        args=[app],
        id="daily_reservations",
        misfire_grace_time=120,
    )
    scheduler.start()
    app.bot_data["scheduler"] = scheduler
    log.info(
        "Scheduler started — daily run at 08:00 America/Montevideo for %d allowed chat(s)",
        len(ALLOWED_CHAT_IDS),
    )


def main():
    if not ALLOWED_CHAT_IDS:
        log.warning("ALLOWED_CHAT_IDS is empty — no one will be able to use this bot")
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("login", login_cmd))
    app.add_handler(CommandHandler("reservar", reservar_cmd))
    app.add_handler(CommandHandler("status", status_cmd))
    app.add_handler(CommandHandler("cancelar", cancelar_cmd))
    app.add_handler(CommandHandler("socios", socios_cmd))
    app.add_handler(CommandHandler(["ayuda", "help", "start"], ayuda_cmd))
    app.run_polling()


if __name__ == "__main__":
    main()

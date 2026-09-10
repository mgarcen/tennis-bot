import json
import logging
import os
import re
from datetime import date as date_cls, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.date import DateTrigger
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
    "partners": [],
    "pending": [],
}


HOUR_RE = re.compile(r"^\d{1,2}:\d{2}$")


def validate_hour_court(hour: str, court: str) -> str | None:
    """Returns an error message if hour/court look swapped or malformed, else None."""
    if not HOUR_RE.match(hour):
        return f"❌ '{hour}' no parece una hora válida (formato HH:MM, ej: 19:00). ¿Escribiste los argumentos en el orden correcto?"
    if not court.isdigit():
        return f"❌ '{court}' no parece un número de cancha válido. ¿Escribiste los argumentos en el orden correcto?"
    return None


def parse_date_arg(text: str) -> date_cls:
    """Parse 'DD/MM' (nearest future occurrence) or 'DD/MM/AAAA'."""
    parts = text.split("/")
    if len(parts) not in (2, 3):
        raise ValueError("Formato de fecha inválido, usá DD/MM (ej: 15/09)")
    try:
        day, month = int(parts[0]), int(parts[1])
    except ValueError:
        raise ValueError("Formato de fecha inválido, usá DD/MM (ej: 15/09)")
    today = datetime.now(TZ).date()
    if len(parts) == 3:
        year = int(parts[2])
        if year < 100:
            year += 2000
    else:
        year = today.year
    try:
        result = date_cls(year, month, day)
    except ValueError:
        raise ValueError("Esa fecha no existe")
    if len(parts) == 2 and result <= today:
        result = date_cls(year + 1, month, day)
    return result


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
        users[key]["pending"] = []
    users[key].setdefault("pending", [])
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
        text="✅ Credenciales guardadas (borré tu mensaje). Usá /reservarfecha para programar una reserva.",
    )


async def reservarfecha_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _authorized(update):
        return
    chat_id = update.effective_chat.id
    u = get_user(chat_id)
    if not u.get("tennis_user"):
        await update.message.reply_text("Primero iniciá sesión: /login usuarioDeporYA contraseña")
        return
    args = context.args
    if len(args) < 4:
        await update.message.reply_text(
            "Uso: /reservarfecha DD/MM HH:MM cancha nombreDelPartner\n"
            "Ej: /reservarfecha 15/09 19:00 5 Kevin Monzon"
        )
        return
    date_str, hour, court, *partner_parts = args
    partner = " ".join(partner_parts)
    err = validate_hour_court(hour, court)
    if err:
        await update.message.reply_text(err)
        return

    try:
        target_date = parse_date_arg(date_str)
    except ValueError as e:
        await update.message.reply_text(f"❌ {e}")
        return

    today = datetime.now(TZ).date()
    if target_date <= today:
        await update.message.reply_text("❌ La fecha tiene que ser a partir de mañana.")
        return

    run_at = datetime.combine(target_date - timedelta(days=1), time(8, 0, 0), tzinfo=TZ)
    entry = {
        "date": target_date.strftime("%d/%m/%Y"),
        "hour": hour,
        "court": court,
        "partner": partner,
        "run_at": run_at.isoformat(),
    }
    u["pending"] = [p for p in u["pending"] if p["date"] != entry["date"]]
    u["pending"].append(entry)
    if partner not in u["partners"]:
        u["partners"].append(partner)
    save_users(users)

    schedule_pending_job(context.application, chat_id, entry)

    if run_at <= datetime.now(TZ):
        await update.message.reply_text(
            f"✅ Ya está abierto — reservando ahora:\n"
            f"🏟️ Cancha {court} · 🕗 {hour} · 📅 {entry['date']} · 🤝 {partner}"
        )
    else:
        await update.message.reply_text(
            f"✅ Programado — el {run_at.strftime('%d/%m')} a las 08:00 reservo automáticamente:\n"
            f"🏟️ Cancha {court} · 🕗 {hour} · 📅 {entry['date']} · 🤝 {partner}"
        )


async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _authorized(update):
        return
    u = get_user(update.effective_chat.id)
    if not u.get("tennis_user"):
        await update.message.reply_text("No iniciaste sesión todavía: /login usuarioDeporYA contraseña")
        return
    if not u["pending"]:
        await update.message.reply_text(
            "📋 No tenés ninguna reserva programada.\nUsá /reservarfecha DD/MM HH:MM cancha partner"
        )
        return
    lines = [f"  • {p['date']} — Cancha {p['court']} {p['hour']} con {p['partner']}" for p in u["pending"]]
    await update.message.reply_text("📆 Reservas programadas:\n" + "\n".join(lines))


async def cancelar_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _authorized(update):
        return
    chat_id = update.effective_chat.id
    u = get_user(chat_id)
    args = context.args

    if not args:
        await update.message.reply_text("Uso: /cancelar DD/MM — cancela la reserva programada para esa fecha.")
        return

    try:
        target_date = parse_date_arg(args[0])
    except ValueError as e:
        await update.message.reply_text(f"❌ {e}")
        return
    date_key = target_date.strftime("%d/%m/%Y")
    before = len(u["pending"])
    u["pending"] = [p for p in u["pending"] if p["date"] != date_key]
    save_users(users)
    scheduler = context.application.bot_data.get("scheduler")
    if scheduler:
        try:
            scheduler.remove_job(pending_job_id(chat_id, date_key))
        except Exception:
            pass
    if len(u["pending"]) < before:
        await update.message.reply_text(f"🚫 Cancelada la reserva programada para el {date_key}.")
    else:
        await update.message.reply_text(f"No tenía ninguna reserva programada para el {date_key}.")


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
        "2️⃣ Programá una reserva para una fecha puntual:\n"
        "`/reservarfecha DD/MM HH:MM cancha partner`\n"
        "Ej: `/reservarfecha 15/09 19:00 5 Kevin Monzon`\n\n"
        "Esto no reserva ahora: espera y reserva automáticamente a las 08:00 del día "
        "anterior (cuando la cancha abre esa fecha), aunque el bot se haya reiniciado "
        "en el medio.\n\n"
        "*/status* — ver tus reservas programadas\n"
        "*/cancelar DD/MM* — cancela una reserva programada\n"
        "*/socios* — ver tus partners guardados\n"
        "*/socios agregar Nombre Apellido* — guardar un partner\n"
        "*/socios borrar Nombre* — borrar un partner\n\n"
        "Te aviso por acá después de cada intento, con una captura de pantalla.\n\n"
        "Cada persona tiene su propia cuenta de DeporYA y su propia programación — "
        "lo que vos configures no afecta a nadie más.",
        parse_mode="Markdown",
    )


def pending_job_id(chat_id: int, date_key: str) -> str:
    return f"oneoff_{chat_id}_{date_key.replace('/', '-')}"


def schedule_pending_job(app: Application, chat_id: int, entry: dict):
    scheduler = app.bot_data.get("scheduler")
    if scheduler is None:
        return
    run_at = datetime.fromisoformat(entry["run_at"])
    now = datetime.now(TZ)
    if run_at < now:
        run_at = now + timedelta(seconds=5)
    scheduler.add_job(
        run_pending_reservation,
        DateTrigger(run_date=run_at, timezone=TZ),
        args=[app, chat_id, entry],
        id=pending_job_id(chat_id, entry["date"]),
        replace_existing=True,
        misfire_grace_time=3600,
    )


async def run_pending_reservation(app: Application, chat_id: int, entry: dict):
    u = get_user(chat_id)
    # One-shot: drop it from pending regardless of outcome.
    u["pending"] = [p for p in u["pending"] if p["date"] != entry["date"]]
    save_users(users)

    if not u.get("tennis_user"):
        try:
            await app.bot.send_message(
                chat_id=chat_id,
                text=f"❌ No pude reservar el {entry['date']}: no hay credenciales guardadas (/login).",
            )
        except Exception:
            log.exception("Could not notify chat %s", chat_id)
        return

    target_date = datetime.strptime(entry["date"], "%d/%m/%Y").date()
    days_ahead = (target_date - datetime.now(TZ).date()).days
    if days_ahead < 1:
        try:
            await app.bot.send_message(
                chat_id=chat_id,
                text=f"❌ No pude reservar el {entry['date']}: la fecha ya pasó (¿el bot estuvo caído?).",
            )
        except Exception:
            log.exception("Could not notify chat %s", chat_id)
        return

    log.info("Running one-off reservation for chat %s: %s", chat_id, entry["date"])
    try:
        result = await run_reservation(
            username=u["tennis_user"],
            password=u["tennis_pass"],
            court=entry["court"],
            hour=entry["hour"],
            days_ahead=days_ahead,
            partner=entry["partner"],
            screenshot_dir=f"screenshots/{chat_id}",
        )
    except Exception as e:
        log.exception("One-off reservation crashed for chat %s", chat_id)
        result = {"success": False, "message": str(e), "screenshot": None}

    text = (
        f"✅ Reserva confirmada para el {entry['date']}: {result['message']}"
        if result["success"]
        else f"❌ Falló la reserva del {entry['date']}: {result['message']}"
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
    scheduler.start()
    app.bot_data["scheduler"] = scheduler

    rearmed = 0
    for chat_id_str, u in users.items():
        for entry in u.get("pending", []):
            schedule_pending_job(app, int(chat_id_str), entry)
            rearmed += 1

    log.info(
        "Scheduler started for %d allowed chat(s), %d pending booking(s) re-armed",
        len(ALLOWED_CHAT_IDS),
        rearmed,
    )


def main():
    if not ALLOWED_CHAT_IDS:
        log.warning("ALLOWED_CHAT_IDS is empty — no one will be able to use this bot")
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("login", login_cmd))
    app.add_handler(CommandHandler("reservarfecha", reservarfecha_cmd))
    app.add_handler(CommandHandler("status", status_cmd))
    app.add_handler(CommandHandler("cancelar", cancelar_cmd))
    app.add_handler(CommandHandler("socios", socios_cmd))
    app.add_handler(CommandHandler(["ayuda", "help", "start"], ayuda_cmd))
    app.run_polling()


if __name__ == "__main__":
    main()

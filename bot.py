from __future__ import annotations

import json
import logging
import sys
import os
import re
from datetime import date as date_cls, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.date import DateTrigger
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from reserve import run_reservation

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    stream=sys.stdout,  # stderr shows up red in Railway as if everything were an error
)
# httpx logs every request URL at INFO, and Telegram URLs embed the bot token.
logging.getLogger("httpx").setLevel(logging.WARNING)
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
        text="✅ Credenciales guardadas (borré tu mensaje). Usá /reservar para programar una reserva.",
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
            "Ej: /reservarfecha 15/09 19:00 5 Kevin Monzon\n"
            "La cancha es el número, de ladrillo o de cemento — lo busco solo."
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

    await update.message.reply_text(
        schedule_booking(context.application, chat_id, target_date, hour, court, partner)
    )


def schedule_booking(app: Application, chat_id: int, target_date: date_cls,
                     hour: str, court: str, partner: str) -> str:
    """Save and arm a booking (replacing any for that date); returns the reply text."""
    u = get_user(chat_id)
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

    schedule_pending_job(app, chat_id, entry)

    summary = f"🏟️ Cancha {court} · 🕗 {hour} · 📅 {entry['date']} · 🤝 {partner}"
    if run_at <= datetime.now(TZ):
        return f"✅ Ya está abierto — reservando ahora:\n{summary}"
    return (f"✅ Programado — el {run_at.strftime('%d/%m')} a las 08:00 reservo automáticamente:\n"
            f"{summary}")


async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _authorized(update):
        return
    u = get_user(update.effective_chat.id)
    if not u.get("tennis_user"):
        await update.message.reply_text("No iniciaste sesión todavía: /login usuarioDeporYA contraseña")
        return
    if not u["pending"]:
        await update.message.reply_text(
            "📋 No tenés ninguna reserva programada.\nUsá /reservar para programar una."
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


# ── /reservar: tap-through booking ───────────────────────────────────────────
# Each step edits the same message with the next set of buttons. Choices so far live in
# context.user_data["rv"]; if the bot restarts mid-flow they're gone and the user is
# asked to start again. "Otra/Otro" lets you type a value the buttons don't offer.

WEEKDAYS = ["Lun", "Mar", "Mié", "Jue", "Vie", "Sáb", "Dom"]
BOOKING_DAYS = 7  # how many days ahead the day picker offers, starting tomorrow


def _parse_range(spec: str) -> list[int]:
    """'1-8' or '1,2,5-7' → [1, 2, 5, 6, 7]."""
    out = []
    for part in spec.split(","):
        part = part.strip()
        if "-" in part:
            a, b = part.split("-", 1)
            out.extend(range(int(a), int(b) + 1))
        elif part:
            out.append(int(part))
    return out


# Hours and courts offered as buttons; set BOOKING_HOURS / BOOKING_COURTS in Railway to change.
HOUR_CHOICES = [f"{h:02d}:00" for h in _parse_range(os.environ.get("BOOKING_HOURS", "7-22"))]
COURT_CHOICES = [str(c) for c in _parse_range(os.environ.get("BOOKING_COURTS", "1-8"))]


def _rows(buttons, per_row):
    return [buttons[i:i + per_row] for i in range(0, len(buttons), per_row)]


def _btn(label, data):
    return InlineKeyboardButton(label, callback_data=f"rv:{data}")


NAV_BACK = _btn("‹ Atrás", "back")
NAV_CANCEL = _btn("✖ Cancelar", "x")


def _day_label(d: date_cls) -> str:
    return f"{WEEKDAYS[d.weekday()]} {d.strftime('%d/%m')}"


def _wizard_view(u: dict, rv: dict):
    """(text, keyboard) for whichever step the flow is on."""
    step = rv["step"]
    if step == "day":
        tomorrow = datetime.now(TZ).date() + timedelta(days=1)
        days = [tomorrow + timedelta(days=i) for i in range(BOOKING_DAYS)]
        buttons = [_btn(_day_label(d), f"d:{d.isoformat()}") for d in days]
        return "📅 ¿Qué día?", InlineKeyboardMarkup(_rows(buttons, 4) + [[NAV_CANCEL]])

    day = date_cls.fromisoformat(rv["date"])
    so_far = f"📅 {_day_label(day)}"
    if step == "hour":
        buttons = [_btn(h, f"h:{h}") for h in HOUR_CHOICES] + [_btn("✏️ Otra", "h:?")]
        return f"{so_far}\n🕐 ¿Hora?", InlineKeyboardMarkup(_rows(buttons, 4) + [[NAV_BACK, NAV_CANCEL]])

    so_far += f" · 🕐 {rv['hour']}"
    if step == "court":
        buttons = [_btn(c, f"c:{c}") for c in COURT_CHOICES] + [_btn("✏️ Otra", "c:?")]
        return f"{so_far}\n🎾 ¿Cancha?", InlineKeyboardMarkup(_rows(buttons, 4) + [[NAV_BACK, NAV_CANCEL]])

    so_far += f" · 🎾 Cancha {rv['court']}"
    if step == "partner":
        buttons = [_btn(name, f"p:{i}") for i, name in enumerate(u["partners"])]
        buttons.append(_btn("✏️ Otro", "p:?"))
        return f"{so_far}\n🤝 ¿Con quién?", InlineKeyboardMarkup(_rows(buttons, 2) + [[NAV_BACK, NAV_CANCEL]])

    # confirm
    text = f"{so_far} · 🤝 {rv['partner']}\n\n¿Confirmo?"
    if any(p["date"] == day.strftime("%d/%m/%Y") for p in u["pending"]):
        text += "\n⚠️ Ya tenés una reserva programada ese día; esta la reemplaza."
    keyboard = [[_btn("✅ Confirmar", "ok"), NAV_BACK, NAV_CANCEL]]
    return text, InlineKeyboardMarkup(keyboard)


# What "‹ Atrás" goes back to from each step (and from typing a custom value).
PREV_STEP = {"hour": "day", "court": "hour", "partner": "court", "confirm": "partner",
             "type-hour": "hour", "type-court": "court", "type-partner": "partner"}
TYPE_PROMPTS = {
    "type-hour": "✏️ Escribí la hora (HH:MM, ej: 19:30):",
    "type-court": "✏️ Escribí el número de cancha:",
    "type-partner": "✏️ Escribí nombre y apellido del partner (como figura en DeporYA):",
}


async def reservar_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _authorized(update):
        return
    u = get_user(update.effective_chat.id)
    if not u.get("tennis_user"):
        await update.message.reply_text("Primero iniciá sesión: /login usuarioDeporYA contraseña")
        return
    rv = context.user_data["rv"] = {"step": "day"}
    text, keyboard = _wizard_view(u, rv)
    msg = await update.message.reply_text(text, reply_markup=keyboard)
    rv["message_id"] = msg.message_id


async def reservar_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not _authorized(update):
        await query.answer()
        return
    chat_id = update.effective_chat.id
    u = get_user(chat_id)
    rv = context.user_data.get("rv")
    action = query.data.removeprefix("rv:")

    # Buttons from an older /reservar (or from before a restart) no longer apply.
    if not rv or rv.get("message_id") != query.message.message_id:
        await query.answer("Este menú ya no está activo, usá /reservar de nuevo.", show_alert=True)
        return
    await query.answer()

    if action == "x":
        context.user_data.pop("rv", None)
        await query.edit_message_text("🚫 Reserva cancelada.")
        return
    if action == "back":
        rv["step"] = PREV_STEP.get(rv["step"], "day")
    elif action == "ok":
        context.user_data.pop("rv", None)
        target_date = date_cls.fromisoformat(rv["date"])
        reply = schedule_booking(context.application, chat_id, target_date,
                                 rv["hour"], rv["court"], rv["partner"])
        await query.edit_message_text(reply)
        return
    else:
        kind, _, value = action.partition(":")
        if value == "?":
            rv["step"] = {"h": "type-hour", "c": "type-court", "p": "type-partner"}[kind]
            await query.edit_message_text(TYPE_PROMPTS[rv["step"]],
                                          reply_markup=InlineKeyboardMarkup([[NAV_BACK, NAV_CANCEL]]))
            return
        if kind == "d":
            rv["date"], rv["step"] = value, "hour"
        elif kind == "h":
            rv["hour"], rv["step"] = value, "court"
        elif kind == "c":
            rv["court"], rv["step"] = value, "partner"
        elif kind == "p":
            idx = int(value)
            if idx >= len(u["partners"]):
                rv["step"] = "partner"  # list changed under us; show it again
            else:
                rv["partner"], rv["step"] = u["partners"][idx], "confirm"

    text, keyboard = _wizard_view(u, rv)
    await query.edit_message_text(text, reply_markup=keyboard)


async def reservar_typed(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Plain text while /reservar is waiting for a typed hour, court or partner."""
    if not _authorized(update):
        return
    rv = context.user_data.get("rv")
    if not rv or not rv["step"].startswith("type-"):
        return
    u = get_user(update.effective_chat.id)
    value = update.message.text.strip()

    if rv["step"] == "type-hour":
        if not HOUR_RE.match(value):
            await update.message.reply_text("❌ Formato HH:MM, ej: 19:30. Probá de nuevo:")
            return
        h, m = (int(x) for x in value.split(":"))
        if h > 23 or m > 59:
            await update.message.reply_text("❌ Esa hora no existe. Probá de nuevo:")
            return
        rv["hour"], rv["step"] = f"{h:02d}:{m:02d}", "court"
    elif rv["step"] == "type-court":
        if not value.isdigit():
            await update.message.reply_text("❌ Tiene que ser un número. Probá de nuevo:")
            return
        rv["court"], rv["step"] = str(int(value)), "partner"
    else:
        rv["partner"], rv["step"] = " ".join(value.split()), "confirm"

    # Retire the old menu and continue in a fresh message below what they typed.
    try:
        await context.bot.edit_message_reply_markup(
            chat_id=update.effective_chat.id, message_id=rv["message_id"], reply_markup=None
        )
    except Exception:
        pass
    text, keyboard = _wizard_view(u, rv)
    msg = await update.message.reply_text(text, reply_markup=keyboard)
    rv["message_id"] = msg.message_id


async def ayuda_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _authorized(update):
        return
    await update.message.reply_text(
        "🎾 *Cómo funciona*\n\n"
        "1️⃣ Iniciá sesión una vez con tu usuario de DeporYA:\n"
        "`/login usuarioDeporYA contraseña`\n"
        "(borro tu mensaje automáticamente por seguridad)\n\n"
        "2️⃣ Programá una reserva con /reservar y elegí día, hora, cancha y partner "
        "con los botones.\n"
        "O escribiendo todo junto: `/reservarfecha DD/MM HH:MM cancha partner`\n"
        "Ej: `/reservarfecha 15/09 19:00 5 Kevin Monzon`\n\n"
        "La cancha es simplemente su número — sirve tanto para las de ladrillo "
        "como para las de cemento, el bot se da cuenta sola de cuál es.\n\n"
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
    app.add_handler(CommandHandler("reservar", reservar_cmd))
    app.add_handler(CallbackQueryHandler(reservar_button, pattern=r"^rv:"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, reservar_typed))
    app.add_handler(CommandHandler("status", status_cmd))
    app.add_handler(CommandHandler("cancelar", cancelar_cmd))
    app.add_handler(CommandHandler("socios", socios_cmd))
    app.add_handler(CommandHandler(["ayuda", "help", "start"], ayuda_cmd))
    app.run_polling()


if __name__ == "__main__":
    main()

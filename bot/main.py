"""
agents-builder lead bot.

Принимает заявки с формы на сайте по HTTP и пересылает их
владельцу в Telegram с кнопками управления статусом
(новая / в работе / готово). Сам «знакомится» с владельцем
через /start — chat_id не нужно искать руками.
"""
import asyncio
import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import uvicorn
from aiogram import BaseMiddleware, Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    TelegramObject,
)
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, field_validator

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("bot")

# ---------- config ----------
BOT_TOKEN = os.environ["BOT_TOKEN"]
ALLOWED_ORIGINS = os.environ.get(
    "ALLOWED_ORIGINS",
    "https://agents-builder.ru,https://www.agents-builder.ru",
).split(",")
DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
STATE_FILE = DATA_DIR / "state.json"
# Hard-locked owner from env. If set, overrides state.json and protects against
# the entire data volume being wiped — bot still knows whom to listen to.
_owner_env = os.environ.get("OWNER_CHAT_ID")
OWNER_CHAT_ID_ENV: int | None = int(_owner_env) if _owner_env and _owner_env.strip() else None
RATE_WINDOW_SECONDS = 60
RATE_MAX_PER_WINDOW = 5
MSK = timezone(timedelta(hours=3))

# ---------- status definitions ----------
STATUS_NEW = "new"
STATUS_IN_PROGRESS = "in_progress"
STATUS_DONE = "done"

STATUS_LABEL = {
    STATUS_NEW: "⏳ <b>Ожидает</b>",
    STATUS_IN_PROGRESS: "🔄 <b>В работе</b>",
    STATUS_DONE: "✅ <b>Готово</b>",
}

# ---------- state ----------
def _default_state() -> dict:
    return {"owner_chat_id": None, "submissions": [], "last_menu_message_id": None}


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            d = json.loads(STATE_FILE.read_text())
            d.setdefault("submissions", [])
            d.setdefault("last_menu_message_id", None)
            return d
        except Exception as e:
            log.warning("state file corrupted, starting fresh: %s", e)
    return _default_state()


def save_state() -> None:
    STATE_FILE.write_text(json.dumps(STATE, ensure_ascii=False, indent=2))


STATE = load_state()
RATE_LIMITS: dict[str, list[float]] = {}

# Env override wins: if OWNER_CHAT_ID is set, force state to match.
if OWNER_CHAT_ID_ENV is not None and STATE.get("owner_chat_id") != OWNER_CHAT_ID_ENV:
    log.info("OWNER_CHAT_ID env override → setting state owner_chat_id=%s", OWNER_CHAT_ID_ENV)
    STATE["owner_chat_id"] = OWNER_CHAT_ID_ENV
    save_state()


def next_id() -> int:
    if not STATE["submissions"]:
        return 1
    return max(s["id"] for s in STATE["submissions"]) + 1


def find_submission(sid: int) -> dict | None:
    for s in STATE["submissions"]:
        if s["id"] == sid:
            return s
    return None


def count_by_status() -> dict[str, int]:
    counts = {STATUS_NEW: 0, STATUS_IN_PROGRESS: 0, STATUS_DONE: 0}
    for s in STATE["submissions"]:
        counts[s.get("status", STATUS_NEW)] = counts.get(s.get("status", STATUS_NEW), 0) + 1
    return counts


# ---------- contact formatting ----------
def format_contact(contact: str) -> tuple[str, str]:
    c = contact.strip()
    if c.startswith("@") and re.match(r"^@[A-Za-z0-9_]{3,32}$", c):
        return "💬", f'<a href="https://t.me/{c[1:]}">Telegram {c}</a>'
    if re.match(r"^https?://t\.me/[A-Za-z0-9_]+", c):
        return "💬", f'<a href="{c}">{c}</a>'
    if re.match(r"^[^\s@]+@[^\s@]+\.[^\s@]+$", c):
        return "✉️", f'<a href="mailto:{c}">{c}</a>'
    digits = re.sub(r"[^\d+]", "", c)
    if re.match(r"^[\+\d\s\-\(\)]{7,25}$", c) and len(digits) >= 7:
        return "📱", f'<a href="tel:{digits}">{c}</a>'
    return "💼", html_escape(c)


def html_escape(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# ---------- keyboards ----------
def main_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="⏳ Ожидают"), KeyboardButton(text="🔄 В работе")],
            [KeyboardButton(text="✅ Готовые"), KeyboardButton(text="📋 Все заявки")],
            [KeyboardButton(text="📊 Статистика"), KeyboardButton(text="❓ Помощь")],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )


def submission_actions(sub: dict) -> InlineKeyboardMarkup:
    """Inline keyboard with status-change buttons for a single submission."""
    status = sub.get("status", STATUS_NEW)
    rows: list[list[InlineKeyboardButton]] = []

    if status == STATUS_NEW:
        rows.append([
            InlineKeyboardButton(text="✋ Взять в работу", callback_data=f"st:{sub['id']}:{STATUS_IN_PROGRESS}"),
        ])
        rows.append([
            InlineKeyboardButton(text="✅ Готово", callback_data=f"st:{sub['id']}:{STATUS_DONE}"),
        ])
    elif status == STATUS_IN_PROGRESS:
        rows.append([
            InlineKeyboardButton(text="✅ Готово", callback_data=f"st:{sub['id']}:{STATUS_DONE}"),
        ])
        rows.append([
            InlineKeyboardButton(text="↩️ Вернуть в ожидание", callback_data=f"st:{sub['id']}:{STATUS_NEW}"),
        ])
    else:  # DONE
        rows.append([
            InlineKeyboardButton(text="🔄 Вернуть в работу", callback_data=f"st:{sub['id']}:{STATUS_IN_PROGRESS}"),
        ])

    return InlineKeyboardMarkup(inline_keyboard=rows)


# ---------- message rendering ----------
def render_submission(sub: dict) -> str:
    status = sub.get("status", STATUS_NEW)
    icon, formatted_contact = format_contact(sub["contact"])
    ts_str = sub["ts"]
    try:
        # Parse ISO timestamp and convert to MSK
        ts = datetime.fromisoformat(ts_str)
        ts_str = ts.astimezone(MSK).strftime("%d.%m.%Y · %H:%M MSK")
    except Exception:
        pass

    return (
        f"🆕 <b>Заявка</b> · <code>#{sub['id']}</code>  ·  {STATUS_LABEL[status]}\n"
        f"<i>{ts_str}</i>\n\n"
        f"📝 <b>Задача</b>\n{html_escape(sub['task'])}\n\n"
        f"{icon} <b>Контакт:</b> {formatted_contact}"
    )


def render_list(submissions: list[dict], title: str, empty_msg: str) -> str:
    if not submissions:
        return f"<b>{title}</b>\n\n<i>{empty_msg}</i>"
    lines = [f"<b>{title}</b> · <code>{len(submissions)}</code>", ""]
    for s in submissions[-10:][::-1]:  # newest first, last 10
        ts_str = ""
        try:
            ts_str = datetime.fromisoformat(s["ts"]).astimezone(MSK).strftime("%d.%m %H:%M")
        except Exception:
            pass
        icon, _ = format_contact(s["contact"])
        task_preview = s["task"].replace("\n", " ")
        if len(task_preview) > 80:
            task_preview = task_preview[:77] + "…"
        lines.append(
            f"<code>#{s['id']}</code> · <i>{ts_str}</i> · {icon} {html_escape(s['contact'])}\n"
            f"   {html_escape(task_preview)}"
        )
    if len(submissions) > 10:
        lines.append(f"\n<i>…и ещё {len(submissions) - 10}</i>")
    return "\n".join(lines)


# ---------- telegram bot ----------
bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()


class OwnerOnlyMiddleware(BaseMiddleware):
    """
    Игнорирует всех, кроме владельца.
    Пока owner не привязан, пропускает только /start (для первичной привязки).
    После привязки — отвечает только тому chat_id, который записан в state.
    Для посторонних бот выглядит «мёртвым» — не отвечает ни на что.
    """

    async def __call__(self, handler, event: TelegramObject, data):
        owner = STATE.get("owner_chat_id")

        if isinstance(event, Message):
            chat_id = event.chat.id
            text = event.text or ""
            is_start = text.startswith("/start")
        elif isinstance(event, CallbackQuery):
            chat_id = event.message.chat.id if event.message else None
            is_start = False
        else:
            return await handler(event, data)

        if owner is None:
            if is_start:
                return await handler(event, data)
            log.info("rejected pre-bind message from chat_id=%s", chat_id)
            return None

        if chat_id == owner:
            return await handler(event, data)

        log.info("rejected foreign message from chat_id=%s (owner=%s)", chat_id, owner)
        return None


dp.message.middleware(OwnerOnlyMiddleware())
dp.callback_query.middleware(OwnerOnlyMiddleware())


async def replace_menu(msg: Message, text: str) -> None:
    """
    Заменяет предыдущий ответ-меню новым: удаляет старый список + само
    сообщение-команду пользователя, чтобы чат оставался чистым.
    Используется только для меню-ответов (списки, статистика, помощь),
    НЕ для самих заявок.
    """
    try:
        await msg.delete()
    except Exception:
        pass
    prev_id = STATE.get("last_menu_message_id")
    if prev_id:
        try:
            await bot.delete_message(chat_id=msg.chat.id, message_id=prev_id)
        except Exception:
            pass
    sent = await bot.send_message(
        chat_id=msg.chat.id,
        text=text,
        reply_markup=main_menu(),
        disable_web_page_preview=True,
    )
    STATE["last_menu_message_id"] = sent.message_id
    save_state()


@dp.message(CommandStart())
async def cmd_start(msg: Message) -> None:
    first_time = STATE.get("owner_chat_id") is None
    if first_time:
        STATE["owner_chat_id"] = msg.chat.id
        save_state()
    if first_time:
        text = (
            "✅ <b>Бот подключён к agents-builder.ru</b>\n\n"
            f"Чат привязан: <code>{msg.chat.id}</code>.\n"
            "Все заявки с формы на сайте теперь будут приходить сюда.\n\n"
            "Нижняя клавиатура — для быстрого доступа к заявкам по статусам. "
            "На каждой новой заявке будут кнопки управления статусом."
        )
    else:
        counts = count_by_status()
        text = (
            "👋 Бот уже подключён.\n\n"
            f"⏳ Ожидают: <b>{counts[STATUS_NEW]}</b>\n"
            f"🔄 В работе: <b>{counts[STATUS_IN_PROGRESS]}</b>\n"
            f"✅ Готовы: <b>{counts[STATUS_DONE]}</b>"
        )
    await msg.answer(text, reply_markup=main_menu())


@dp.message(Command("help"))
@dp.message(F.text == "❓ Помощь")
async def cmd_help(msg: Message) -> None:
    await replace_menu(msg,
        "<b>agents-builder · bot</b>\n\n"
        "Принимает заявки с формы на сайте и присылает их в этот чат с кнопками управления статусом.\n\n"
        "<b>Кнопки на заявке</b>\n"
        "• <b>Взять в работу</b> — статус «в работе»\n"
        "• <b>Готово</b> — заявка обработана\n"
        "• <b>Вернуть</b> — переключение между статусами\n\n"
        "<b>Команды</b>\n"
        "/start — (пере)привязать этот чат\n"
        "/test — тестовая заявка\n"
        "/stats — статистика\n"
        "/help — это сообщение",
    )


@dp.message(Command("stats"))
@dp.message(F.text == "📊 Статистика")
async def cmd_stats(msg: Message) -> None:
    counts = count_by_status()
    total = sum(counts.values())
    await replace_menu(msg,
        f"📊 <b>Статистика</b>\n\n"
        f"⏳ Ожидают: <b>{counts[STATUS_NEW]}</b>\n"
        f"🔄 В работе: <b>{counts[STATUS_IN_PROGRESS]}</b>\n"
        f"✅ Готовы: <b>{counts[STATUS_DONE]}</b>\n\n"
        f"📋 Всего: <b>{total}</b>",
    )


@dp.message(Command("test"))
async def cmd_test(msg: Message) -> None:
    await deliver_submission(
        task="Нужен Telegram-бот поддержки для интернет-магазина мебели. "
             "~200 типовых вопросов, бот должен закрывать большинство, "
             "сложное передавать оператору.",
        contact="@example_user",
    )


@dp.message(F.text == "⏳ Ожидают")
async def filter_new(msg: Message) -> None:
    subs = [s for s in STATE["submissions"] if s.get("status", STATUS_NEW) == STATUS_NEW]
    await replace_menu(msg, render_list(subs, "⏳ Ожидающие заявки", "Нет заявок в ожидании."))


@dp.message(F.text == "🔄 В работе")
async def filter_in_progress(msg: Message) -> None:
    subs = [s for s in STATE["submissions"] if s.get("status") == STATUS_IN_PROGRESS]
    await replace_menu(msg, render_list(subs, "🔄 Заявки в работе", "Сейчас ничего не в работе."))


@dp.message(F.text == "✅ Готовые")
async def filter_done(msg: Message) -> None:
    subs = [s for s in STATE["submissions"] if s.get("status") == STATUS_DONE]
    await replace_menu(msg, render_list(subs, "✅ Готовые заявки", "Пока ничего не закрыто."))


@dp.message(F.text == "📋 Все заявки")
async def filter_all(msg: Message) -> None:
    await replace_menu(msg, render_list(STATE["submissions"], "📋 Все заявки", "Заявок ещё не было."))


@dp.callback_query(F.data.startswith("st:"))
async def cb_change_status(cb: CallbackQuery) -> None:
    try:
        _, sid_str, new_status = cb.data.split(":")
        sid = int(sid_str)
    except ValueError:
        await cb.answer("Битый callback")
        return
    if new_status not in STATUS_LABEL:
        await cb.answer("Неизвестный статус")
        return

    sub = find_submission(sid)
    if not sub:
        await cb.answer("Заявка не найдена")
        return

    old_status = sub.get("status", STATUS_NEW)
    sub["status"] = new_status
    save_state()

    # Edit the original message with updated status + new buttons
    try:
        await cb.message.edit_text(
            render_submission(sub),
            reply_markup=submission_actions(sub),
            disable_web_page_preview=True,
        )
    except Exception as e:
        log.warning("edit_text failed: %s", e)

    transitions = {
        STATUS_NEW: "вернул в ожидание",
        STATUS_IN_PROGRESS: "в работе",
        STATUS_DONE: "закрыл как готово",
    }
    await cb.answer(f"✓ {transitions.get(new_status, 'обновлено')}")
    log.info("submission #%d: %s → %s", sid, old_status, new_status)


# ---------- delivery ----------
async def deliver_submission(task: str, contact: str) -> dict:
    chat_id = STATE.get("owner_chat_id")
    if chat_id is None:
        raise RuntimeError("owner chat_id not set — send /start to the bot first")

    sub = {
        "id": next_id(),
        "task": task,
        "contact": contact,
        "ts": datetime.now(timezone.utc).isoformat(),
        "status": STATUS_NEW,
        "message_id": None,
    }
    STATE["submissions"].append(sub)
    save_state()

    sent = await bot.send_message(
        chat_id=chat_id,
        text=render_submission(sub),
        reply_markup=submission_actions(sub),
        disable_web_page_preview=True,
    )
    sub["message_id"] = sent.message_id
    save_state()
    return sub


# ---------- http api ----------
class Submission(BaseModel):
    task: str = Field(min_length=4, max_length=4000)
    contact: str = Field(min_length=3, max_length=200)
    website: str = Field(default="", max_length=200)  # honeypot

    @field_validator("task", "contact")
    @classmethod
    def strip_strings(cls, v: str) -> str:
        return v.strip()


def check_rate_limit(ip: str) -> bool:
    now = datetime.now().timestamp()
    window_start = now - RATE_WINDOW_SECONDS
    hits = [t for t in RATE_LIMITS.get(ip, []) if t > window_start]
    if len(hits) >= RATE_MAX_PER_WINDOW:
        return False
    hits.append(now)
    RATE_LIMITS[ip] = hits
    return True


app = FastAPI(title="agents-builder lead bot", docs_url=None, redoc_url=None)
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_methods=["POST", "GET", "OPTIONS"],
    allow_headers=["Content-Type"],
)


@app.post("/submit")
async def submit(s: Submission, request: Request):
    if s.website:
        log.info("honeypot triggered, dropping submission silently")
        return {"ok": True}

    ip = request.headers.get("x-forwarded-for", request.client.host if request.client else "?").split(",")[0].strip()
    if not check_rate_limit(ip):
        raise HTTPException(429, "Слишком много заявок с этого IP. Попробуйте через минуту.")

    if STATE.get("owner_chat_id") is None:
        raise HTTPException(503, "Бот ещё не настроен — отправьте /start в Telegram.")

    try:
        sub = await deliver_submission(s.task, s.contact)
        return {"ok": True, "number": sub["id"]}
    except Exception as e:
        log.exception("delivery failed")
        raise HTTPException(502, f"Не удалось доставить: {e}")


@app.get("/health")
async def health():
    counts = count_by_status()
    return {
        "status": "ok",
        "bot_configured": STATE.get("owner_chat_id") is not None,
        "submissions": counts,
    }


# ---------- entrypoint ----------
async def main() -> None:
    config = uvicorn.Config(app, host="0.0.0.0", port=8000, log_level="info", access_log=False)
    server = uvicorn.Server(config)
    log.info("starting bot polling + api server on :8000")
    await asyncio.gather(
        dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types()),
        server.serve(),
    )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("shutdown")

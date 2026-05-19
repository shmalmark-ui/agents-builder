"""
agents-builder.ru — demo bot
Meta-bot that answers questions about the agents-builder.ru service itself.

Architecture:
- python-telegram-bot v21 (async) with long polling
- anthropic SDK with claude-sonnet-4-6 model
- Streaming responses via message editing
- Inline keyboard menu on /start
- Tool calling for lead capture → forwards to owner Telegram
- File-based state for owner_chat_id persistence
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

import anthropic
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.constants import ChatAction, ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from knowledge import KNOWLEDGE

# ============== CONFIG ==============

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("demo-bot")

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
OWNER_USERNAME = os.environ.get("OWNER_USERNAME", "Linprimee").lstrip("@")
MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-6")

DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
STATE_FILE = DATA_DIR / "state.json"

claude = anthropic.AsyncAnthropic(api_key=ANTHROPIC_API_KEY)


# ============== STATE ==============

def load_state() -> dict[str, Any]:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            log.exception("state.json corrupted, resetting")
    return {"owner_chat_id": None, "conversations": {}}


def save_state() -> None:
    STATE_FILE.write_text(json.dumps(STATE, ensure_ascii=False, indent=2))


STATE = load_state()


# ============== PROMPTS ==============

SYSTEM_PROMPT = f"""Ты — демо-бот для сайта agents-builder.ru. Ты живая демонстрация того, что умеет соло-разработчик ИИ-агентов, который делает этот сайт. Посетитель пришёл потрогать продукт и понять — стоит ли заказывать.

# Твоя роль

Отвечай на вопросы про услуги, цены, сроки, процесс. Будь конкретным и уверенным. Никогда не выдумывай факты — если чего-то не знаешь, скажи «уточню у разработчика» и предложи оставить контакт.

# Тон

- Дружелюбный, но не панибратский
- Без жаргона (никаких «ТЗ», «MVP», «RAG», «эскалация»)
- Без корпоратива («Здравствуйте, уважаемый клиент»)
- Прямой: говори по делу, не растекайся
- Эмодзи — умеренно, только функциональные (💬 📅 📚 🛠 👉 ✓)
- На «вы», но не натянуто

# Что ты НЕ делаешь

- Не врёшь про цены или сроки. Если вопрос про конкретную задачу клиента, говори диапазон по сайту и предлагай обсудить детали с разработчиком.
- Не уходишь в общие разговоры про ИИ. Ты тут про конкретный продукт.
- Не обещаешь то чего нет на сайте (например голосовые роботы — это в «не подойду»).
- Не выдаёшь себя за человека. Если спросят — честно: «Я демо-бот сайта, разработчик отвечает лично в личке».

# Лид-захват

Когда видишь явный сигнал что человек хочет заказать (фразы вроде «хочу обсудить», «у меня задача», «можно созвониться», «интересно начать», «сколько за мою задачу»), спокойно скажи что-то вроде:

«Чтобы передать вашу задачу разработчику — кратко опишите: что нужно сделать, и как с вами связаться (Telegram-юзернейм или email). Передам и он ответит в личке.»

Когда клиент даст контакт и описание задачи — **вызови инструмент capture_lead** с тремя полями: имя (если назвал, иначе «не указано»), контакт, задача. После вызова инструмента ответь клиенту что заявка передана.

# Форматирование

- Используй **жирный** для акцентов
- Списки `- пункт` или `1. пункт` где уместно
- Цены пиши как `от 80 000 ₽`, не сокращай в `80k`
- Ссылки на сайт: agents-builder.ru (без https://)

# База знаний (всё что у тебя есть про сайт)

{KNOWLEDGE}

# Финальное правило

Ты не «AI ассистент в общем», ты конкретно демо-помощник этого сайта. Каждый ответ должен либо помочь посетителю принять решение, либо приближать к лиду.
"""


LEAD_TOOL = {
    "name": "capture_lead",
    "description": "Вызови когда клиент явно хочет заказать и предоставил имя/контакт/описание задачи. После вызова разработчик получит лид в Telegram-личку.",
    "input_schema": {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "Имя клиента или 'не указано' если не назвал",
            },
            "contact": {
                "type": "string",
                "description": "Telegram-юзернейм, email, или телефон",
            },
            "task": {
                "type": "string",
                "description": "Краткое описание задачи клиента (1-3 предложения)",
            },
        },
        "required": ["name", "contact", "task"],
    },
}


# ============== WELCOME ==============

WELCOME_TEXT = (
    "👋 Привет. Я *демо-бот* сайта *agents-builder.ru* — живой пример того, "
    "что умеет соло-разработчик, который делает ИИ-агентов под ключ.\n\n"
    "Спросите меня про *услуги, цены, сроки, процесс* — отвечу на основе сайта. "
    "Или ткните в кнопку ниже:"
)

WELCOME_KEYBOARD = InlineKeyboardMarkup([
    [
        InlineKeyboardButton("💬 Какие боты делаете?", callback_data="q:services"),
        InlineKeyboardButton("💰 Сколько стоит?", callback_data="q:price"),
    ],
    [
        InlineKeyboardButton("⏱ Сроки?", callback_data="q:timeline"),
        InlineKeyboardButton("⚙️ Как идёт работа?", callback_data="q:process"),
    ],
    [
        InlineKeyboardButton("🌐 Открыть сайт", url="https://agents-builder.ru"),
    ],
])

QUICK_QUESTIONS = {
    "q:services": "Какие боты вы делаете? Перечислите коротко с ценами.",
    "q:price": "Сколько стоят ваши услуги? Дайте обзор по всем продуктам.",
    "q:timeline": "За какой срок вы делаете бот? Опишите этапы.",
    "q:process": "Как происходит работа от первой беседы до запуска?",
}


# ============== HANDLERS ==============

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    chat_id = update.effective_chat.id

    # Auto-detect owner on first interaction
    if user.username and user.username.lower() == OWNER_USERNAME.lower():
        if STATE.get("owner_chat_id") != chat_id:
            STATE["owner_chat_id"] = chat_id
            save_state()
            log.info("owner_chat_id set to %s (@%s)", chat_id, user.username)
            await update.message.reply_text(
                f"✓ Вы зарегистрированы как владелец (@{user.username}). "
                "Лиды от посетителей будут прилетать сюда.",
            )

    # Reset conversation history for this chat
    STATE.setdefault("conversations", {})[str(chat_id)] = []
    save_state()

    await update.message.reply_text(
        WELCOME_TEXT,
        reply_markup=WELCOME_KEYBOARD,
        parse_mode=ParseMode.MARKDOWN,
    )


async def cb_quick_question(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    question = QUICK_QUESTIONS.get(query.data)
    if not question:
        return
    # Treat the quick-question tap as if the user typed it
    await answer_question(
        chat_id=query.message.chat_id,
        user_message=question,
        ctx=ctx,
        user=query.from_user,
    )


async def msg_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    user_message = update.message.text or ""
    if not user_message.strip():
        return
    await answer_question(
        chat_id=update.effective_chat.id,
        user_message=user_message,
        ctx=ctx,
        user=update.effective_user,
    )


async def answer_question(
    *,
    chat_id: int,
    user_message: str,
    ctx: ContextTypes.DEFAULT_TYPE,
    user,
) -> None:
    bot = ctx.bot

    # Conversation history per chat (kept short to control tokens)
    history = STATE.setdefault("conversations", {}).setdefault(str(chat_id), [])
    history.append({"role": "user", "content": user_message})
    history = history[-12:]  # last 6 turns

    # Show typing indicator
    await bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)

    # Start with an empty placeholder message we'll edit as text streams
    placeholder = await bot.send_message(chat_id=chat_id, text="…")
    accumulated = ""
    last_edit_len = 0
    tool_use_block: dict[str, Any] | None = None

    try:
        async with claude.messages.stream(
            model=MODEL,
            max_tokens=1024,
            system=SYSTEM_PROMPT,
            tools=[LEAD_TOOL],
            messages=history,
        ) as stream:
            async for event in stream:
                if event.type == "content_block_delta" and event.delta.type == "text_delta":
                    accumulated += event.delta.text
                    # Throttle edits: only update when ≥40 new chars or stream is winding down
                    if len(accumulated) - last_edit_len >= 40:
                        try:
                            await placeholder.edit_text(accumulated, parse_mode=ParseMode.MARKDOWN)
                            last_edit_len = len(accumulated)
                        except Exception:
                            # Markdown parse error → fall back to plain
                            try:
                                await placeholder.edit_text(accumulated)
                                last_edit_len = len(accumulated)
                            except Exception:
                                pass

            # Final message after stream completes
            final_message = await stream.get_final_message()

            # Check if Claude called the lead-capture tool
            for block in final_message.content:
                if block.type == "tool_use" and block.name == "capture_lead":
                    tool_use_block = {
                        "id": block.id,
                        "input": block.input,
                    }

        # Send final text version (markdown or plain)
        if accumulated:
            try:
                await placeholder.edit_text(accumulated, parse_mode=ParseMode.MARKDOWN)
            except Exception:
                try:
                    await placeholder.edit_text(accumulated)
                except Exception:
                    pass
            history.append({"role": "assistant", "content": accumulated})
            save_state()
        elif not tool_use_block:
            # No text and no tool — likely an error
            await placeholder.edit_text(
                "Хм, что-то задумался. Перепишите вопрос ещё раз, пожалуйста.",
            )
            return

        # Handle lead capture
        if tool_use_block:
            lead = tool_use_block["input"]
            await forward_lead(
                ctx=ctx,
                lead=lead,
                source_user=user,
                source_chat_id=chat_id,
            )

    except anthropic.APIError as e:
        log.exception("Claude API error: %s", e)
        await placeholder.edit_text(
            "Извините, не получилось ответить — модель временно недоступна. "
            "Попробуйте через минуту или напишите напрямую: agents-builder.ru",
        )


async def forward_lead(
    *,
    ctx: ContextTypes.DEFAULT_TYPE,
    lead: dict[str, Any],
    source_user,
    source_chat_id: int,
) -> None:
    """Forward the lead to the owner's Telegram and notify the visitor."""
    owner_chat_id = STATE.get("owner_chat_id")
    name = lead.get("name", "не указано")
    contact = lead.get("contact", "—")
    task = lead.get("task", "—")

    visitor_handle = (
        f"@{source_user.username}" if source_user.username else f"id={source_user.id}"
    )

    if owner_chat_id:
        text = (
            f"🔥 *Лид из demo-бота*\n\n"
            f"*Имя:* {name}\n"
            f"*Контакт:* {contact}\n"
            f"*Задача:* {task}\n\n"
            f"_От: {visitor_handle}, chat_id={source_chat_id}_"
        )
        try:
            await ctx.bot.send_message(
                chat_id=owner_chat_id,
                text=text,
                parse_mode=ParseMode.MARKDOWN,
            )
            log.info("Lead forwarded to owner %s", owner_chat_id)
        except Exception:
            log.exception("Failed to forward lead to owner")
    else:
        log.warning(
            "Lead captured but owner_chat_id not set — owner must /start the bot first. "
            "Lead: %s",
            lead,
        )

    # Confirm to the visitor
    await ctx.bot.send_message(
        chat_id=source_chat_id,
        text=(
            f"✓ Принял.\n\n"
            f"*Имя:* {name}\n"
            f"*Контакт:* {contact}\n"
            f"*Задача:* {task}\n\n"
            f"Разработчик увидит сообщение и ответит вам напрямую в течение дня."
        ),
        parse_mode=ParseMode.MARKDOWN,
    )


async def cmd_reset(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    STATE.setdefault("conversations", {})[str(chat_id)] = []
    save_state()
    await update.message.reply_text("История очищена. Начинаем заново.")


# ============== MAIN ==============

def main() -> None:
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("reset", cmd_reset))
    app.add_handler(CallbackQueryHandler(cb_quick_question, pattern=r"^q:"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, msg_text))

    log.info(
        "Starting demo-bot. owner_username=%s owner_chat_id=%s model=%s",
        OWNER_USERNAME,
        STATE.get("owner_chat_id"),
        MODEL,
    )
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()

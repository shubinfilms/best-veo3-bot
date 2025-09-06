# -*- coding: utf-8 -*-
# Best VEO3 bot — rollback (PTB 13 + polling)

import os, json, logging, traceback, requests
from typing import Optional, Dict, Any
from dotenv import load_dotenv

from telegram import (
    Update, InlineKeyboardMarkup, InlineKeyboardButton, ParseMode
)
from telegram.ext import (
    Updater, CallbackContext, CommandHandler, MessageHandler, Filters, CallbackQueryHandler
)

# ------------ ENV & LOG ------------
load_dotenv()

TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN", "")
KIE_API_KEY      = os.getenv("KIE_API_KEY", "")
KIE_BASE_URL     = (os.getenv("KIE_BASE_URL") or "https://api.kie.ai").rstrip("/")
KIE_GENERATE_PATH= (os.getenv("KIE_GENERATE_PATH") or "/api/v1/veo/generate").strip()
BOT_MODEL        = os.getenv("BOT_MODEL", "veo3_fast")  # veo3_fast (быстро) или veo3 (качество)

def _normalize_path(p: str) -> str:
    p = p.strip()
    if not p.startswith("/"):
        p = "/" + p
    return p

KIE_GENERATE_PATH = _normalize_path(KIE_GENERATE_PATH)
GEN_URL = f"{KIE_BASE_URL}{KIE_GENERATE_PATH}"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)
log = logging.getLogger("best-veo3")

# ------------ UI BUILDERS ------------
def kb_aspect() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ 16:9", callback_data="ar:16:9"),
        InlineKeyboardButton("9:16",    callback_data="ar:9:16"),
    ],[
        InlineKeyboardButton("Fast ⚡",    callback_data="model:veo3_fast"),
        InlineKeyboardButton("Quality 🎬", callback_data="model:veo3"),
    ],[
        InlineKeyboardButton("🚀 Сгенерировать", callback_data="go")
    ],[
        InlineKeyboardButton("⬅️ Назад", callback_data="back")
    ]])

# Сессия пользователя (простая in-memory; Render — один процесс)
STATE: Dict[int, Dict[str, Any]] = {}

def get_state(chat_id: int) -> Dict[str, Any]:
    st = STATE.setdefault(chat_id, {
        "aspect_ratio": "16:9",
        "model": BOT_MODEL,
        "prompt": ""
    })
    return st

# ------------ HANDLERS ------------
def start(update: Update, _: CallbackContext):
    chat_id = update.effective_chat.id
    get_state(chat_id)  # init
    update.message.reply_text(
        "Привет! Пришли видео-идею **или готовый промпт**.\n"
        "Выбери формат и режим, затем жми «Сгенерировать».",
        reply_markup=kb_aspect(),
        parse_mode=ParseMode.MARKDOWN
    )

def on_text(update: Update, _: CallbackContext):
    chat_id = update.effective_chat.id
    st = get_state(chat_id)
    st["prompt"] = update.message.text.strip()
    update.message.reply_text(
        "Промпт принят.\n\n"
        f"*Параметры:*\n• Формат: `{st['aspect_ratio']}`\n• Режим: `{st['model']}`",
        reply_markup=kb_aspect(),
        parse_mode=ParseMode.MARKDOWN
    )

def on_cb(update: Update, context: CallbackContext):
    query = update.callback_query
    query.answer()
    chat_id = query.message.chat_id
    st = get_state(chat_id)

    data = query.data or ""
    if data.startswith("ar:"):
        st["aspect_ratio"] = data.split(":", 1)[1]
        txt = _summary_text(st)
        query.edit_message_text(txt, reply_markup=kb_aspect(), parse_mode=ParseMode.MARKDOWN)
        return
    if data.startswith("model:"):
        st["model"] = data.split(":", 1)[1]
        txt = _summary_text(st)
        query.edit_message_text(txt, reply_markup=kb_aspect(), parse_mode=ParseMode.MARKDOWN)
        return
    if data == "back":
        txt = _summary_text(st)
        query.edit_message_text(txt, reply_markup=kb_aspect(), parse_mode=ParseMode.MARKDOWN)
        return
    if data == "go":
        if not st.get("prompt"):
            query.edit_message_text("Пришли текстовый промпт.", reply_markup=kb_aspect())
            return
        query.edit_message_text("🚀 Отправляю задачу в VEO3…")
        _send_task(query, st, context)
        return

def _summary_text(st: Dict[str, Any]) -> str:
    p = st.get("prompt") or "_промпт ещё не задан_"
    return (
        "Промпт:\n"
        f"```\n{p}\n```\n\n"
        "Параметры генерации:\n"
        f"• Формат: `{st['aspect_ratio']}`\n"
        f"• Режим: `{st['model']}`\n\n"
        "Выбери параметры и жми «Сгенерировать»."
    )

def _send_task(query, st: Dict[str, Any], context: CallbackContext):
    payload = {
        "prompt": st["prompt"],
        "model": st["model"],                 # "veo3" | "veo3_fast"
        "aspectRatio": st["aspect_ratio"],    # "16:9" | "9:16" (в API кейс не важен)
        # "enableFallback": False,            # при желании можно включить
    }
    headers = {
        "Authorization": f"Bearer {KIE_API_KEY}",
        "Content-Type": "application/json",
    }
    try:
        log.info("KIE POST %s payload=%s", GEN_URL, payload)
        r = requests.post(GEN_URL, headers=headers, json=payload, timeout=60)
        body = {}
        try:
            body = r.json()
        except Exception:
            pass

        # Ответы KIE:
        # 200 — успех (задача создана)
        # 402 — недостаточно кредитов
        # 4xx/5xx — другие ошибки
        if r.ok and body.get("code") == 200:
            task_id = body.get("data", {}).get("taskId") or "unknown"
            query.edit_message_text(
                f"✅ Задача отправлена! ID: `{task_id}`\nОбычно рендер 2–5 минут.",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад в меню", callback_data="back")]])
            )
        elif body.get("code") == 402:
            query.edit_message_text("❌ Недостаточно кредитов на Kie.ai. Пополните баланс.")
        else:
            msg = body.get("msg") or r.text
            query.edit_message_text(f"❌ Не удалось создать задачу:\nAPI ответ: {msg}")
        log.info("KIE ответ %s -> %s", r.status_code, body or r.text)
    except Exception as e:
        log.error("KIE ошибка: %s\n%s", e, traceback.format_exc())
        query.edit_message_text("❌ Сервис временно недоступен. Попробуй снова чуть позже.")

# ------------ MAIN ------------
def main():
    if not TELEGRAM_TOKEN:
        raise RuntimeError("TELEGRAM_TOKEN is empty")

    # ВАЖНО: PTB 13 — классический Updater + polling
    updater = Updater(token=TELEGRAM_TOKEN, use_context=True)
    dp = updater.dispatcher

    dp.add_handler(CommandHandler("start", start))
    dp.add_handler(MessageHandler(Filters.text & ~Filters.command, on_text))
    dp.add_handler(CallbackQueryHandler(on_cb))

    log.info("Bot is starting (polling)…")
    updater.start_polling(timeout=60, clean=True)
    updater.idle()

if __name__ == "__main__":
    main()

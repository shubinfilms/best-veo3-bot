# -*- coding: utf-8 -*-
# BEST VEO3 bot — PTB v20.7, Webhook-ready (Render)

import os
import json
import logging
from typing import Dict, Any, Optional, Tuple

import aiohttp
from telegram import (
    Update, InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardRemove
)
from telegram.ext import (
    Application, ApplicationBuilder, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters
)

# ---------------- ENV & LOG ----------------
logging.basicConfig(
    level=os.getenv("LOGLEVEL", "INFO"),
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s"
)
log = logging.getLogger("best-veo3-bot")

TOKEN = os.getenv("TELEGRAM_TOKEN", os.getenv("BOT_TOKEN", ""))
if not TOKEN:
    raise RuntimeError("TELEGRAM_TOKEN is not set")

# Kie AI
KIE_API_KEY = os.getenv("KIE_API_KEY", "").strip()
KIE_BASE_URL = os.getenv("KIE_BASE_URL", "https://api.kie.ai").strip().rstrip("/")
KIE_GEN_PATH = os.getenv("KIE_GENERATE_PATH", "/api/v1/veo/generate").strip()
DEFAULT_MODEL = os.getenv("BOT_MODEL", "veo3_fast").strip()  # veo3_fast | veo3

# Webhook
USE_WEBHOOK = os.getenv("BOT_WEBHOOK", "1").strip() == "1"
PUBLIC_URL = os.getenv("PUBLIC_URL", "").strip().rstrip("/")

def _normalize_path(p: str) -> str:
    """Ensure starts with /api... even if user set 'v1/...' """
    p = p.strip()
    if not p.startswith("/"):
        p = "/" + p
    if p.startswith("/v1/") or p.startswith("/veo/"):
        p = "/api" + p  # safety: people often paste 'v1/...'
    return p

KIE_GEN_PATH = _normalize_path(KIE_GEN_PATH)
KIE_GENERATE_URL = f"{KIE_BASE_URL}{KIE_GEN_PATH}"

# ---------- UI helpers ----------
AR16 = "ar_16_9"
AR9 = "ar_9_16"
FAST = "model_fast"
QUALITY = "model_quality"
SUBMIT = "submit"

def kb_aspect() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ 16:9", callback_data=AR16),
         InlineKeyboardButton("9:16", callback_data=AR9)]
    ])

def kb_aspect_9() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("16:9", callback_data=AR16),
         InlineKeyboardButton("✅ 9:16", callback_data=AR9)]
    ])

def kb_speed(cur: str) -> InlineKeyboardMarkup:
    fast_sel = "✅ Fast" if cur == "veo3_fast" else "Fast"
    quality_sel = "✅ Quality" if cur == "veo3" else "Quality"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(fast_sel, callback_data=FAST),
         InlineKeyboardButton(quality_sel, callback_data=QUALITY)],
        [InlineKeyboardButton("🚀 Сгенерировать", callback_data=SUBMIT)]
    ])

def pretty_err(code: int, msg: str) -> str:
    mapping = {
        200: "Успех.",
        400: "1080P ещё в обработке. Проверь позже.",
        401: "Проблема с авторизацией (ключ API неверен).",
        402: "Недостаточно кредитов на аккаунте Kie AI.",
        404: "Эндпойнт/ресурс не найден.",
        422: "Параметры запроса не прошли валидацию.",
        429: "Лимит запросов превышен. Попробуй позже.",
        455: "Сервис на тех. обслуживании.",
        500: "Внутренняя ошибка сервера.",
        501: "Не удалось создать видео.",
        505: "Функция сейчас отключена.",
    }
    base = mapping.get(code, f"Неизвестная ошибка ({code}).")
    extra = f"\nСообщение сервиса: {msg}" if msg else ""
    return base + extra

# ---------- Kie AI call ----------
async def kie_generate(prompt: str, aspect_ratio: str, model: str) -> Tuple[bool, str]:
    """
    Возвращает (ok, human_message). При успехе human_message содержит taskId/unknown.
    """
    if not KIE_API_KEY:
        return False, "KIE_API_KEY не задан в переменных окружения."

    payload: Dict[str, Any] = {
        "prompt": prompt,
        "model": model,                # "veo3" | "veo3_fast"
        "aspect_ratio": aspect_ratio,  # "16:9" | "9:16"
        # "enableFallback": False,     # при необходимости можно включить
        # "callBackUrl": f"{PUBLIC_URL}/veo3-callback" if PUBLIC_URL else None
    }
    # Удалим None
    payload = {k: v for k, v in payload.items() if v is not None}

    headers = {
        "Authorization": f"Bearer {KIE_API_KEY}",
        "Content-Type": "application/json"
    }

    log.info("KIE POST %s | payload=%s", KIE_GENERATE_URL, json.dumps(payload, ensure_ascii=False))
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=120)) as sess:
        async with sess.post(KIE_GENERATE_URL, json=payload, headers=headers) as resp:
            txt = await resp.text()
            log.info("KIE RESP %s %s", resp.status, txt)
            # сервис всегда возвращает JSON с полями {code, msg, data?}
            try:
                data = json.loads(txt)
            except Exception:
                return False, f"Сервис вернул не-JSON (HTTP {resp.status}): {txt[:400]}"

            code = data.get("code", resp.status)
            msg = data.get("msg") or data.get("message") or ""
            if code == 200:
                task_id = (data.get("data") or {}).get("taskId") or "unknown"
                return True, f"✅ Задача отправлена! ID: `{task_id}`"
            else:
                return False, "❌ " + pretty_err(code, msg)

# ---------- Handlers ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.clear()
    await update.message.reply_text(
        "Присылай видео *или* готовый промпт ✍️\n\n"
        "Сначала выбери соотношение сторон:",
        parse_mode="Markdown",
        reply_markup=kb_aspect()
    )
    # значения по умолчанию
    context.user_data["aspect"] = "16:9"
    context.user_data["model"] = DEFAULT_MODEL

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Команды:\n"
        "/start — начать заново\n"
        "/model — переключить Fast/Quality\n"
        "Отправь текст — это будет промпт для Veo 3."
    )

async def model_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cur = context.user_data.get("model", DEFAULT_MODEL)
    await update.message.reply_text(
        f"Текущая модель: *{cur}*\nВыбери режим:",
        parse_mode="Markdown",
        reply_markup=kb_speed(cur)
    )

async def on_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (update.message.text or "").strip()
    if not text:
        return
    context.user_data["prompt"] = text
    cur = context.user_data.get("model", DEFAULT_MODEL)
    ar = context.user_data.get("aspect", "16:9")
    await update.message.reply_text(
        "Промпт принят ✅\n\n"
        f"• Формат: *{ar}*\n"
        f"• Режим: *{'Fast' if cur=='veo3_fast' else 'Quality'}*\n\n"
        "Можно сразу жать «Сгенерировать», либо поменять параметры:",
        parse_mode="Markdown",
        reply_markup=kb_speed(cur)
    )

async def on_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    data = q.data

    if data == AR16:
        context.user_data["aspect"] = "16:9"
        await q.edit_message_reply_markup(reply_markup=kb_aspect())
    elif data == AR9:
        context.user_data["aspect"] = "9:16"
        await q.edit_message_reply_markup(reply_markup=kb_aspect_9())
    elif data == FAST:
        context.user_data["model"] = "veo3_fast"
        await q.edit_message_reply_markup(reply_markup=kb_speed("veo3_fast"))
    elif data == QUALITY:
        context.user_data["model"] = "veo3"
        await q.edit_message_reply_markup(reply_markup=kb_speed("veo3"))
    elif data == SUBMIT:
        prompt = context.user_data.get("prompt")
        if not prompt:
            await q.edit_message_text("Сначала пришли текстовый промпт ✍️")
            return
        ar = context.user_data.get("aspect", "16:9")
        model = context.user_data.get("model", DEFAULT_MODEL)
        await q.edit_message_text("🚀 Отправляю задачу в VEO3…")

        ok, msg = await kie_generate(prompt, ar, model)
        if ok:
            await q.message.reply_text(msg, parse_mode="Markdown", reply_markup=ReplyKeyboardRemove())
        else:
            await q.message.reply_text(msg)

# -------------- App --------------
def main() -> None:
    app: Application = ApplicationBuilder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("model", model_cmd))
    app.add_handler(CallbackQueryHandler(on_cb))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_prompt))

    if USE_WEBHOOK:
        if not PUBLIC_URL:
            raise RuntimeError("BOT_WEBHOOK=1, но PUBLIC_URL пуст. Задай PUBLIC_URL в Render.")
        port = int(os.getenv("PORT", "10000"))
        log.info("Starting webhook on 0.0.0.0:%s -> %s", port, PUBLIC_URL)
        app.run_webhook(
            listen="0.0.0.0",
            port=port,
            url_path=TOKEN,  # секретный путь
            webhook_url=f"{PUBLIC_URL}/{TOKEN}",
            drop_pending_updates=True,
        )
    else:
        log.info("Starting long polling…")
        app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()

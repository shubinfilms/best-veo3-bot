# -*- coding: utf-8 -*-
# BEST VEO3 bot — PTB v20.7 + Webhook/Polling, KIE.ai Veo3 Fast/Quality

import os
import json
import logging
import asyncio
from typing import Optional, Dict, Any

import requests
from dotenv import load_dotenv

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    ConversationHandler,
    filters,
)

# ----------------- ENV & LOG -----------------
load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
KIE_API_KEY    = os.getenv("KIE_API_KEY", "")
KIE_BASE_URL   = os.getenv("KIE_BASE_URL", "https://api.kie.ai").rstrip("/")
KIE_GEN_PATH   = os.getenv("KIE_GENERATE_PATH", "/api/v1/veo/generate").strip()
BOT_MODEL_DEF  = os.getenv("BOT_MODEL", "veo3_fast").strip()  # veo3_fast (Fast) или veo3 (Quality)
BOT_WEBHOOK    = os.getenv("BOT_WEBHOOK", "1").strip()        # "1" -> webhook; иначе polling
PUBLIC_URL     = os.getenv("PUBLIC_URL", "").rstrip("/")      # https://best-veo3-bot-xxxx.onrender.com
PORT           = int(os.getenv("PORT", "10000"))

if not KIE_GEN_PATH.startswith("/"):
    KIE_GEN_PATH = "/" + KIE_GEN_PATH

KIE_GENERATE_URL   = f"{KIE_BASE_URL}{KIE_GEN_PATH}"
KIE_STATUS_URL     = f"{KIE_BASE_URL}/api/v1/veo/record-info"
KIE_GET_1080P_URL  = f"{KIE_BASE_URL}/api/v1/veo/get-1080p-video"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s | %(message)s",
)
log = logging.getLogger("best-veo3")

# --------------- STATE MACHINE ---------------
CHOOSE_AR, CHOOSE_SPEED, ENTER_PROMPT, CONFIRM = range(4)


def kb_ar(current: Optional[str] = None) -> InlineKeyboardMarkup:
    v16 = "✅ 16:9" if current == "16:9" else "16:9"
    v916 = "✅ 9:16" if current == "9:16" else "9:16"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(v16, callback_data="ar:16:9"),
         InlineKeyboardButton(v916, callback_data="ar:9:16")],
        [InlineKeyboardButton("⬅️ Назад в меню", callback_data="menu")]
    ])


def kb_speed(current: Optional[str] = None) -> InlineKeyboardMarkup:
    f = "✅ Fast" if current == "veo3_fast" else "Fast"
    q = "✅ Quality" if current == "veo3" else "Quality"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f, callback_data="speed:veo3_fast"),
         InlineKeyboardButton(q, callback_data="speed:veo3")],
        [InlineKeyboardButton("🚀 Сгенерировать", callback_data="go")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="back_ar")]
    ])


def _headers() -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {KIE_API_KEY}",
        "Content-Type": "application/json",
    }


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["aspect_ratio"] = context.user_data.get("aspect_ratio", "16:9")
    context.user_data["model"] = context.user_data.get("model", BOT_MODEL_DEF)
    context.user_data["prompt"] = ""
    text = "Пришли видео *или* готовый промпт ✍️\n\nВыбери формат:"
    await update.effective_message.reply_text(
        text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb_ar(context.user_data["aspect_ratio"])
    )
    return CHOOSE_AR


async def on_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer()
    data = q.data

    if data == "menu":
        return await start(update, context)

    if data.startswith("ar:"):
        _, ar = data.split("ar:", 1)
        context.user_data["aspect_ratio"] = ar
        await q.edit_message_text(
            "Выбери режим скорости:",
            reply_markup=kb_speed(context.user_data.get("model"))
        )
        return CHOOSE_SPEED

    if data == "back_ar":
        await q.edit_message_text(
            "Выбери формат:",
            reply_markup=kb_ar(context.user_data.get("aspect_ratio"))
        )
        return CHOOSE_AR

    if data.startswith("speed:"):
        _, model = data.split("speed:", 1)
        context.user_data["model"] = model
        await q.edit_message_reply_markup(
            reply_markup=kb_speed(context.user_data.get("model"))
        )
        return CHOOSE_SPEED

    if data == "go":
        await q.edit_message_text(
            "Ок! Пришли промпт текстом (можно большим)."
        )
        return ENTER_PROMPT

    return CHOOSE_AR


async def on_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    prompt = update.effective_message.text.strip()
    context.user_data["prompt"] = prompt

    ar = context.user_data["aspect_ratio"]
    model = context.user_data["model"]
    est = "≈ 2.0 токена (Fast)" if model == "veo3_fast" else "≈ 5.0 токенов (Quality)"

    txt = (
        "📝 *Промпт принят.*\n"
        f"• Формат: *{ar}*\n"
        f"• Режим: *{'Fast' if model=='veo3_fast' else 'Quality'}*\n"
        f"• Оценочная стоимость: {est}\n\n"
        "Нажми *Сгенерировать*."
    )
    await update.effective_message.reply_text(
        txt, parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🚀 Сгенерировать", callback_data="submit")],
            [InlineKeyboardButton("✏️ Изменить промпт", callback_data="go")],
            [InlineKeyboardButton("⬅️ Назад в меню", callback_data="menu")],
        ])
    )
    return CONFIRM


def _create_task(prompt: str, model: str, aspect_ratio: str) -> Dict[str, Any]:
    payload = {
        "prompt": prompt,
        "model": model,                # "veo3" | "veo3_fast"
        "aspect_ratio": aspect_ratio,  # "16:9" | "9:16"
        # "enableFallback": True,      # при желании можно включить
        # "callBackUrl": "...",        # если поднимешь приём коллбэков
    }
    r = requests.post(KIE_GENERATE_URL, headers=_headers(), data=json.dumps(payload), timeout=90)
    try:
        return r.json()
    except Exception:
        return {"code": r.status_code, "msg": r.text}


async def on_submit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer()

    prompt = context.user_data.get("prompt", "").strip()
    if not prompt:
        await q.edit_message_text("Промпт пуст. Пришли текст — и снова нажми *Сгенерировать*.",
                                  parse_mode=ParseMode.MARKDOWN)
        return ENTER_PROMPT

    ar = context.user_data["aspect_ratio"]
    model = context.user_data["model"]

    await q.edit_message_text("🚀 Отправляю задачу в VEO3…")
    log.info("KIE POST %s payload=%s", KIE_GENERATE_URL, {"model": model, "aspect_ratio": ar})

    resp = await asyncio.to_thread(_create_task, prompt, model, ar)
    code = resp.get("code")
    msg = resp.get("msg")
    data = resp.get("data", {}) or {}

    if code == 200:
        task_id = data.get("taskId") or data.get("task_id") or "unknown"
        txt = (
            f"✅ Задача отправлена!\n"
            f"*Task ID:* `{task_id}`\n\n"
            "Обычно рендер занимает 2–5 минут.\n"
            f"Проверить статус: `/status {task_id}`\n"
            f"1080p (если доступно): `/hd {task_id}`"
        )
        await q.edit_message_text(txt, parse_mode=ParseMode.MARKDOWN)
        return ConversationHandler.END

    # Частые коды
    human = {
        400: "Сейчас считается 1080p. Попробуй через 1–2 минуты.",
        401: "Проверь API-ключ KIE.",
        402: "Недостаточно кредитов на KIE.",
        404: "Эндпоинт не найден (URL/путь?).",
        422: "Параметры запроса отклонены (попробуй другой формулировкой).",
        429: "Слишком много запросов. Подожди немного.",
        455: "Сервис на обслуживании.",
        500: "Ошибка сервера.",
        501: "Не удалось создать видео.",
        505: "Функция временно отключена.",
    }.get(code, msg or "Неизвестная ошибка")

    await q.edit_message_text(f"❌ Не удалось создать задачу:\nAPI code {code}: {human}")
    return ConversationHandler.END


def _check_status(task_id: str) -> Dict[str, Any]:
    r = requests.get(f"{KIE_STATUS_URL}?taskId={task_id}", headers=_headers(), timeout=60)
    try:
        return r.json()
    except Exception:
        return {"code": r.status_code, "msg": r.text}


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.effective_message.reply_text("Использование: `/status <taskId>`",
                                                  parse_mode=ParseMode.MARKDOWN)
        return

    task_id = context.args[0]
    data = await asyncio.to_thread(_check_status, task_id)
    code = data.get("code")
    if code != 200:
        await update.effective_message.reply_text(f"❌ API code {code}: {data.get('msg')}")
        return

    info = data.get("data") or {}
    flag = info.get("successFlag")  # 0 — в процессе, 1 — готово, 2/3 — ошибка
    if flag == 0:
        await update.effective_message.reply_text("⌛️ Генерация ещё идёт. Загляни позже.")
        return
    if flag in (2, 3):
        await update.effective_message.reply_text("❌ Генерация не удалась. Попробуй другой промпт/режим.")
        return

    # Успех
    try:
        urls = json.loads(info.get("resultUrls") or "[]")
    except Exception:
        urls = []

    if not urls:
        await update.effective_message.reply_text("Готово, но ссылки не пришли. Подожди чуть-чуть и повтори `/status`.")
        return

    text = "🎬 *Готово!* Ссылки на видео:\n" + "\n".join(urls)
    await update.effective_message.reply_text(text, parse_mode=ParseMode.MARKDOWN, disable_web_page_preview=False)


def _get_1080p(task_id: str) -> Dict[str, Any]:
    r = requests.get(f"{KIE_GET_1080P_URL}?taskId={task_id}", headers=_headers(), timeout=60)
    try:
        return r.json()
    except Exception:
        return {"code": r.status_code, "msg": r.text}


async def cmd_hd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.effective_message.reply_text("Использование: `/hd <taskId>`",
                                                  parse_mode=ParseMode.MARKDOWN)
        return
    task_id = context.args[0]
    data = await asyncio.to_thread(_get_1080p, task_id)
    code = data.get("code")
    if code != 200:
        await update.effective_message.reply_text(f"❌ API code {code}: {data.get('msg')}")
        return
    info = data.get("data") or {}
    url = info.get("url") or info.get("resultUrl")
    if not url:
        await update.effective_message.reply_text("Пока 1080p недоступно. Проверь позже.")
        return
    await update.effective_message.reply_text(f"🎞️ 1080p: {url}")


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    txt = (
        "Команды:\n"
        "/start — создать задачу\n"
        "/status <taskId> — статус генерации\n"
        "/hd <taskId> — получить 1080p\n"
        "\nМодели:\n"
        "• *veo3* — Quality (лучшее качество)\n"
        "• *veo3_fast* — Fast (быстрее и дешевле)\n"
    )
    await update.effective_message.reply_text(txt, parse_mode=ParseMode.MARKDOWN)


def build_app() -> Application:
    return ApplicationBuilder().token(TELEGRAM_TOKEN).build()


def add_handlers(app: Application) -> None:
    conv = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            CHOOSE_AR: [CallbackQueryHandler(on_cb)],
            CHOOSE_SPEED: [CallbackQueryHandler(on_cb)],
            ENTER_PROMPT: [
                CallbackQueryHandler(on_cb),
                MessageHandler(filters.TEXT & ~filters.COMMAND, on_prompt)
            ],
            CONFIRM: [CallbackQueryHandler(on_submit, pattern="^submit$"),
                      CallbackQueryHandler(on_cb)]
        },
        fallbacks=[CommandHandler("start", start)],
        name="main_conv",
        persistent=False,
    )
    app.add_handler(conv)
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("hd", cmd_hd))
    app.add_handler(CommandHandler("help", help_cmd))


def main() -> None:
    if not TELEGRAM_TOKEN:
        raise RuntimeError("TELEGRAM_TOKEN is empty")
    if not KIE_API_KEY:
        log.warning("⚠️ KIE_API_KEY is empty!")

    app = build_app()
    add_handlers(app)

    use_webhook = BOT_WEBHOOK == "1" and PUBLIC_URL
    if use_webhook:
        # Вебхуки для Render (PORT выдаёт Render)
        url_path = TELEGRAM_TOKEN  # безопасный путь
        webhook_url = f"{PUBLIC_URL}/{url_path}"
        log.info("Starting WEBHOOK on 0.0.0.0:%s, %s", PORT, webhook_url)
        app.run_webhook(
            listen="0.0.0.0",
            port=PORT,
            url_path=url_path,
            webhook_url=webhook_url,
        )
    else:
        log.info("Starting POLLING")
        app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()

# -*- coding: utf-8 -*-
# BEST VEO3 BOT — polling-only, Kie API (/api/v1/veo/*), Fast/Quality toggle
# 2025-09-06

import os, json, logging, traceback, requests, asyncio
from typing import Dict, Any, Optional

from dotenv import load_dotenv
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardRemove
from telegram.constants import ParseMode, ChatAction
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters
)

# ----------------- ENV & LOGGING -----------------
load_dotenv()

TELEGRAM_TOKEN  = os.getenv("TELEGRAM_TOKEN", "")
KIE_API_KEY     = os.getenv("KIE_API_KEY", "")
KIE_BASE_URL    = os.getenv("KIE_BASE_URL", "https://api.kie.ai").rstrip("/")
# пути Kie (оставляем отдельно, чтобы легко менять при необходимости)
KIE_GENERATE_PATH = os.getenv("KIE_GENERATE_PATH", "/api/v1/veo/generate")
KIE_RECORD_PATH   = os.getenv("KIE_RECORD_PATH", "/api/v1/veo/record-info")

# модель по умолчанию: veo3 (Quality) или veo3_fast (Fast)
DEFAULT_MODEL   = os.getenv("BOT_MODEL", "veo3").strip() or "veo3"

LOG_LEVEL       = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)
log = logging.getLogger("best-veo3")

# ----------------- UI -----------------
def kb_main(model_label: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🎬 Текст → видео", callback_data="gen_text")],
        [InlineKeyboardButton("🖼️ Фото → видео", callback_data="gen_photo")],
        [InlineKeyboardButton("🧠 Промпт-мастер", callback_data="prompt_master")],
        [InlineKeyboardButton("⚡ Режим: " + model_label, callback_data="toggle_model")],
        [InlineKeyboardButton("❓ FAQ", callback_data="faq")],
    ])

FORMAT_KB = InlineKeyboardMarkup([
    [InlineKeyboardButton("16:9", callback_data="fmt_16x9"),
     InlineKeyboardButton("9:16", callback_data="fmt_9x16")],
    [InlineKeyboardButton("⬅️ В меню", callback_data="back_menu")],
])

RUN_KB = InlineKeyboardMarkup([
    [InlineKeyboardButton("🚀 Сгенерировать", callback_data="run")],
    [InlineKeyboardButton("⬅️ В меню", callback_data="back_menu")],
])

# ----------------- STATE -----------------
def _model_label(m: str) -> str:
    return "Quality (veo3)" if m == "veo3" else "Fast (veo3_fast)"

def userstate(ctx: ContextTypes.DEFAULT_TYPE) -> Dict[str, Any]:
    if "state" not in ctx.user_data:
        ctx.user_data["state"] = {
            "mode": None,                # gen_text | gen_photo | prompt_master
            "aspect": "16:9",
            "prompt": None,
            "image_url": None,
            "model": DEFAULT_MODEL,      # veo3 | veo3_fast
            "_typing_stop": None,
        }
    return ctx.user_data["state"]

# ----------------- HELPERS -----------------
def looks_like_ready_prompt(text: str) -> bool:
    if not text:
        return False
    t = text.strip()
    if t.startswith("{") and t.endswith("}"):
        return True
    score = 0
    for kw in ("fps","lens","anamorphic","rack focus","lighting","bokeh","camera",
               "push-in","dolly","glide","35mm","85mm","shot","scene"):
        if kw in t.lower():
            score += 1
    return score >= 3 or len(t) > 400

async def _typing_loop(chat_id: int, ctx: ContextTypes.DEFAULT_TYPE, stop_event: asyncio.Event):
    try:
        while not stop_event.is_set():
            await ctx.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
            await asyncio.sleep(4)
    except Exception:
        pass

def _kie_headers() -> Dict[str, str]:
    return {"Authorization": f"Bearer {KIE_API_KEY}", "Content-Type": "application/json"}

def _kie_url(path: str) -> str:
    return f"{KIE_BASE_URL}{path}"

def _map_api_error(status: int, body_text: str) -> str:
    # Дружелюбные сообщения по кодам статуса
    mapping = {
        400: "Идёт обработка 1080p. Попробуйте запрос статуса чуть позже.",
        401: "Проблема с авторизацией. Проверь KIE_API_KEY.",
        402: "Недостаточно кредитов в Kie AI.",
        404: "Эндпоинт не найден. Проверь путь /api/v1/veo/generate.",
        422: "Параметры не прошли проверку (Validation Error).",
        429: "Слишком много запросов. Подождите немного.",
        455: "Сервис временно недоступен (обслуживание).",
        500: "Ошибка на стороне сервера.",
        501: "Не удалось создать видео.",
        505: "Функция сейчас отключена (disabled).",
    }
    base = mapping.get(status, f"Неизвестный ответ API ({status})")
    return f"{base}\nОтвет: {body_text[:300]}"

# ----------------- KIE SUBMIT -----------------
def submit_text_job(prompt: str, aspect: str, model: str, enable_fallback: bool=False) -> Dict[str, Any]:
    payload = {
        "prompt": prompt,
        "model": model,                      # veo3 | veo3_fast
        "aspectRatio": "16:9" if aspect == "16:9" else "9:16",
        "enableFallback": bool(enable_fallback),
    }
    try:
        r = requests.post(_kie_url(KIE_GENERATE_PATH), headers=_kie_headers(),
                          data=json.dumps(payload), timeout=30)
        if r.status_code == 200:
            data = r.json()
            return {"ok": True, "task_id": data.get("data",{}).get("taskId") or data.get("taskId") or "unknown"}
        return {"ok": False, "error": _map_api_error(r.status_code, r.text)}
    except Exception as e:
        return {"ok": False, "error": f"Сеть/таймаут: {e}"}

def submit_photo_job(image_url: str, prompt: str, aspect: str, model: str, enable_fallback: bool=False) -> Dict[str, Any]:
    payload = {
        "prompt": prompt,
        "imageUrls": [image_url],
        "model": model,
        "aspectRatio": "16:9" if aspect == "16:9" else "9:16",
        "enableFallback": bool(enable_fallback),
    }
    try:
        r = requests.post(_kie_url(KIE_GENERATE_PATH), headers=_kie_headers(),
                          data=json.dumps(payload), timeout=30)
        if r.status_code == 200:
            data = r.json()
            return {"ok": True, "task_id": data.get("data",{}).get("taskId") or data.get("taskId") or "unknown"}
        return {"ok": False, "error": _map_api_error(r.status_code, r.text)}
    except Exception as e:
        return {"ok": False, "error": f"Сеть/таймаут: {e}"}

# ----------------- HANDLERS -----------------
async def /start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    st = userstate(ctx); st["mode"] = None
    await update.effective_chat.send_message(
        "👋 Привет! Это бот Veo 3. Выбери режим и формат. "
        "Модель можно переключать: **Quality** (veo3) или **Fast** (veo3_fast).",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=kb_main(_model_label(st["model"]))
    )
    await update.effective_chat.send_message("Выбери соотношение сторон:", reply_markup=FORMAT_KB)

# (название функции не может начинаться со слеша — дублируем)
async def start_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):  # для совместимости
    await /start(update, ctx)

async def cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    st = userstate(ctx)
    data = q.data

    if data == "back_menu":
        await q.edit_message_text("Главное меню:", reply_markup=kb_main(_model_label(st["model"])))
        return

    if data == "toggle_model":
        st["model"] = "veo3_fast" if st["model"] == "veo3" else "veo3"
        await q.edit_message_text(f"Режим переключён: **{_model_label(st['model'])}**",
                                  parse_mode=ParseMode.MARKDOWN,
                                  reply_markup=kb_main(_model_label(st["model"])))
        return

    if data.startswith("fmt_"):
        st["aspect"] = "16:9" if data == "fmt_16x9" else "9:16"
        await q.edit_message_text(f"✅ Формат: {st['aspect']}.", reply_markup=RUN_KB)
        return

    if data == "faq":
        await q.edit_message_text(
            "📖 FAQ\n• Модели: `veo3` (Quality), `veo3_fast` (Fast)\n"
            "• Соотношение: 16:9 или 9:16\n• 1080p приходит только для 16:9\n"
            "• Возможен fallback (вкл. админом)\n• Статусы 402 = пополните кредиты.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=RUN_KB
        ); return

    if data == "gen_text":
        st["mode"] = "gen_text"; st["image_url"] = None
        await q.edit_message_text(
            "✍️ Пришли **идею или готовый промпт**. Если промпт готов — я не буду его переписывать.",
            reply_markup=FORMAT_KB
        ); return

    if data == "gen_photo":
        st["mode"] = "gen_photo"
        await q.edit_message_text(
            "📸 Пришли **фото**. Можно с подписью — это станет базой для промпта.",
            reply_markup=FORMAT_KB
        ); return

    if data == "prompt_master":
        st["mode"] = "prompt_master"; st["image_url"] = None
        await q.edit_message_text("🧠 Промпт-мастер включён. Опиши идею 1–2 фразами.", reply_markup=FORMAT_KB)
        return

    if data == "run":
        if not st.get("prompt"):
            await q.answer("Нет подготовленного промпта.", show_alert=True); return
        await q.edit_message_text("🚀 Отправляю задачу в Veo…")

        if st["mode"] == "gen_photo" and st.get("image_url"):
            res = submit_photo_job(st["image_url"], st["prompt"], st["aspect"], st["model"])
        else:
            res = submit_text_job(st["prompt"], st["aspect"], st["model"])

        if res["ok"]:
            await q.edit_message_text(
                f"✅ Задача создана! `taskId = {res['task_id']}`\nОбычно рендер 2–5 минут.",
                parse_mode=ParseMode.MARKDOWN, reply_markup=RUN_KB
            )
        else:
            await q.edit_message_text(f"❌ Не удалось создать задачу:\n{res['error']}", reply_markup=RUN_KB)
        return

async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    st = userstate(ctx)
    text = (update.message.text or "").strip()
    chat_id = update.effective_chat.id

    # Промпт-мастер = не переписываем идею пользователя, просто усиливаем её.
    if st["mode"] == "prompt_master":
        notice = await update.message.reply_text("⌛ Пишу кинематографический промпт…")
        # имитация «набирает» (без OpenAI — чтобы не зависеть)
        stop = asyncio.Event(); st["_typing_stop"] = stop
        asyncio.create_task(_typing_loop(chat_id, ctx, stop))
        try:
            # упрощённый шаблон (без внешних API)
            prompt = (
                f"{text}\n\n"
                "Camera: smooth push-in, natural DOF, subtle rack-focus. "
                "Lens: 35mm/85mm mix, gentle bokeh. Lighting: soft, cinematic, "
                "warm highlights and cool shadows. Details: micro-particles, "
                "skin speculars, cloth texture, breathing room. "
                "Sound: light ambience, airy foley; no logos or text in frame."
            )
            st["prompt"] = prompt
            stop.set()
            await notice.edit_text("✅ Промпт готов и сохранён. Жми «🚀 Сгенерировать».",
                                   reply_markup=RUN_KB)
        except Exception as e:
            stop.set()
            await notice.edit_text(f"❌ Ошибка при подготовке промпта: {e}")
        return

    # Обычная генерация текстом
    if st["mode"] in (None, "gen_text", "gen_photo"):
        if st["mode"] == "gen_photo" and not st.get("image_url"):
            await update.message.reply_text("Сначала пришли фото, потом текст."); return

        if looks_like_ready_prompt(text):
            st["prompt"] = text
            await update.message.reply_text("✅ Принял готовый промпт. Жми «🚀 Сгенерировать».",
                                            reply_markup=RUN_KB)
            return

        notice = await update.message.reply_text("⌛ Формулирую аккуратный промпт…")
        stop = asyncio.Event(); st["_typing_stop"] = stop
        asyncio.create_task(_typing_loop(chat_id, ctx, stop))
        try:
            # лёгкое усиление текста (без сторонних вызовов)
            st["prompt"] = (
                f"{text}\n\n"
                "Cinematic composition, motivated lighting, gentle camera motion (dolly / push-in), "
                "realistic textures, no on-screen text or logos."
            )
            stop.set()
            await notice.edit_text("✅ Промпт готов. Жми «🚀 Сгенерировать».", reply_markup=RUN_KB)
        except Exception as e:
            stop.set()
            await notice.edit_text(f"❌ Ошибка при подготовке промпта: {e}")
        return

async def on_photo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    st = userstate(ctx); chat_id = update.effective_chat.id
    try:
        photo = update.message.photo[-1]
        f = await ctx.bot.get_file(photo.file_id)
        image_url = f"https://api.telegram.org/file/bot{TELEGRAM_TOKEN}/{f.file_path}"
        st["image_url"] = image_url
        caption = (update.message.caption or "").strip()

        if caption:
            notice = await update.message.reply_text("📸 Принял фото. Пишу промпт…")
            stop = asyncio.Event(); st["_typing_stop"] = stop
            asyncio.create_task(_typing_loop(chat_id, ctx, stop))
            st["prompt"] = (
                f"{caption}\n\n"
                "Keep subject fidelity. Natural light, smooth parallax, true-to-photo colors, "
                "no text/logos in frame."
            )
            stop.set()
            await notice.edit_text("✅ Фото и промпт готовы. Жми «🚀 Сгенерировать».", reply_markup=RUN_KB)
        else:
            await update.message.reply_text("📸 Фото получено. Теперь напиши короткое описание сцены.",
                                            reply_markup=RUN_KB)
            st["mode"] = "gen_photo"
    except Exception as e:
        await update.message.reply_text(f"❌ Не удалось обработать фото: {e}")

async def exit_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data.clear()
    await update.message.reply_text("Ок, выходим. Открываю меню…", reply_markup=ReplyKeyboardRemove())
    await /start(update, ctx)

async def error_handler(update: Optional[Update], ctx: ContextTypes.DEFAULT_TYPE):
    log.error("Exception:\n%s", traceback.format_exc())
    try:
        if update and update.effective_chat:
            await update.effective_chat.send_message("⚠️ Упс, что-то пошло не так. Попробуйте ещё раз.")
    except:
        pass

# ----------------- MAIN -----------------
def main():
    if not TELEGRAM_TOKEN:
        raise RuntimeError("TELEGRAM_TOKEN не задан.")
    if not KIE_API_KEY:
        log.warning("KIE_API_KEY не задан — генерация будет падать на отправке задачи.")
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).concurrent_updates(True).build()

    # команды
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("exit", exit_cmd))

    # кнопки
    app.add_handler(CallbackQueryHandler(
        cb,
        pattern=r"^(gen_text|gen_photo|prompt_master|faq|run|back_menu|fmt_16x9|fmt_9x16|toggle_model)$"
    ))

    # сообщения
    app.add_handler(MessageHandler(filters.PHOTO, on_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    app.add_error_handler(error_handler)
    log.info("Bot started (polling).")
    app.run_polling(drop_pending_updates=True)

# из-за имени функции /start выше — создадим алиас корректного идентификатора
def /start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    return start_cmd(update, ctx)

if __name__ == "__main__":
    main()

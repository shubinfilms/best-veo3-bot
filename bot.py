# -*- coding: utf-8 -*-
# Best VEO3 bot — stable build: text/photo → KIE → poll → send video back
# python-telegram-bot 20+, requests, (optional) openai==0.28.x

import os, json, asyncio, logging, traceback, requests
from typing import Any, Dict, Optional

from dotenv import load_dotenv
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardRemove
from telegram.constants import ParseMode, ChatAction
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters
)

# --------------------------- ENV & LOG ---------------------------
load_dotenv()

TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN", "")
KIE_API_KEY      = os.getenv("KIE_API_KEY", "")

# базовый URL и пути KIE; можно менять в окружении при необходимости
KIE_BASE_URL     = (os.getenv("KIE_BASE_URL") or "https://api.kie.ai").strip().rstrip("/")
KIE_GEN_PATH     = (os.getenv("KIE_GEN_PATH") or os.getenv("KIE_GENERATE_PATH") or "/api/v1/veo/generate").strip()
KIE_GET_TASKPATH = (os.getenv("KIE_GET_TASK_PATH") or "/api/v1/common/get-task").strip()

OPENAI_API_KEY   = os.getenv("OPENAI_API_KEY", "") or os.getenv("OPENAI_KEY", "")

LOG_LEVEL = (os.getenv("LOG_LEVEL") or "INFO").upper()
logging.basicConfig(level=getattr(logging, LOG_LEVEL, logging.INFO),
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("best-veo3")

def _join(base: str, path: str) -> str:
    if not path.startswith("/"):
        path = "/" + path
    url = f"{base}{path}"
    # убрать возможные двойные слэши (кроме протокола)
    return url.replace("://", "§§").replace("//", "/").replace("§§", "://")

KIE_GENERATE_URL = _join(KIE_BASE_URL, KIE_GEN_PATH)
KIE_GET_TASK_URL = _join(KIE_BASE_URL, KIE_GET_TASKPATH)
log.info(f"KIE generate endpoint: {KIE_GENERATE_URL}")
log.info(f"KIE get-task endpoint: {KIE_GET_TASK_URL}")

# --------------------------- UI ---------------------------
MAIN_MENU = InlineKeyboardMarkup([
    [InlineKeyboardButton("🎬 Сгенерировать видео по тексту", callback_data="gen_text")],
    [InlineKeyboardButton("🖼️ Сгенерировать видео по фото",  callback_data="gen_photo")],
    [InlineKeyboardButton("🧠 Промпт-мастер (ChatGPT)",       callback_data="prompt_master")],
    [InlineKeyboardButton("💬 Обычный чат (ChatGPT)",         callback_data="chat")],
    [InlineKeyboardButton("❓ FAQ", callback_data="faq"),
     InlineKeyboardButton("📚 Канал с промптами", url="https://t.me/bestveo3promts")],
])

def kb_format(aspect: str, with_run: bool) -> InlineKeyboardMarkup:
    b16  = f"{'✅ ' if aspect=='16:9' else ''}🎬 16:9"
    b916 = f"{'✅ ' if aspect=='9:16' else ''}📱 9:16"
    rows = [[InlineKeyboardButton(b16, callback_data="fmt_16x9"),
             InlineKeyboardButton(b916, callback_data="fmt_9x16")]]
    if with_run:
        rows += [[InlineKeyboardButton("🚀 Запустить генерацию", callback_data="run")]]
    rows += [[InlineKeyboardButton("⬅️ Назад в меню", callback_data="back_menu")]]
    return InlineKeyboardMarkup(rows)

# --------------------------- STATE ---------------------------
def st(ctx: ContextTypes.DEFAULT_TYPE) -> Dict[str, Any]:
    if "state" not in ctx.user_data:
        ctx.user_data["state"] = {
            "mode": None,              # gen_text | gen_photo | prompt_master | chat
            "aspect": "16:9",
            "last_prompt": None,
            "last_image_url": None,
            "chat_history": [],
            "_typing_stop": None
        }
    return ctx.user_data["state"]

# --------------------------- Helpers ---------------------------
def looks_like_ready_prompt(text: str) -> bool:
    if not text: return False
    if text.strip().startswith("{") and "}" in text:  # JSON-like
        return True
    score = 0
    for kw in ["fps","anamorphic","85mm","35mm","lens","DOF","bokeh","rack focus",
               "color palette","lighting","camera","glide","push-in","tone","sound",
               "\"shot\"","\"scene\"","\"audio\"","cinematic"]:
        if kw.lower() in text.lower():
            score += 1
    return score >= 3 or len(text) > 400

async def typing_loop(chat_id: int, ctx: ContextTypes.DEFAULT_TYPE, stop_event: asyncio.Event):
    try:
        while not stop_event.is_set():
            await ctx.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
            await asyncio.sleep(4)
    except Exception:
        pass

# ----- Prompt-Master (OpenAI 0.28.x); если ключа нет — делаем fallback
PM_SYSTEM = {
    "role": "system",
    "content": (
        "Ты — режиссёр/промпт-сценарист для Veo3. Усиль идею пользователя: композиция, оптика (мм/анаморф), "
        "движение камеры (push-in, dolly, glide, rack focus), свет/палитра, темп/ритм, микро-детали, звук. "
        "Пиши по-английски, кинематографично, 3–6 абзацев (500–900 chars). Без текста/логотипов в кадре."
    )
}
def build_prompt_with_openai(user_text: str) -> str:
    if not OPENAI_API_KEY:
        # Fallback — простой шаблон на англ., чтобы бот никогда не падал
        base = user_text.strip()[:240]
        return (
            f"{base}\n\n"
            "Camera opens with a slow push-in; lens 35mm, soft anamorphic flare. Warm key light, cool rim. "
            "Add micro-details (dust, steam, reflections). Keep pacing dynamic with short beats. "
            "No text or logos. Capture cinematic depth and natural soundscape."
        )
    try:
        import openai
        openai.api_key = OPENAI_API_KEY
        resp = openai.ChatCompletion.create(
            model="gpt-4o-mini",
            messages=[PM_SYSTEM, {"role": "user", "content": user_text}],
            temperature=0.7,
            max_tokens=900
        )
        return resp.choices[0].message["content"].strip()
    except Exception as e:
        log.error(f"OpenAI error: {e}")
        return build_prompt_with_openai("")  # fallback

# --------------------------- KIE API ---------------------------
def submit_kie(prompt: str, aspect: str, image_url: Optional[str]=None) -> Dict[str, Any]:
    """Создаём задачу в Veo3. Модель фиксируем как 'veo3' (80 кредитов)."""
    if not (KIE_API_KEY and KIE_BASE_URL):
        return {"ok": False, "id": None, "error": "KIE_API_KEY или KIE_BASE_URL не заданы."}

    payload = {
        "model": "veo3",
        "prompt": prompt,
        "aspect_ratio": "16:9" if aspect == "16:9" else "9:16",
    }
    if image_url:
        payload["image_url"] = image_url

    headers = {"Authorization": f"Bearer {KIE_API_KEY}", "Content-Type": "application/json"}
    log.info(f"KIE POST -> {KIE_GENERATE_URL} | {payload['aspect_ratio']} | img:{'y' if image_url else 'n'}")

    try:
        r = requests.post(KIE_GENERATE_URL, headers=headers, data=json.dumps(payload), timeout=60)
        data = {}
        try:
            data = r.json()
        except Exception:
            pass

        # KIE обычно возвращает {"code":200,"msg":"success","data":{"taskid":"..."}} при успехе
        if r.status_code == 200:
            # если в теле есть code и он 200 или 0 — это успех
            body_code = (data.get("code") if isinstance(data, dict) else None)
            if body_code in (None, 0, 200):
                taskid = None
                if isinstance(data, dict):
                    # некоторые возвращают taskid на верхнем уровне, некоторые в data
                    taskid = data.get("taskid") or (data.get("data") or {}).get("taskid") \
                             or data.get("id") or (data.get("data") or {}).get("id")
                return {"ok": True, "id": taskid or "unknown", "error": None}
            # иначе — это бизнес-ошибка в теле
            return {"ok": False, "id": None, "error": f"KIE code {body_code}: {data.get('msg')}"}

        # HTTP-ошибка
        preview = (r.text or "")[:300]
        if r.status_code == 402:
            return {"ok": False, "id": None, "error": "Недостаточно кредитов на KIE аккаунте."}
        if "Illegal IP" in preview or r.status_code in (401, 403):
            return {"ok": False, "id": None, "error": "Доступ API запрещён (whitelist IP)."}
        return {"ok": False, "id": None, "error": f"API {r.status_code}: {preview}"}

    except Exception as e:
        return {"ok": False, "id": None, "error": f"Network error: {e}"}

async def poll_and_send_video(taskid: str, chat_id: int, ctx: ContextTypes.DEFAULT_TYPE):
    """Опрос состояния и отправка видео в Telegram когда готово."""
    headers = {"Authorization": f"Bearer {KIE_API_KEY}", "Content-Type": "application/json"}
    # отправляем оба поля на всякий случай: разные провайдеры принимают taskid/task_id
    body = {"taskid": taskid, "task_id": taskid}

    # ждём до ~6 минут (72 * 5 сек)
    for i in range(72):
        try:
            r = requests.post(KIE_GET_TASK_URL, headers=headers, data=json.dumps(body), timeout=30)
            data = r.json() if r.headers.get("content-type","").startswith("application/json") else {}
            status = (data.get("data") or {}).get("status") or data.get("status")
            if status == "success":
                # берем первый url
                urls = (data.get("data") or {}).get("result_urls") or data.get("result_urls") or []
                if not urls:
                    break
                video_url = urls[0]
                try:
                    await ctx.bot.send_video(chat_id=chat_id, video=video_url, supports_streaming=True)
                except Exception as send_err:
                    # если Telegram не принял как видео — отправим ссылкой
                    await ctx.bot.send_message(chat_id=chat_id, text=f"🎬 Видео готово: {video_url}")
                    log.warning(f"send_video fallback to link: {send_err}")
                return
            elif status in ("failed", "error"):
                msg = (data.get("data") or {}).get("msg") or data.get("msg") or "unknown"
                await ctx.bot.send_message(chat_id=chat_id, text=f"❌ Генерация не удалась: {msg}")
                return
        except Exception as e:
            log.error(f"Polling error: {e}")
        await asyncio.sleep(5)

    await ctx.bot.send_message(chat_id=chat_id, text="⚠️ Не удалось получить видео в срок. Попробуйте позже.")

# --------------------------- Handlers ---------------------------
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    s = st(ctx); s["mode"] = None
    await update.effective_chat.send_message(
        "👋 Привет! Это бот Google Veo 3. Выберите режим и формат кадра.",
        reply_markup=MAIN_MENU
    )
    await update.effective_chat.send_message("Выбери формат:", reply_markup=kb_format(s["aspect"], with_run=False))

async def callbacks(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    s = st(ctx); data = q.data

    if data == "back_menu":
        await q.edit_message_text("Главное меню:", reply_markup=MAIN_MENU); return

    if data.startswith("fmt_"):
        s["aspect"] = "16:9" if data == "fmt_16x9" else "9:16"
        await q.edit_message_text(f"✅ Формат: {s['aspect']}.", reply_markup=kb_format(s["aspect"], with_run=bool(s["last_prompt"]))); return

    if data == "gen_text":
        s["mode"] = "gen_text"; s["last_image_url"] = None
        s["last_prompt"] = None
        await q.edit_message_text("✍️ Пришли идею или готовый промпт. После этого появится кнопка запуска.",
                                  reply_markup=kb_format(s["aspect"], with_run=False)); return

    if data == "gen_photo":
        s["mode"] = "gen_photo"; s["last_prompt"] = None
        await q.edit_message_text("📸 Пришли фото (можно с подписью). После получения фото — сформирую промпт.",
                                  reply_markup=kb_format(s["aspect"], with_run=False)); return

    if data == "prompt_master":
        s["mode"] = "prompt_master"; s["last_image_url"] = None
        s["last_prompt"] = None
        await q.edit_message_text("🧠 Промпт-мастер включён. Опиши идею 1–2 фразами — **сразу напишу промпт**.",
                                  reply_markup=kb_format(s["aspect"], with_run=False)); return

    if data == "chat":
        s["mode"] = "chat"
        await q.edit_message_text("💬 Обычный чат. Пиши сообщения. /exit — выход.",
                                  reply_markup=kb_format(s["aspect"], with_run=False)); return

    if data == "faq":
        await q.edit_message_text(
            "📖 FAQ\n• Примеры: https://t.me/bestveo3promts\n• Форматы: 16:9 и 9:16\n"
            "• Рендер обычно 2–5 мин.\n• Без текста/логотипов в кадре.",
            reply_markup=kb_format(s["aspect"], with_run=bool(s["last_prompt"]))
        ); return

    if data == "run":
        if not s.get("last_prompt"):
            await q.answer("Нет подготовленного промпта.", show_alert=True); return
        await q.edit_message_text("🚀 Отправляю задачу в Veo3…")
        res = submit_kie(s["last_prompt"], s["aspect"], s.get("last_image_url"))
        if res["ok"]:
            taskid = res["id"]
            await q.edit_message_text(
                f"✅ Задача создана. ID: `{taskid}`\nОбычно рендер 2–5 минут.",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=kb_format(s["aspect"], with_run=True)
            )
            # запускаем опрос (не блокируем поток)
            ctx.application.create_task(poll_and_send_video(taskid, update.effective_chat.id, ctx))
        else:
            await q.edit_message_text(f"❌ Ошибка запуска генерации: {res['error']}",
                                      reply_markup=kb_format(s["aspect"], with_run=True))
        return

async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    s = st(ctx); text = (update.message.text or "").strip()
    chat_id = update.effective_chat.id

    # обычный чат
    if s["mode"] == "chat":
        try:
            # простой эхо (чтобы не трогать ChatGPT-часть, если ключей нет)
            await update.message.reply_text(text)
        except Exception as e:
            await update.message.reply_text(f"Ошибка чата: {e}")
        return

    # prompt master
    if s["mode"] == "prompt_master":
        notice = await update.message.reply_text("⌛ Пишу кинематографический промпт…")
        s["_typing_stop"] = asyncio.Event()
        asyncio.create_task(typing_loop(chat_id, ctx, s["_typing_stop"]))
        try:
            prompt = build_prompt_with_openai(text)
            s["last_prompt"] = prompt
            s["_typing_stop"].set()
            await notice.edit_text("🧠 Готовый промпт для Veo3:",
                                   reply_markup=kb_format(s["aspect"], with_run=True))
            await update.effective_chat.send_message(f"<pre>{prompt}</pre>", parse_mode=ParseMode.HTML)
        except Exception as e:
            s["_typing_stop"].set()
            await notice.edit_text(f"❌ Prompt-Master error: {e}")
        return

    # генерация по тексту/фото
    if s["mode"] in (None, "gen_text", "gen_photo"):
        if s["mode"] == "gen_photo" and not s.get("last_image_url"):
            await update.message.reply_text("Нужно фото. Пришли изображение — потом сформулирую промпт.")
            return

        if looks_like_ready_prompt(text):
            s["last_prompt"] = text
            await update.message.reply_text("✅ Принял готовый промпт.", reply_markup=kb_format(s["aspect"], with_run=True))
            return

        notice = await update.message.reply_text("⌛ Формулирую кинематографический промпт…")
        s["_typing_stop"] = asyncio.Event()
        asyncio.create_task(typing_loop(chat_id, ctx, s["_typing_stop"]))
        try:
            prompt = build_prompt_with_openai(text)
            s["last_prompt"] = prompt
            s["_typing_stop"].set()
            await notice.edit_text("✅ Промпт готов. Нажми «🚀 Запустить генерацию».",
                                   reply_markup=kb_format(s["aspect"], with_run=True))
        except Exception as e:
            s["_typing_stop"].set()
            await notice.edit_text(f"❌ Ошибка при подготовке промпта: {e}")
        return

async def on_photo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    s = st(ctx); chat_id = update.effective_chat.id
    try:
        photo = update.message.photo[-1]
        f = await ctx.bot.get_file(photo.file_id)
        image_url = f"https://api.telegram.org/file/bot{TELEGRAM_TOKEN}/{f.file_path}"
        s["last_image_url"] = image_url

        caption = (update.message.caption or "").strip()
        if caption:
            notice = await update.message.reply_text("📸 Фото получено. ⌛ Формулирую промпт…")
            s["_typing_stop"] = asyncio.Event()
            asyncio.create_task(typing_loop(chat_id, ctx, s["_typing_stop"]))
            try:
                prompt = build_prompt_with_openai(caption)
                s["last_prompt"] = prompt
                s["_typing_stop"].set()
                await notice.edit_text("✅ Фото и промпт готовы. Нажми «🚀 Запустить генерацию».",
                                       reply_markup=kb_format(s["aspect"], with_run=True))
            except Exception as e:
                s["_typing_stop"].set()
                await notice.edit_text(f"❌ Ошибка при подготовке промпта: {e}")
        else:
            s["mode"] = "gen_photo"
            await update.message.reply_text(
                "📸 Фото получено. Напиши короткое **описание сцены** — сформирую промпт.",
                reply_markup=kb_format(s["aspect"], with_run=False)
            )
    except Exception as e:
        await update.message.reply_text(f"❌ Не удалось обработать фото: {e}")

async def exit_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data.clear()
    await update.message.reply_text("Вышел из режима. Открываю меню…", reply_markup=ReplyKeyboardRemove())
    await start(update, ctx)

async def error_handler(update: Optional[Update], ctx: ContextTypes.DEFAULT_TYPE):
    log.error("Exception:\n%s", traceback.format_exc())
    try:
        if update and update.effective_chat:
            await update.effective_chat.send_message("⚠️ Что-то пошло не так. Попробуйте ещё раз.")
    except:
        pass

# --------------------------- MAIN ---------------------------
def main():
    if not TELEGRAM_TOKEN:
        raise RuntimeError("TELEGRAM_TOKEN не задан.")
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).concurrent_updates(True).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("exit", exit_cmd))
    app.add_handler(CallbackQueryHandler(
        callbacks,
        pattern=r"^(gen_text|gen_photo|prompt_master|chat|faq|run|back_menu|fmt_16x9|fmt_9x16)$"
    ))
    app.add_handler(MessageHandler(filters.PHOTO, on_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_error_handler(error_handler)

    log.info("Bot started.")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()

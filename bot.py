# -*- coding: utf-8 -*-
# BEST VEO3 BOT — text & photo generation + Prompt-Master
# PTB v20+, requests, openai==0.28.1
# Всегда model = "veo3_fast"

import os, json, time, logging, traceback, requests, asyncio
from typing import Optional, Dict, Any, List

from dotenv import load_dotenv
from telegram import (
    Update, InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardRemove, InputFile
)
from telegram.constants import ParseMode, ChatAction
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters
)

# --------------- ENV & LOG ---------------
load_dotenv()

TELEGRAM_TOKEN  = os.getenv("TELEGRAM_TOKEN", "")
OPENAI_API_KEY  = os.getenv("OPENAI_API_KEY", "") or os.getenv("OPENAI_KEY", "")
KIE_API_KEY     = os.getenv("KIE_API_KEY", "")

KIE_BASE_URL    = (os.getenv("KIE_BASE_URL") or "https://api.kie.ai").strip().rstrip("/")
RAW_PATH        = (os.getenv("KIE_GEN_PATH") or os.getenv("KIE_GENERATE_PATH") or "/api/v1/veo/generate").strip()

def _normalize_path(p: str) -> str:
    if not p.startswith("/"):
        p = "/" + p
    if p.startswith("/v1/"):
        p = "/api" + p
    return p

KIE_GEN_PATH = _normalize_path(RAW_PATH)

LOG_LEVEL = (os.getenv("LOG_LEVEL") or "INFO").upper()
logging.basicConfig(level=getattr(logging, LOG_LEVEL, logging.INFO),
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("best-veo3")
log.info(f"KIE endpoint: {KIE_BASE_URL}{KIE_GEN_PATH}")

# --------------- UI ---------------
MAIN_KB = InlineKeyboardMarkup([
    [InlineKeyboardButton("🎬 Сгенерировать видео по тексту", callback_data="mode_text")],
    [InlineKeyboardButton("🖼️ Сгенерировать видео по фото",  callback_data="mode_photo")],
    [InlineKeyboardButton("🧠 Промпт-мастер (ChatGPT)",       callback_data="mode_pm")],
    [InlineKeyboardButton("💬 Обычный чат (ChatGPT)",         callback_data="mode_chat")],
    [InlineKeyboardButton("❓ FAQ", callback_data="faq"),
     InlineKeyboardButton("📈 Канал с промптами", url="https://t.me/bestveo3promts")]
])

def kb_aspect(aspect: str, with_run: bool=False):
    b16  = f"{'✅ ' if aspect=='16:9' else ''}🎬 16:9"
    b916 = f"{'✅ ' if aspect=='9:16' else ''}📱 9:16"
    rows = [[InlineKeyboardButton(b16, callback_data="fmt_16x9"),
             InlineKeyboardButton(b916, callback_data="fmt_9x16")]]
    if with_run:
        rows.append([InlineKeyboardButton("🚀 Запустить генерацию", callback_data="run")])
    rows.append([InlineKeyboardButton("⬅️ Назад в меню", callback_data="back")])
    return InlineKeyboardMarkup(rows)

def kb_run():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🚀 Запустить генерацию", callback_data="run")],
        [InlineKeyboardButton("⬅️ Назад в меню", callback_data="back")]
    ])

# --------------- STATE ---------------
def state(ctx: ContextTypes.DEFAULT_TYPE) -> Dict[str, Any]:
    if "state" not in ctx.user_data:
        ctx.user_data["state"] = {
            "mode": None,          # text | photo | pm | chat
            "aspect": "16:9",
            "last_prompt": None,
            "last_image_url": None,
            "chat": []
        }
    return ctx.user_data["state"]

# --------------- Helpers ---------------
def _looks_like_ready_prompt(text: str) -> bool:
    if not text: return False
    score = 0
    for kw in ["camera", "lighting", "lens", "mm", "bokeh", "rack", "dolly", "fps", "grade"]:
        if kw in text.lower(): score += 1
    return score >= 2 or len(text) > 400

async def _typing(ctx: ContextTypes.DEFAULT_TYPE, chat_id: int, stop: asyncio.Event):
    try:
        while not stop.is_set():
            await ctx.bot.send_chat_action(chat_id, ChatAction.TYPING)
            await asyncio.sleep(4)
    except Exception:
        pass

# --------------- OpenAI (Prompt-Master) ---------------
def build_prompt_master(idea: str) -> str:
    """
    Генерируем кинематографичный англ. промпт 500–900 симв.
    Используем openai==0.28.1 (старый API).
    """
    if not OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY не задан.")
    import openai
    openai.api_key = OPENAI_API_KEY

    system = {
        "role": "system",
        "content": (
            "You are a senior film director and prompt-writer for Google Veo 3. "
            "Take the user's idea and craft a vivid, cinematic English prompt (500–900 characters), "
            "including: composition, lens (mm/anamorphic), camera motion (push-in, dolly, glide, rack focus), "
            "lighting & color palette, micro-details (dust, steam, reflections), atmosphere and sound cues. "
            "No brand names, logos, or on-screen text. Use natural, evocative language."
        )
    }
    user = {"role":"user","content": idea.strip()}
    resp = openai.ChatCompletion.create(
        model="gpt-4o-mini",
        messages=[system, user],
        temperature=0.8,
        max_tokens=900
    )
    return resp.choices[0].message["content"].strip()

# --------------- KIE / VEO3 FAST ---------------
def _kie_url() -> str:
    url = f"{KIE_BASE_URL}{KIE_GEN_PATH}"
    return url.replace("://","§§").replace("//","/").replace("§§","://")

def _submit_task(prompt: str, aspect: str, image_url: Optional[str]=None) -> Dict[str,Any]:
    """
    Возвращает {ok, task_id, error}
    """
    url = _kie_url()
    headers = {"Authorization": f"Bearer {KIE_API_KEY}", "Content-Type":"application/json"}
    payload = {
        "prompt": prompt,
        "model": "veo3_fast",
        "aspectRatio": "16:9" if aspect=="16:9" else "9:16"
    }
    if image_url:
        payload["imageUrls"] = [image_url]

    try:
        log.info(f"KIE POST -> {url} | aspect={payload['aspectRatio']} | model=veo3_fast | img={bool(image_url)}")
        r = requests.post(url, headers=headers, data=json.dumps(payload), timeout=60)
    except Exception as e:
        return {"ok": False, "task_id": None, "error": f"Network error: {e}"}

    # Часть провайдеров возвращают внутри тела code/msg
    try:
        data = r.json()
    except Exception:
        data = {}

    if r.status_code == 200 and isinstance(data, dict) and (data.get("code") in (0,200) or "taskId" in (data.get("data") or {})):
        task_id = data.get("data",{}).get("taskId") or data.get("taskId") or data.get("id") or "unknown"
        return {"ok": True, "task_id": task_id, "error": None}

    # Разбор типовых ошибок
    body = data if isinstance(data,dict) else {"raw": r.text[:400]}
    code = data.get("code")
    msg  = data.get("msg") or data.get("message") or r.reason

    if r.status_code == 402 or code == 402:
        return {"ok": False, "task_id": None, "error": "Недостаточно кредитов на аккаунте Kie.ai (код 402)."}

    if r.status_code in (401,403):
        return {"ok": False, "task_id": None, "error": "Авторизация отклонена: проверь KIE_API_KEY / whitelist IP."}

    if r.status_code == 404:
        return {"ok": False, "task_id": None, "error": "Эндпоинт не найден (404). Проверь KIE_BASE_URL и KIE_GEN_PATH."}

    return {"ok": False, "task_id": None, "error": f"Ошибка запуска: HTTP {r.status_code}, code={code}, msg={msg}"}

def _check_status(task_id: str) -> Dict[str,Any]:
    """
    GET /api/v1/veo/record-info?taskId=...
    Возвращает:
      successFlag: 0 — идёт, 1 — готово, 2/3 — ошибка
      resultUrls: JSON-строка со списком URL
    """
    url = f"{KIE_BASE_URL}/api/v1/veo/record-info"
    headers = {"Authorization": f"Bearer {KIE_API_KEY}"}
    try:
        r = requests.get(url, headers=headers, params={"taskId": task_id}, timeout=30)
        data = r.json()
    except Exception as e:
        return {"ok": False, "status": None, "urls": [], "error": f"Network error: {e}"}

    if r.status_code != 200 or data.get("code") not in (0,200):
        return {"ok": False, "status": None, "urls": [], "error": data.get("msg") or f"HTTP {r.status_code}"}

    info = data.get("data") or {}
    flag = info.get("successFlag")
    urls = []
    try:
        if info.get("resultUrls"):
            urls = json.loads(info["resultUrls"])
    except Exception:
        pass
    return {"ok": True, "status": flag, "urls": urls, "error": None}

# --------------- Handlers ---------------
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    st = state(ctx); st["mode"] = None
    await update.effective_chat.send_message(
        "👋 Привет! Это бот Veo3 (Fast). Выбери режим ниже.",
        reply_markup=MAIN_KB
    )
    await update.effective_chat.send_message("Выбери формат кадра:", reply_markup=kb_aspect(st["aspect"]))

async def callbacks(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    st = state(ctx)
    data = q.data

    if data == "back":
        await q.edit_message_text("Главное меню:", reply_markup=MAIN_KB); return

    if data == "fmt_16x9":
        st["aspect"] = "16:9"
        await q.edit_message_text("Формат 16:9 выбран.", reply_markup=kb_aspect(st["aspect"], with_run=bool(st["last_prompt"]))); return

    if data == "fmt_9x16":
        st["aspect"] = "9:16"
        await q.edit_message_text("Формат 9:16 выбран.", reply_markup=kb_aspect(st["aspect"], with_run=bool(st["last_prompt"]))); return

    if data == "mode_text":
        st["mode"] = "text"; st["last_prompt"] = None
        await q.edit_message_text(
            "✍️ Пришлите **описание видео** (идею или готовый промпт).\n\n"
            "Когда закончите — нажмите «🚀 Запустить генерацию».",
            reply_markup=kb_aspect(st["aspect"])
        ); return

    if data == "mode_photo":
        st["mode"] = "photo"; st["last_image_url"] = None; st["last_prompt"] = None
        await q.edit_message_text(
            "📸 Пришлите **фото** (с подписью-идеей — по желанию).",
            reply_markup=kb_aspect(st["aspect"])
        ); return

    if data == "mode_pm":
        st["mode"] = "pm"; st["last_prompt"] = None
        await q.edit_message_text(
            "🧠 Режим «Промпт-мастер» активирован.\n"
            "Отправьте **идею в 1–2 фразах** — я сразу верну **готовый англ. промпт (500–900 симв.)** "
            "и предложу запустить генерацию.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад в меню", callback_data="back")]])
        ); return

    if data == "mode_chat":
        st["mode"] = "chat"; st["chat"].clear()
        await q.edit_message_text("💬 Обычный чат включён. Пишите сообщения. /exit — выход.",
                                  reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад в меню", callback_data="back")]]))
        return

    if data == "faq":
        await q.edit_message_text(
            "📖 FAQ\n• Форматы: 16:9 и 9:16\n• Модель: Veo3 Fast\n• Срок рендера обычно 2–5 минут.\n"
            "• В кадре — никаких логотипов и субтитров.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⬅️ Назад в меню", callback_data="back"),
                 InlineKeyboardButton("📈 Канал с промптами", url="https://t.me/bestveo3promts")]
            ])
        ); return

    if data == "run":
        if not st.get("last_prompt"):
            await q.answer("Нет промпта.", show_alert=True); return
        await q.edit_message_text("🚀 Отправляю задачу в Veo3 Fast…")
        img = st.get("last_image_url")
        res = _submit_task(st["last_prompt"], st["aspect"], image_url=img)
        if not res["ok"]:
            await q.edit_message_text(f"❌ Ошибка запуска генерации: {res['error']}\n", reply_markup=kb_run()); return

        task_id = res["task_id"]
        await q.edit_message_text(f"🚀 Задача отправлена. ID: `{task_id}`\nЖдём результат…", parse_mode=ParseMode.MARKDOWN)

        # Поллинг статуса
        started = time.time()
        while True:
            await asyncio.sleep(15)
            status = _check_status(task_id)
            if not status["ok"]:
                await q.edit_message_text(f"⚠️ Ошибка статуса: {status['error']}", reply_markup=kb_run())
                break

            if status["status"] == 0:
                # идёт
                elapsed = int(time.time() - started)
                await q.edit_message_text(f"⏳ Генерация идёт… {elapsed} сек\nID: `{task_id}`", parse_mode=ParseMode.MARKDOWN)
                # ограничим ожидание ~8 минут
                if elapsed > 8*60:
                    await q.edit_message_text("⌛ Время ожидания вышло. Попробуйте позже / ещё раз.", reply_markup=kb_run())
                    break

            elif status["status"] == 1:
                urls: List[str] = status["urls"]
                if not urls:
                    await q.edit_message_text("✅ Готово, но ссылки не получены. Откройте историю задач в Kie.ai.", reply_markup=kb_run())
                    break
                # Пошлём первую ссылку (и список)
                await q.edit_message_text("✅ Видео готово! Отправляю ссылки…")
                text = "🎬 *Результат:*\n" + "\n".join([f"- {u}" for u in urls])
                await q.message.chat.send_message(text, parse_mode=ParseMode.MARKDOWN, disable_web_page_preview=False)
                break

            else:
                await q.edit_message_text("❌ Генерация не удалась. Попробуйте изменить промпт или формат.", reply_markup=kb_run())
                break

# --- Text / Photo / Chat / PM ---
async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    st = state(ctx)
    text = (update.message.text or "").strip()

    # обычный чат
    if st["mode"] == "chat":
        await update.message.reply_text("🤝 (чат-режим) Давай вернёмся к генерации — нажми «Назад в меню».")
        return

    # prompt-master — сразу генерим готовый промпт
    if st["mode"] == "pm":
        typing_stop = asyncio.Event()
        asyncio.create_task(_typing(ctx, update.effective_chat.id, typing_stop))
        try:
            prompt = build_prompt_master(text)
        except Exception as e:
            typing_stop.set()
            msg = f"❌ Prompt-Master error:\n{e}"
            await update.message.reply_text(msg)
            return
        typing_stop.set()
        st["last_prompt"] = prompt
        await update.message.reply_text(
            "🧠 *Готовый промпт для Veo3 (Fast):*",
            parse_mode=ParseMode.MARKDOWN
        )
        await update.message.reply_text(f"```\n{prompt}\n```", parse_mode=ParseMode.MARKDOWN)
        await update.message.reply_text("Можем запускать рендер:", reply_markup=kb_aspect(st["aspect"], with_run=True))
        return

    # режимы генерации
    if st["mode"] in (None, "text", "photo"):
        # если прислали уже сформированный промпт — не переписываем
        if _looks_like_ready_prompt(text):
            st["last_prompt"] = text
            await update.message.reply_text("✅ Принял промпт. Готов к запуску.", reply_markup=kb_aspect(st["aspect"], with_run=True))
            return
        # иначе превратим идею в кинопромпт через Prompt-Master
        typing_stop = asyncio.Event()
        asyncio.create_task(_typing(ctx, update.effective_chat.id, typing_stop))
        try:
            prompt = build_prompt_master(text)
        except Exception as e:
            typing_stop.set()
            await update.message.reply_text(f"❌ Не удалось сформировать промпт: {e}")
            return
        typing_stop.set()
        st["last_prompt"] = prompt
        await update.message.reply_text("🧠 Предложение промпта:")
        await update.message.reply_text(f"```\n{prompt}\n```", parse_mode=ParseMode.MARKDOWN)
        await update.message.reply_text("Нажмите «🚀 Запустить генерацию», когда будете готовы.", reply_markup=kb_aspect(st["aspect"], with_run=True))
        return

async def on_photo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    st = state(ctx)
    st["mode"] = "photo"
    try:
        photo = update.message.photo[-1]
        f = await ctx.bot.get_file(photo.file_id)
        st["last_image_url"] = f"https://api.telegram.org/file/bot{TELEGRAM_TOKEN}/{f.file_path}"
        caption = (update.message.caption or "").strip()
        if caption:
            # сразу сделаем промпт из подписи
            prompt = build_prompt_master(caption)
            st["last_prompt"] = prompt
            await update.message.reply_text("📸 Фото получено. Промпт подготовлен:")
            await update.message.reply_text(f"```\n{prompt}\n```", parse_mode=ParseMode.MARKDOWN)
            await update.message.reply_text("Готов к запуску:", reply_markup=kb_aspect(st["aspect"], with_run=True))
        else:
            await update.message.reply_text("📸 Фото получено. Пришлите идею текстом — я подготовлю промпт.")
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

# --------------- MAIN ---------------
def main():
    if not TELEGRAM_TOKEN:
        raise RuntimeError("TELEGRAM_TOKEN не задан.")
    if not KIE_API_KEY:
        log.warning("KIE_API_KEY пуст — генерация не сработает.")
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).concurrent_updates(True).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("exit", exit_cmd))
    app.add_handler(CallbackQueryHandler(callbacks,
        pattern=r"^(mode_text|mode_photo|mode_pm|mode_chat|faq|fmt_16x9|fmt_9x16|back|run)$"))
    app.add_handler(MessageHandler(filters.PHOTO, on_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_error_handler(error_handler)

    log.info("Bot started.")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()

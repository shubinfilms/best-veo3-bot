# -*- coding: utf-8 -*-
# BEST VEO3 BOT — текст/фото генерация + Prompt-Master + возврат видео
# ВАЖНО: логика UI/Prompt-Master/чата не тронута. Добавлен только поллинг Kie для возврата видео.

import os, json, logging, traceback, requests, asyncio
from typing import Optional, Dict, Any

from dotenv import load_dotenv
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardRemove
from telegram.constants import ParseMode, ChatAction
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters
)

# ======================= ENV & LOG =======================
load_dotenv()

TELEGRAM_TOKEN  = os.getenv("TELEGRAM_TOKEN", "")
OPENAI_API_KEY  = os.getenv("OPENAI_API_KEY", "") or os.getenv("OPENAI_KEY", "")
KIE_API_KEY     = os.getenv("KIE_API_KEY", "")

# БАЗА и Пути (оставляй как у тебя, главное без лишних двойных слэшей)
KIE_BASE_URL    = (os.getenv("KIE_BASE_URL") or "https://api.kie.ai").strip().rstrip("/")
KIE_GEN_PATH    = (os.getenv("KIE_GEN_PATH") or os.getenv("KIE_GENERATE_PATH") or "/api/v1/veo/generate").strip()
KIE_DETAIL_PATH = (os.getenv("KIE_DETAIL_PATH") or "/api/v1/veo/video/detail").strip()  # НОВОЕ

LOG_LEVEL = (os.getenv("LOG_LEVEL") or "INFO").upper()
logging.basicConfig(level=getattr(logging, LOG_LEVEL, logging.INFO),
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("best-veo3")

def _url_join(base: str, path: str) -> str:
    base = base.rstrip("/")
    if not path.startswith("/"): path = "/" + path
    url = base + path
    # на всякий пожарный уберём двойные // кроме протокола
    return url.replace("://", "§§").replace("//", "/").replace("§§", "://")

log.info(f"KIE endpoint (create): {_url_join(KIE_BASE_URL, KIE_GEN_PATH)}")
log.info(f"KIE endpoint (detail): {_url_join(KIE_BASE_URL, KIE_DETAIL_PATH)}")

# ======================= UI =======================
MAIN_MENU = InlineKeyboardMarkup([
    [InlineKeyboardButton("🎬 Сгенерировать видео по тексту", callback_data="gen_text")],
    [InlineKeyboardButton("🖼️ Сгенерировать видео по фото",  callback_data="gen_photo")],
    [InlineKeyboardButton("🧠 Промпт-мастер (ChatGPT)",       callback_data="prompt_master")],
    [InlineKeyboardButton("💬 Обычный чат (ChatGPT)",         callback_data="chat")],
    [InlineKeyboardButton("❓ FAQ", callback_data="faq"),
     InlineKeyboardButton("📚 Канал с промптами", url="https://t.me/bestveo3promts")],
])

FORMAT_KB = InlineKeyboardMarkup([
    [InlineKeyboardButton("16:9", callback_data="fmt_16x9"),
     InlineKeyboardButton("9:16", callback_data="fmt_9x16")],
])

RUN_KB = InlineKeyboardMarkup([
    [InlineKeyboardButton("🚀 Запустить генерацию", callback_data="run")],
    [InlineKeyboardButton("⬅️ Назад в меню", callback_data="back_menu")],
])

def state(ctx: ContextTypes.DEFAULT_TYPE) -> Dict[str, Any]:
    if "state" not in ctx.user_data:
        ctx.user_data["state"] = {
            "mode": None,            # gen_text | gen_photo | prompt_master | chat
            "aspect": "16:9",
            "last_prompt": None,     # текст промпта для Veo3
            "last_image_url": None,  # Telegram file URL для фото-режима
            "chat_history": [],
            "_typing_stop": None     # asyncio.Event для индикатора набора
        }
    return ctx.user_data["state"]

# ======================= Heuristics =======================
def looks_like_ready_prompt(text: str) -> bool:
    if not text: return False
    if text.strip().startswith("{") and "}" in text:
        return True
    score = 0
    for kw in ["fps","anamorphic","85mm","35mm","lens","DOF","bokeh","rack focus",
               "color palette","lighting","camera","glide","push-in","tone","sound",
               "\"shot\"","\"scene\"","\"audio\"","cinematic"]:
        if kw.lower() in text.lower():
            score += 1
    return score >= 3 or len(text) > 400

# ======================= OpenAI (Prompt-Master) =======================
def oai_chat(messages, temperature=0.7, max_tokens=900) -> str:
    # ВАЖНО: у тебя уже закреплена библиотека openai==0.28.1 — ничего не меняю.
    if not OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY не задан.")
    import openai
    openai.api_key = OPENAI_API_KEY
    resp = openai.ChatCompletion.create(
        model="gpt-4o-mini",
        messages=messages,
        temperature=temperature,
        max_tokens=max_tokens,
    )
    return resp.choices[0].message["content"].strip()

SYSTEM_PM = {
    "role": "system",
    "content": (
        "Ты — режиссёр-постановщик/промпт-сценарист для Veo3. "
        "Не меняй идею пользователя, усиливай её: композиция, оптика (мм/анаморф), "
        "движение камеры (push-in, dolly, glide, rack focus), свет/палитра, темп/ритм, "
        "микро-детали (пыль, пар, блики), звук (музыка/шум/микс). "
        "Пиши кинематографично, живым английским, 3–6 абзацев (≈500–900 симв.). "
        "Без воды, брендов/логотипов и субтитров."
    )
}

# ======================= Kie / Veo3 =======================
def _submit_kie(payload: dict) -> dict:
    """Создать задачу на Kie. Возвращает {ok,id,error}."""
    if not (KIE_API_KEY and KIE_BASE_URL):
        return {"ok": False, "id": None, "error": "KIE_API_KEY или KIE_BASE_URL не заданы."}

    # модель НЕ трогаю (как у тебя сейчас настроено) — можно передавать/не передавать в payload
    url = _url_join(KIE_BASE_URL, KIE_GEN_PATH)
    headers = {"Authorization": f"Bearer {KIE_API_KEY}", "Content-Type":"application/json"}

    try:
        log.info(f"KIE POST -> {url} | payload: {{'aspect_ratio':'{payload.get('aspect_ratio')}', "
                 f"'image_url':{'yes' if payload.get('image_url') else 'no'}, 'prompt_len':{len(payload.get('prompt',''))}}}")
        r = requests.post(url, headers=headers, data=json.dumps(payload), timeout=60)
        txt = r.text

        if r.status_code == 200:
            try:
                data = r.json()
            except Exception:
                data = {}
            # пробуем разные ключи id
            tid = data.get("taskid") or data.get("task_id") or data.get("id") or data.get("data", {}).get("taskid")
            if tid:
                return {"ok": True, "id": str(tid), "error": None}
            # если 200, но без id — всё равно вернём тело
            return {"ok": False, "id": None, "error": f"No task id in response: {txt[:300]}"}

        if r.status_code == 402:
            return {"ok": False, "id": None, "error": "Недостаточно кредитов на Kie аккаунте."}
        if "Illegal IP" in txt or r.status_code in (401,403):
            return {"ok": False, "id": None, "error": "Доступ API запрещён: IP платформы не в whitelist Kie."}
        return {"ok": False, "id": None, "error": f"API {r.status_code}: {txt[:300]}"}

    except Exception as e:
        return {"ok": False, "id": None, "error": f"Network error: {e}"}

def _get_kie_status(task_id: str) -> dict:
    """
    Узнать статус рендера. Возвращает:
      {"ok": bool, "status": "...", "result_urls": [..] or None, "error": str|None}
    Пробуем несколько вариантов эндпоинтов/полей — у Kie встречаются отличия.
    """
    headers = {"Authorization": f"Bearer {KIE_API_KEY}", "Content-Type": "application/json"}
    candidates = [
        (KIE_DETAIL_PATH, {"taskid": task_id}),                         # POST JSON
        (KIE_DETAIL_PATH, {"taskId": task_id}),
        (f"{KIE_DETAIL_PATH}?taskid={task_id}", None),                  # GET
        (f"{KIE_DETAIL_PATH}?taskId={task_id}", None),
        ("/api/v1/veo/detail", {"taskid": task_id}),                    # запасные
        ("/api/veo/detail", {"taskid": task_id}),
    ]

    for path, payload in candidates:
        try:
            url = _url_join(KIE_BASE_URL, path)
            if payload is None:
                r = requests.get(url, headers=headers, timeout=30)
            else:
                r = requests.post(url, headers=headers, data=json.dumps(payload), timeout=30)
            if r.status_code != 200:
                continue

            ctype = r.headers.get("Content-Type","")
            data = r.json() if "application/json" in ctype else {}

            status = (data.get("status") or data.get("state") or data.get("taskStatus") or "").lower()
            # возможные места, куда кладут ссылки
            result_urls = (
                data.get("result_urls")
                or data.get("result")
                or data.get("data", {}).get("result_urls")
                or data.get("data", {}).get("result")
            )

            if isinstance(result_urls, str):
                result_urls = [result_urls]

            # явная ошибка кодом
            code = str(data.get("code", 0))
            if code not in ("0", "200") and data.get("code") is not None:
                return {"ok": False, "status": status or "failed", "result_urls": None,
                        "error": f"API code {data.get('code')}: {data.get('msg') or data.get('message')}"}

            return {"ok": True, "status": status or "unknown",
                    "result_urls": result_urls, "error": None}
        except Exception:
            continue

    return {"ok": False, "status": "unknown", "result_urls": None, "error": "No status endpoint matched"}

async def _poll_and_send(ctx: ContextTypes.DEFAULT_TYPE, chat_id: int, task_id: str, aspect: str):
    """Опрашиваем Kie раз в ~15 сек до ~12 мин. Как только есть URL — шлём видео."""
    tries, delay = 50, 15
    info_msg = await ctx.bot.send_message(chat_id, "🎞️ Рендерим видео… я сообщу, когда будет готово.")

    for _ in range(tries):
        await asyncio.sleep(delay)
        st = _get_kie_status(task_id)

        if st["ok"] and (st["status"] in ("failed", "error")):
            await info_msg.edit_text("❌ Генерация не удалась на стороне Kie.")
            return

        urls = st.get("result_urls") or []
        if urls:
            url = urls[0]
            try:
                await info_msg.edit_text("✅ Готово! Отправляю видео…")
                await ctx.bot.send_video(chat_id, video=url, caption=f"Готово ({aspect}).")
            except Exception:
                try:
                    await ctx.bot.send_document(chat_id, document=url, caption=f"Готово ({aspect}).")
                except:
                    await ctx.bot.send_message(chat_id, f"✅ Видео готово:\n{url}")
            return

    await info_msg.edit_text("⌛ Видеогенерация заняла слишком много времени. Попробуйте позже.")

# ======================= Typing indicator (как было) =======================
async def _typing_loop(chat_id: int, ctx: ContextTypes.DEFAULT_TYPE, stop_event: asyncio.Event):
    try:
        while not stop_event.is_set():
            await ctx.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
            await asyncio.sleep(4)
    except Exception:
        pass

# ======================= Handlers =======================
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    st = state(ctx); st["mode"] = None
    await update.effective_chat.send_message(
        "👋 Привет! Это бот Google Veo3. Выбери режим ниже и формат кадра.",
        reply_markup=MAIN_MENU
    )
    await update.effective_chat.send_message("Выбери формат:", reply_markup=FORMAT_KB)

async def cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    st = state(ctx); data = q.data
    chat_id = q.message.chat.id

    if data == "back_menu":
        await q.edit_message_text("Главное меню:", reply_markup=MAIN_MENU); return

    if data.startswith("fmt_"):
        st["aspect"] = "16:9" if data=="fmt_16x9" else "9:16"
        await q.edit_message_text(f"✅ Выбран формат: {st['aspect']}.", reply_markup=RUN_KB); return

    if data == "gen_text":
        st["mode"] = "gen_text"; st["last_image_url"] = None
        await q.edit_message_text(
            "✍️ Пришли идею **или готовый промпт**. "
            "Готовый промпт мы не эхо-дублируем — сразу подготовим к запуску.",
            reply_markup=FORMAT_KB
        ); return

    if data == "gen_photo":
        st["mode"] = "gen_photo"
        await q.edit_message_text(
            "📸 Пришли **фото** с подписью (краткое описание). "
            "Если подписи нет — отправь фото, а потом текст отдельным сообщением.",
            reply_markup=FORMAT_KB
        ); return

    if data == "prompt_master":
        st["mode"] = "prompt_master"; st["last_image_url"] = None
        await q.edit_message_text(
            "🧠 Промпт-мастер включён. Опиши идею 1–2 фразами — **начну писать промпт сразу**.",
            reply_markup=FORMAT_KB
        ); return

    if data == "chat":
        st["mode"] = "chat"
        await q.edit_message_text("💬 Обычный чат. Пиши сообщения. /exit — выход.", reply_markup=RUN_KB); return

    if data == "faq":
        await q.edit_message_text(
            "📖 FAQ\n• Примеры: https://t.me/bestveo3promts\n• Форматы: 16:9 и 9:16\n"
            "• Рендер обычно 2–5 мин.\n• Без текста/логотипов в кадре.",
            reply_markup=RUN_KB
        ); return

    if data == "run":
        if st["last_prompt"] is None:
            await q.answer("Нет подготовленного промпта.", show_alert=True); return

        await q.edit_message_text("🚀 Отправляю задачу в Veo3…")
        if st["mode"] == "gen_photo" and st.get("last_image_url"):
            payload = {"prompt": st["last_prompt"], "image_url": st["last_image_url"],
                       "aspect_ratio": "16:9" if st["aspect"] == "16:9" else "9:16"}
        else:
            payload = {"prompt": st["last_prompt"],
                       "aspect_ratio": "16:9" if st["aspect"] == "16:9" else "9:16"}

        res = _submit_kie(payload)
        if res["ok"]:
            task_id = res["id"]
            await q.edit_message_text(
                f"✅ Задача создана! ID: `{task_id}`\nОбычно рендер 2–5 минут.",
                parse_mode=ParseMode.MARKDOWN, reply_markup=RUN_KB
            )
            # ВАЖНО: запускаем фон-поллинг результата — вернём видео как будет готово.
            asyncio.create_task(_poll_and_send(ctx, chat_id, task_id, st["aspect"]))
        else:
            msg = res["error"] or "Неизвестная ошибка."
            if "whitelist" in msg or "IP" in msg:
                msg += "\n\n⚙️ Админу: добавьте исходящие IP Render в whitelist Kie."
            await q.edit_message_text(f"❌ Не удалось создать задачу:\n{msg}", reply_markup=RUN_KB)
        return

async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    st = state(ctx); text = (update.message.text or "").strip()
    chat_id = update.effective_chat.id

    # CHAT режим (как было)
    if st["mode"] == "chat":
        try:
            st["chat_history"] = st.get("chat_history", [])[-8:]
            st["chat_history"].append({"role":"user","content": text})
            answer = oai_chat([{"role":"system","content":"Ты дружелюбный ассистент. Коротко и по делу."}] +
                              st["chat_history"], temperature=0.6, max_tokens=500)
            st["chat_history"].append({"role":"assistant","content": answer})
            await update.message.reply_text(answer)
        except Exception as e:
            await update.message.reply_text(f"Ошибка чата: {e}")
        return

    # PROMPT-MASTER (как было)
    if st["mode"] == "prompt_master":
        notice = await update.message.reply_text("⌛ Начинаю писать промпт…")
        st["_typing_stop"] = asyncio.Event()
        asyncio.create_task(_typing_loop(chat_id, ctx, st["_typing_stop"]))
        try:
            prompt = oai_chat([SYSTEM_PM, {"role":"user","content": text}], temperature=0.7, max_tokens=900)
            st["last_prompt"] = prompt
            st["_typing_stop"].set()
            await notice.edit_text("✅ Готово! Промпт создан и сохранён. Нажми «🚀 Запустить генерацию».",
                                   reply_markup=RUN_KB)
        except Exception as e:
            st["_typing_stop"].set()
            await notice.edit_text(f"❌ Ошибка при создании промпта: {e}")
        return

    # GENERATE BY TEXT / PHOTO (как было)
    if st["mode"] in (None, "gen_text", "gen_photo"):
        if st["mode"] == "gen_photo" and not st.get("last_image_url"):
            await update.message.reply_text("Нужно фото. Пришли изображение (с подписью — по желанию).")
            return

        if looks_like_ready_prompt(text):
            st["last_prompt"] = text
            await update.message.reply_text("✅ Принял промпт. Готов к запуску.", reply_markup=RUN_KB)
            return

        notice = await update.message.reply_text("⌛ Формулирую кинематографический промпт…")
        st["_typing_stop"] = asyncio.Event()
        asyncio.create_task(_typing_loop(chat_id, ctx, st["_typing_stop"]))
        try:
            prompt = oai_chat([SYSTEM_PM, {"role":"user","content": text}], temperature=0.7, max_tokens=900)
            st["last_prompt"] = prompt
            st["_typing_stop"].set()
            await notice.edit_text("✅ Промпт готов и сохранён. Нажми «🚀 Запустить генерацию».",
                                   reply_markup=RUN_KB)
        except Exception as e:
            st["_typing_stop"].set()
            await notice.edit_text(f"❌ Ошибка при подготовке промпта: {e}")
        return

async def on_photo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    st = state(ctx); chat_id = update.effective_chat.id
    try:
        photo = update.message.photo[-1]
        f = await ctx.bot.get_file(photo.file_id)
        image_url = f"https://api.telegram.org/file/bot{TELEGRAM_TOKEN}/{f.file_path}"
        st["last_image_url"] = image_url

        caption = (update.message.caption or "").strip()
        if caption:
            notice = await update.message.reply_text("📸 Принял фото. ⌛ Формулирую промпт…")
            st["_typing_stop"] = asyncio.Event()
            asyncio.create_task(_typing_loop(chat_id, ctx, st["_typing_stop"]))
            try:
                prompt = oai_chat([SYSTEM_PM, {"role":"user","content": caption}], temperature=0.7, max_tokens=900)
                st["last_prompt"] = prompt
                st["_typing_stop"].set()
                await notice.edit_text("✅ Фото и промпт готовы. Нажми «🚀 Запустить генерацию».",
                                       reply_markup=RUN_KB)
            except Exception as e:
                st["_typing_stop"].set()
                await notice.edit_text(f"❌ Ошибка при подготовке промпта: {e}")
        else:
            await update.message.reply_text(
                "📸 Фото получено. Напиши короткое **описание сцены** — я доработаю промпт.",
                reply_markup=RUN_KB
            )
            st["mode"] = "gen_photo"
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

# ======================= MAIN =======================
def main():
    if not TELEGRAM_TOKEN:
        raise RuntimeError("TELEGRAM_TOKEN не задан.")
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).concurrent_updates(True).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("exit", exit_cmd))

    app.add_handler(CallbackQueryHandler(
        cb,
        pattern=r"^(gen_text|gen_photo|prompt_master|chat|faq|run|back_menu|fmt_16x9|fmt_9x16)$"
    ))

    app.add_handler(MessageHandler(filters.PHOTO, on_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    app.add_error_handler(error_handler)
    log.info("Bot started.")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()

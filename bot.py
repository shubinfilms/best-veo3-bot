# -*- coding: utf-8 -*-
# BEST VEO3 BOT — text + photo generation + Prompt-Master
# PTB v21, requests, OpenAI==0.28.x (старый ChatCompletion)
# Модель KIE: ВСЕГДА veo3_fast

import os, json, logging, traceback, requests, asyncio
from typing import Optional, Dict, Any

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
OPENAI_API_KEY  = os.getenv("OPENAI_API_KEY", "") or os.getenv("OPENAI_KEY", "")
KIE_API_KEY     = os.getenv("KIE_API_KEY", "")
# Оставляем разделение БАЗА + ПУТЬ, как тебе удобно в Render
KIE_BASE_URL    = (os.getenv("KIE_BASE_URL") or "https://api.kie.ai").strip().rstrip("/")
KIE_GEN_PATH    = (os.getenv("KIE_GEN_PATH") or os.getenv("KIE_GENERATE_PATH") or "/api/v1/veo/generate").strip()
if not KIE_GEN_PATH.startswith("/"):
    KIE_GEN_PATH = "/" + KIE_GEN_PATH

LOG_LEVEL       = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(level=getattr(logging, LOG_LEVEL, logging.INFO),
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("best-veo3")
log.info(f"KIE endpoint: {KIE_BASE_URL}{KIE_GEN_PATH}")

# ----------------- UI -----------------
MAIN_MENU = InlineKeyboardMarkup([
    [InlineKeyboardButton("🎬 Сгенерировать видео по тексту", callback_data="gen_text")],
    [InlineKeyboardButton("🖼️ Сгенерировать видео по фото", callback_data="gen_photo")],
    [InlineKeyboardButton("🧠 Промпт-мастер (ChatGPT)", callback_data="prompt_master")],
    [InlineKeyboardButton("💬 Обычный чат (ChatGPT)", callback_data="chat")],
    [InlineKeyboardButton("❓ FAQ", callback_data="faq"),
     InlineKeyboardButton("📈 Канал с промптами", url="https://t.me/bestveo3promts")],
])

FORMAT_KB = InlineKeyboardMarkup([
    [InlineKeyboardButton("🎬 16:9", callback_data="fmt_16x9"),
     InlineKeyboardButton("📱 9:16", callback_data="fmt_9x16")],
])

def kb_run(aspect: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(("✅ " if aspect=="16:9" else "")+"🎬 16:9", callback_data="fmt_16x9"),
         InlineKeyboardButton(("✅ " if aspect=="9:16" else "")+"📱 9:16", callback_data="fmt_9x16")],
        [InlineKeyboardButton("🚀 Запустить генерацию", callback_data="run")],
        [InlineKeyboardButton("⬅️ Назад в меню", callback_data="back_menu")],
    ])

def state(ctx: ContextTypes.DEFAULT_TYPE) -> Dict[str, Any]:
    if "state" not in ctx.user_data:
        ctx.user_data["state"] = {
            "mode": None,            # gen_text | gen_photo | prompt_master | chat
            "aspect": "16:9",
            "last_prompt": None,     # текст промпта для Veo3
            "last_image_url": None,  # TG file URL для фото-режима
            "chat_history": [],
            "_typing_stop": None
        }
    return ctx.user_data["state"]

# ----------------- Heuristics -----------------
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

def html_escape(s: str) -> str:
    return s.replace("&","&amp;").replace("<","&lt;").replace(">","&gt;")

# ----------------- OpenAI (СТАРЫЙ SDK, как работало) -----------------
def oai_chat(messages, temperature=0.7, max_tokens=900) -> str:
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
        "You are a cinematic prompt-writer for Google Veo 3. "
        "Write ONE polished, production-ready prompt in ENGLISH, 500–900 characters. "
        "Keep the user's idea, enhance with: composition, lens (mm/anamorphic), camera moves "
        "(push-in/dolly/glide/rack focus), lighting/palette, pacing, micro-details (dust/steam/flares), "
        "and sound. No on-screen text/logos/subtitles."
    )
}

# ----------------- Kie / Veo3 (ВСЕГДА veo3_fast) -----------------
def _kie_url() -> str:
    url = f"{KIE_BASE_URL}{KIE_GEN_PATH}"
    url = url.replace("://", "§§").replace("//", "/").replace("§§", "://")
    return url

def _submit_kie(payload: dict) -> dict:
    """Единая отправка задачи в KIE. Модель фиксируем: veo3_fast."""
    if not (KIE_API_KEY and KIE_BASE_URL):
        return {"ok": False, "id": None, "error": "KIE_API_KEY или KIE_BASE_URL не заданы."}

    payload = dict(payload or {})
    payload["model"] = "veo3_fast"  # <- требуемая модель
    url = _kie_url()
    headers = {"Authorization": f"Bearer {KIE_API_KEY}", "Content-Type":"application/json"}

    try:
        log.info(f"KIE POST -> {url} | payload: {{'model':'{payload.get('model')}','aspect_ratio':'{payload.get('aspect_ratio')}',"
                 f"'image_url':{'yes' if payload.get('image_url') else 'no'}, 'prompt_len':{len(payload.get('prompt',''))}}}")
        r = requests.post(url, headers=headers, data=json.dumps(payload), timeout=60)

        if r.status_code == 200:
            try:
                data = r.json()
            except Exception:
                data = {}
            # разные варианты поля id
            task_id = (data.get("taskId") or data.get("task_id") or data.get("id")
                       or data.get("data", {}).get("taskId"))
            # обработка «кредиты закончились»
            if str(data).lower().find("insufficient") != -1 or data.get("code") in (402, 42901):
                return {"ok": False, "id": None, "error": "Недостаточно кредитов на KIE аккаунте."}
            return {"ok": True, "id": task_id or "unknown", "error": None}

        body = r.text[:400]
        if r.status_code == 402:
            return {"ok": False, "id": None, "error": "Недостаточно кредитов на KIE аккаунте."}
        if "Illegal IP" in body or r.status_code in (401,403):
            return {"ok": False, "id": None, "error": "Доступ API запрещён: IP Render не в whitelist KIE."}
        return {"ok": False, "id": None, "error": f"API {r.status_code}: {body}"}
    except Exception as e:
        return {"ok": False, "id": None, "error": f"Network error: {e}"}

def submit_veo_job_text(prompt: str, aspect: str) -> dict:
    return _submit_kie({"prompt": prompt,
                        "aspect_ratio": "16:9" if aspect=="16:9" else "9:16"})

def submit_veo_job_photo(image_url: str, prompt: str, aspect: str) -> dict:
    return _submit_kie({"prompt": prompt, "image_url": image_url,
                        "aspect_ratio":"16:9" if aspect=="16:9" else "9:16"})

# ----------------- Typing Indicator -----------------
async def _typing_loop(chat_id: int, ctx: ContextTypes.DEFAULT_TYPE, stop_event: asyncio.Event):
    try:
        while not stop_event.is_set():
            await ctx.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
            await asyncio.sleep(4)
    except Exception:
        pass

# ----------------- Handlers -----------------
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    st = state(ctx); st["mode"] = None
    await update.effective_chat.send_message(
        "👋 Привет! Это бот Google Veo 3. Выберите режим и формат кадра.",
        reply_markup=MAIN_MENU
    )
    await update.effective_chat.send_message("Выбери формат:", reply_markup=FORMAT_KB)

async def cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    st = state(ctx); data = q.data

    if data == "back_menu":
        await q.edit_message_text("Главное меню:", reply_markup=MAIN_MENU); return

    if data.startswith("fmt_"):
        st["aspect"] = "16:9" if data=="fmt_16x9" else "9:16"
        await q.edit_message_text(f"✅ Выбран формат: {st['aspect']}.", reply_markup=kb_run(st["aspect"])); return

    if data == "gen_text":
        st["mode"] = "gen_text"; st["last_image_url"] = None
        await q.edit_message_text(
            "✍️ Пришлите **идею** или **готовый промпт**. "
            "Если пришлёте идею — я сформулирую промпт автоматически.",
            reply_markup=FORMAT_KB
        ); return

    if data == "gen_photo":
        st["mode"] = "gen_photo"
        await q.edit_message_text(
            "📸 Пришлите **фото** с подписью (короткое описание). "
            "Если подписи нет — отправьте фото, затем текст отдельным сообщением.",
            reply_markup=FORMAT_KB
        ); return

    if data == "prompt_master":
        st["mode"] = "prompt_master"; st["last_image_url"] = None
        await q.edit_message_text(
            "🧠 Промпт-мастер включён. Опишите идею **1–2 фразами** — я сразу напишу готовый промпт (EN, 500–900 симв.).",
            reply_markup=FORMAT_KB
        ); return

    if data == "chat":
        st["mode"] = "chat"
        await q.edit_message_text("💬 Обычный чат. Пишите сообщения. /exit — выход.", reply_markup=kb_run(st["aspect"])); return

    if data == "faq":
        await q.edit_message_text(
            "📖 FAQ\n• Примеры и идеи: https://t.me/bestveo3promts\n• Форматы: 16:9 и 9:16\n"
            "• Рендер обычно 2–5 мин.\n• Без текста/логотипов в кадре.",
            reply_markup=kb_run(st["aspect"])
        ); return

    if data == "run":
        if not st.get("last_prompt"):
            await q.answer("Нет подготовленного промпта.", show_alert=True); return
        await q.edit_message_text("🚀 Отправляю задачу в Veo3 Fast…")
        res = (submit_veo_job_photo(st["last_image_url"], st["last_prompt"], st["aspect"])
               if st["mode"]=="gen_photo" and st.get("last_image_url")
               else submit_veo_job_text(st["last_prompt"], st["aspect"]))
        if res["ok"]:
            await q.edit_message_text(
                f"✅ Задача создана. ID: `{res['id']}`\nОбычно рендер 2–5 мин.",
                parse_mode=ParseMode.MARKDOWN, reply_markup=kb_run(st["aspect"])
            )
        else:
            msg = res["error"] or "Неизвестная ошибка."
            await q.edit_message_text(f"❌ Ошибка запуска генерации: {msg}", reply_markup=kb_run(st["aspect"]))
        return

async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    st = state(ctx); text = (update.message.text or "").strip()
    chat_id = update.effective_chat.id

    # CHAT
    if st["mode"] == "chat":
        try:
            st["chat_history"] = st.get("chat_history", [])[-8:]
            st["chat_history"].append({"role":"user","content": text})
            answer = oai_chat(
                [{"role":"system","content":"You are a helpful assistant. Reply briefly and clearly."}]
                + st["chat_history"], temperature=0.6, max_tokens=500
            )
            st["chat_history"].append({"role":"assistant","content": answer})
            await update.message.reply_text(answer)
        except Exception as e:
            await update.message.reply_text(f"Ошибка чата: {e}")
        return

    # PROMPT-MASTER (сразу один готовый промпт, EN, 500–900)
    if st["mode"] == "prompt_master":
        notice = await update.message.reply_text("⌛ Пишу промпт…")
        st["_typing_stop"] = asyncio.Event()
        asyncio.create_task(_typing_loop(chat_id, ctx, st["_typing_stop"]))
        try:
            prompt = oai_chat([SYSTEM_PM, {"role":"user","content": text}], temperature=0.7, max_tokens=900)
            st["last_prompt"] = prompt
            st["_typing_stop"].set()
            await notice.edit_text(
                "🧠 Готовый промпт для Veo3 Fast:\n"
                f"<pre>{html_escape(prompt)}</pre>",
                parse_mode=ParseMode.HTML,
                reply_markup=kb_run(st["aspect"])
            )
        except Exception as e:
            st["_typing_stop"].set()
            await notice.edit_text(f"❌ Prompt-Master error:\n{e}")
        return

    # GEN BY TEXT (готовый промпт или идея)
    if st["mode"] in (None, "gen_text", "gen_photo"):
        if st["mode"] == "gen_photo" and not st.get("last_image_url"):
            await update.message.reply_text("Нужно фото. Пришлите изображение (подпись — по желанию).")
            return

        # если это «готовый промпт», просто принимаем
        if looks_like_ready_prompt(text):
            st["last_prompt"] = text
            await update.message.reply_text("✅ Принял промпт. Нажмите «🚀 Запустить генерацию».",
                                            reply_markup=kb_run(st["aspect"]))
            return

        # иначе — превращаем идею в промпт
        notice = await update.message.reply_text("⌛ Формулирую кинематографический промпт…")
        st["_typing_stop"] = asyncio.Event()
        asyncio.create_task(_typing_loop(chat_id, ctx, st["_typing_stop"]))
        try:
            prompt = oai_chat([SYSTEM_PM, {"role":"user","content": text}], temperature=0.7, max_tokens=900)
            st["last_prompt"] = prompt
            st["_typing_stop"].set()
            await notice.edit_text(
                "✅ Промпт готов и сохранён. Нажмите «🚀 Запустить генерацию».",
                reply_markup=kb_run(st["aspect"])
            )
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
            notice = await update.message.reply_text("📸 Принял фото. ⌛ Пишу промпт…")
            st["_typing_stop"] = asyncio.Event()
            asyncio.create_task(_typing_loop(chat_id, ctx, st["_typing_stop"]))
            try:
                prompt = oai_chat([SYSTEM_PM, {"role":"user","content": caption}], temperature=0.7, max_tokens=900)
                st["last_prompt"] = prompt
                st["_typing_stop"].set()
                await notice.edit_text("✅ Фото и промпт готовы. Нажмите «🚀 Запустить генерацию».",
                                       reply_markup=kb_run(st["aspect"]))
            except Exception as e:
                st["_typing_stop"].set()
                await notice.edit_text(f"❌ Ошибка при подготовке промпта: {e}")
        else:
            await update.message.reply_text(
                "📸 Фото получено. Напишите короткое **описание сцены** — я доработаю промпт.",
                reply_markup=kb_run(st["aspect"])
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

# ----------------- MAIN -----------------
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

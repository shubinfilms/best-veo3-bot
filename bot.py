# -*- coding: utf-8 -*-
# BEST VEO3 BOT — text + photo + Prompt-Master, polling/worker версия
# стабильная сборка (PTB 21.6)

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
KIE_BASE_URL    = os.getenv("KIE_BASE_URL", "https://api.kie.ai")
KIE_ENDPOINT    = os.getenv("KIE_GENERATE_PATH", "/v1/veo3/generations")  # можно оставить по умолчанию
BOT_MODEL       = os.getenv("BOT_MODEL", "veo3").strip()  # 'veo3' или 'veo3_fast'
PUBLIC_URL      = os.getenv("PUBLIC_URL", "")  # не обязательно
LOG_LEVEL       = os.getenv("LOG_LEVEL", "INFO").upper()

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)
log = logging.getLogger("best-veo3")

# ----------------- UI -----------------
MAIN_MENU = InlineKeyboardMarkup([
    [InlineKeyboardButton("🎬 Сгенерировать видео по тексту", callback_data="gen_text")],
    [InlineKeyboardButton("🖼️ Сгенерировать видео по фото",  callback_data="gen_photo")],
    [InlineKeyboardButton("🧠 Промпт-мастер (ChatGPT)",       callback_data="prompt_master")],
    [InlineKeyboardButton("💬 Обычный чат (ChatGPT)",         callback_data="chat")],
    [InlineKeyboardButton("❓ FAQ",                           callback_data="faq"),
     InlineKeyboardButton("📚 Канал с промптами",            url="https://t.me/bestveo3promts")],
])

FORMAT_KB = InlineKeyboardMarkup([
    [InlineKeyboardButton("16:9", callback_data="fmt_16x9"),
     InlineKeyboardButton("9:16", callback_data="fmt_9x16")],
])

RUN_KB = InlineKeyboardMarkup([
    [InlineKeyboardButton("🚀 Запустить генерацию", callback_data="run")],
    [InlineKeyboardButton("⬅️ Назад в меню",        callback_data="back_menu")],
])

def state(ctx: ContextTypes.DEFAULT_TYPE) -> Dict[str, Any]:
    if "state" not in ctx.user_data:
        ctx.user_data["state"] = {
            "mode": None,            # gen_text | gen_photo | prompt_master | chat
            "aspect": "16:9",
            "last_prompt": None,
            "last_image_url": None,
            "chat_history": [],
            "_typing_stop": None,
        }
    return ctx.user_data["state"]

# ----------------- Heuristics -----------------
def looks_like_ready_prompt(text: str) -> bool:
    if not text:
        return False
    t = text.strip()
    if t.startswith("{") and "}" in t:
        return True
    score = 0
    for kw in [
        "fps","anamorphic","85mm","35mm","lens","DOF","bokeh","rack focus",
        "color palette","lighting","camera","glide","push-in","tone","sound",
        "subtitles","\"shot\"","\"scene\"","\"audio\"","\"cinematic\""
    ]:
        if kw in t.lower():
            score += 1
    return score >= 3 or len(t) > 400

# ----------------- OpenAI -----------------
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
        "Ты — режиссёр-постановщик/промпт-сценарист для Veo3. "
        "Не меняй идею пользователя, усиливай её: композиция, оптика (мм/анаморф), "
        "движение камеры (push-in, dolly, glide, rack focus), свет/палитра, темп/ритм, "
        "микро-детали (пыль, пар, блики), звук (музыка/шум/микс). "
        "Пиши кинематографично, живым английским, 3–6 абзацев (≈500–900 симв.). "
        "Без воды, брендов/логотипов и субтитров."
    )
}

# ----------------- Kie / Veo3 -----------------
def _submit_kie(payload: dict) -> dict:
    if not (KIE_API_KEY and KIE_BASE_URL):
        return {"ok": False, "id": None, "error": "KIE_API_KEY или KIE_BASE_URL не заданы."}
    url = f"{KIE_BASE_URL.rstrip('/')}{KIE_ENDPOINT}"
    headers = {"Authorization": f"Bearer {KIE_API_KEY}", "Content-Type":"application/json"}
    try:
        r = requests.post(url, headers=headers, data=json.dumps(payload), timeout=30)
        if r.status_code == 200:
            data = r.json()
            task_id = data.get("id") or data.get("task_id") or data.get("data", {}).get("taskId") or "unknown"
            return {"ok": True, "id": task_id, "error": None}
        txt = r.text
        if "Illegal IP" in txt or r.status_code in (401,403):
            return {"ok": False, "id": None, "error": "Доступ API запрещён: IP Render не в whitelist Kie."}
        if r.status_code == 402:
            return {"ok": False, "id": None, "error": "Недостаточно кредитов (402)."}
        return {"ok": False, "id": None, "error": f"API {r.status_code}: {txt[:300]}"}
    except Exception as e:
        return {"ok": False, "id": None, "error": f"Network error: {e}"}

def submit_veo_job_text(prompt: str, aspect: str) -> dict:
    return _submit_kie({
        "model": BOT_MODEL if BOT_MODEL in ("veo3", "veo3_fast") else "veo3",
        "prompt": prompt,
        "aspect_ratio": "16:9" if aspect == "16:9" else "9:16"
    })

def submit_veo_job_photo(image_url: str, prompt: str, aspect: str) -> dict:
    return _submit_kie({
        "model": BOT_MODEL if BOT_MODEL in ("veo3", "veo3_fast") else "veo3",
        "prompt": prompt,
        "image_url": image_url,
        "aspect_ratio": "16:9" if aspect == "16:9" else "9:16"
    })

# ----------------- Helpers -----------------
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
        "👋 Привет! Это бот Google Veo3. Выбери режим ниже и формат кадра.",
        reply_markup=MAIN_MENU
    )
    await update.effective_chat.send_message("Выбери формат:", reply_markup=FORMAT_KB)

async def cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    st = state(ctx); data = q.data

    if data == "back_menu":
        await q.edit_message_text("Главное меню:", reply_markup=MAIN_MENU); return

    if data.startswith("fmt_"):
        st["aspect"] = "16:9" if data == "fmt_16x9" else "9:16"
        await q.edit_message_text(f"✅ Выбран формат: {st['aspect']}.", reply_markup=RUN_KB); return

    if data == "gen_text":
        st["mode"] = "gen_text"; st["last_image_url"] = None
        await q.edit_message_text(
            "✍️ Пришли идею **или готовый промпт**. "
            "Готовый промпт мы не переписываем — сразу подготовим к запуску.",
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
        if not st.get("last_prompt"):
            await q.answer("Нет подготовленного промпта.", show_alert=True); return
        await q.edit_message_text("🚀 Отправляю задачу в Veo3…")

        res = (submit_veo_job_photo(st["last_image_url"], st["last_prompt"], st["aspect"])
               if st["mode"] == "gen_photo" and st.get("last_image_url")
               else submit_veo_job_text(st["last_prompt"], st["aspect"]))

        if res["ok"]:
            await q.edit_message_text(
                f"✅ Задача создана. ID: `{res['id']}`\nОбычно генерация 2–5 мин.",
                parse_mode=ParseMode.MARKDOWN, reply_markup=RUN_KB
            )
        else:
            msg = res["error"] or "Неизвестная ошибка."
            if "whitelist" in msg or "IP" in msg:
                msg += "\n\n⚙️ Админу: добавьте исходящие IP Render в whitelist Kie."
            await q.edit_message_text(f"❌ Не удалось создать задачу:\n{msg}", reply_markup=RUN_KB)
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
                [{"role":"system","content":"Ты дружелюбный ассистент. Коротко и по делу."}] + st["chat_history"],
                temperature=0.6, max_tokens=500
            )
            st["chat_history"].append({"role":"assistant","content": answer})
            await update.message.reply_text(answer)
        except Exception as e:
            await update.message.reply_text(f"Ошибка чата: {e}")
        return

    # PROMPT-MASTER
    if st["mode"] == "prompt_master":
        notice = await update.message.reply_text("⌛ Начинаю писать промпт…")
        st["_typing_stop"] = asyncio.Event()
        asyncio.create_task(_typing_loop(chat_id, ctx, st["_typing_stop"]))
        try:
            prompt = oai_chat([SYSTEM_PM, {"role":"user","content": text}], temperature=0.7, max_tokens=900)
            st["last_prompt"] = prompt
            st["_typing_stop"].set()
            await notice.edit_text("✅ Готово! Промпт сохранён. Нажми «🚀 Запустить генерацию».",
                                   reply_markup=RUN_KB)
        except Exception as e:
            st["_typing_stop"].set()
            await notice.edit_text(f"❌ Ошибка при создании промпта: {e}")
        return

    # TEXT/PHOTO режимы
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

# ----------------- MAIN -----------------
def main():
    if not TELEGRAM_TOKEN:
        raise RuntimeError("TELEGRAM_TOKEN не задан.")
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).concurrent_updates(True).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("exit",  exit_cmd))

    app.add_handler(CallbackQueryHandler(
        cb,
        pattern=r"^(gen_text|gen_photo|prompt_master|chat|faq|run|back_menu|fmt_16x9|fmt_9x16)$"
    ))

    app.add_handler(MessageHandler(filters.PHOTO, on_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    app.add_error_handler(error_handler)
    log.info("Bot started (polling).")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()

# -*- coding: utf-8 -*-
# BEST VEO3 BOT — text + photo generation, Prompt-Master tuned

import os, json, logging, traceback, requests
from typing import Optional, Dict, Any, List

from dotenv import load_dotenv
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardRemove
from telegram.constants import ParseMode
from telegram.ext import (
    ApplicationBuilder, Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters
)

# ---------- ENV & LOG ----------
load_dotenv()

# поддерживаем оба варианта имен
TELEGRAM_TOKEN  = os.getenv("TELEGRAM_TOKEN") or os.getenv("BOT_TOKEN") or ""
OPENAI_API_KEY  = os.getenv("OPENAI_API") or os.getenv("OPENAI_API_KEY") or os.getenv("OPENAI_KEY") or ""
KIE_API_KEY     = os.getenv("KIE_API_KEY", "")
KIE_BASE_URL    = (os.getenv("KIE_BASE_URL", "https://api.kie.ai")).rstrip("/")
KIE_ENDPOINT    = os.getenv("KIE_ENDPOINT", "/v1/veo3/generations").strip()  # можно поменять без правки кода
LOG_LEVEL       = os.getenv("LOG_LEVEL", "INFO").upper()

logging.basicConfig(level=getattr(logging, LOG_LEVEL, logging.INFO),
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("best-veo3")

# ---------- KEYBOARDS ----------
MAIN_MENU = InlineKeyboardMarkup([
    [InlineKeyboardButton("🎬 Сгенерировать видео по тексту", callback_data="mode_gen_text")],
    [InlineKeyboardButton("🖼️ Сгенерировать видео по фото",  callback_data="mode_gen_photo")],
    [InlineKeyboardButton("🧠 Промпт-мастер (ChatGPT)",       callback_data="mode_prompt_master")],
    [InlineKeyboardButton("💬 Обычный чат (ChatGPT)",         callback_data="mode_chat")],
    [InlineKeyboardButton("❓ FAQ", callback_data="faq"),
     InlineKeyboardButton("📚 Канал с промптами", url="https://t.me/bestveo3promts")],
])

def format_kb(aspect: str) -> InlineKeyboardMarkup:
    """Компактная клавиатура выбора формата с пиктограммами."""
    b16  = f"{'✅ ' if aspect=='16:9' else ''}🎬 16:9"
    b916 = f"{'✅ ' if aspect=='9:16' else ''}📱 9:16"
    return InlineKeyboardMarkup([[InlineKeyboardButton(b16,  callback_data="fmt_16x9"),
                                  InlineKeyboardButton(b916, callback_data="fmt_9x16")]])

RUN_KB = InlineKeyboardMarkup([
    [InlineKeyboardButton("🚀 Запустить генерацию", callback_data="run")],
    [InlineKeyboardButton("⬅️ Назад в меню", callback_data="back_menu")],
])

AFTER_PM_ACTIONS = InlineKeyboardMarkup([
    [InlineKeyboardButton("🎬 Сгенерировать по тексту", callback_data="mode_gen_text_from_pm")],
    [InlineKeyboardButton("🖼️ Сгенерировать по фото",  callback_data="mode_gen_photo_from_pm")],
])

# ---------- STATE ----------
def state(ctx: ContextTypes.DEFAULT_TYPE) -> Dict[str, Any]:
    if "state" not in ctx.user_data:
        ctx.user_data["state"] = {
            "mode": None,            # gen_text | gen_photo | prompt_master | chat
            "aspect": "16:9",
            "last_prompt": None,
            "last_image_url": None,
            "chat_history": []
        }
    return ctx.user_data["state"]

# ---------- HELPERS ----------
def looks_like_ready_prompt(text: str) -> bool:
    if not text: 
        return False
    if text.strip().startswith("{") and "}" in text:
        return True
    score = 0
    for kw in ("fps","anamorphic","85mm","35mm","lens","DOF","bokeh","rack focus",
               "color palette","lighting","camera","glide","push-in","tone","sound",
               "\"shot\"","\"scene\"","\"audio\"","cinematic"):
        if kw in text.lower():
            score += 1
    return score >= 3 or len(text) > 400

def oai_chat(messages: List[Dict[str, str]], temperature=0.7, max_tokens=900) -> str:
    if not OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API / OPENAI_API_KEY не задан.")
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
        "Идею пользователя не меняй, а усиливай: композиция, оптика (мм/анаморф), "
        "движение камеры (push-in, dolly, glide, rack focus), свет и палитра, темп/ритм, "
        "микро-детали (пыль, пар, блики), звук (музыка/шум/микс). "
        "Стиль: кинематографический, живой английский, 3–6 абзацев, 500–900 символов. "
        "Без воды, брендов, логотипов и субтитров."
    )
}

# ---------- KIE ----------
def _submit_kie(payload: dict) -> dict:
    if not (KIE_API_KEY and KIE_BASE_URL and KIE_ENDPOINT):
        return {"ok": False, "id": None, "error": "KIE_API_KEY, KIE_BASE_URL или KIE_ENDPOINT не заданы."}
    url = f"{KIE_BASE_URL}{KIE_ENDPOINT}"
    headers = {"Authorization": f"Bearer {KIE_API_KEY}", "Content-Type":"application/json"}
    try:
        r = requests.post(url, headers=headers, data=json.dumps(payload), timeout=30)
        if r.status_code == 200:
            data = r.json()
            return {"ok": True, "id": data.get("id") or data.get("task_id") or "unknown", "error": None}
        txt = r.text
        if "Illegal IP" in txt or r.status_code in (401, 403):
            return {"ok": False, "id": None, "error": "Доступ API запрещён: IP Render не в whitelist Kie."}
        return {"ok": False, "id": None, "error": f"API {r.status_code}: {txt[:300]} (url={url})"}
    except Exception as e:
        return {"ok": False, "id": None, "error": f"Network error: {e}"}

def submit_veo_job_text(prompt: str, aspect: str) -> dict:
    return _submit_kie({
        "model": "veo3",
        "prompt": prompt,
        "aspect_ratio": "16:9" if aspect == "16:9" else "9:16"
    })

def submit_veo_job_photo(image_url: str, prompt: str, aspect: str) -> dict:
    return _submit_kie({
        "model": "veo3",
        "prompt": prompt,
        "image_url": image_url,   # публичный URL Telegram файла
        "aspect_ratio": "16:9" if aspect == "16:9" else "9:16"
    })

# ---------- HANDLERS ----------
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    st = state(ctx); st["mode"] = None
    await update.effective_chat.send_message(
        "👋 Привет! Это бот Google Veo3. Выбери режим ниже.",
        reply_markup=MAIN_MENU
    )

async def cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    st = state(ctx); data = q.data

    # выбор формата — просто подсветка клавиатуры
    if data == "fmt_16x9":
        st["aspect"] = "16:9"
        try: await q.edit_message_reply_markup(reply_markup=format_kb(st["aspect"]))
        except: pass
        return
    if data == "fmt_9x16":
        st["aspect"] = "9:16"
        try: await q.edit_message_reply_markup(reply_markup=format_kb(st["aspect"]))
        except: pass
        return

    if data == "back_menu":
        st["mode"] = None
        await q.edit_message_text("Главное меню:", reply_markup=MAIN_MENU); 
        return

    # режимы
    if data == "mode_gen_text":
        st.update({"mode":"gen_text","last_image_url":None,"last_prompt":None})
        await q.edit_message_text("✍️ Пришли идею **или готовый промпт**.\n\nВыбери формат кадра:",
                                  reply_markup=format_kb(st["aspect"]))
        return

    if data == "mode_gen_photo":
        st.update({"mode":"gen_photo","last_prompt":None})
        await q.edit_message_text("📸 Пришли **фото** с подписью (краткое описание).\n\nВыбери формат кадра:",
                                  reply_markup=format_kb(st["aspect"]))
        return

    if data == "mode_prompt_master":
        st.update({"mode":"prompt_master","last_image_url":None,"last_prompt":None})
        await q.edit_message_text(
            "🧠 Промпт-мастер включён. Опиши идею 1–2 фразами — **начну писать промпт**…\n\n"
            "Выбери формат кадра:", reply_markup=format_kb(st["aspect"])
        )
        return

    if data == "mode_chat":
        st["mode"] = "chat"
        await q.edit_message_text("💬 Обычный чат. Пиши сообщения. /exit — выход.", reply_markup=RUN_KB)
        return

    # быстрый переход после PM
    if data == "mode_gen_text_from_pm":
        st["mode"] = "gen_text"
        await q.edit_message_text("Режим «по тексту». Нажми «🚀 Запустить генерацию» или измени формат ниже.",
                                  reply_markup=format_kb(st["aspect"]))
        return
    if data == "mode_gen_photo_from_pm":
        st["mode"] = "gen_photo"
        await q.edit_message_text("Режим «по фото». Отправь изображение и подпись (если нужно).",
                                  reply_markup=format_kb(st["aspect"]))
        return

    # запуск
    if data == "run":
        if not st.get("last_prompt"):
            await q.answer("Нет подготовленного промпта.", show_alert=True); 
            return
        await q.edit_message_text("🚀 Отправляю задачу в Veo3…")
        res = (submit_veo_job_photo(st["last_image_url"], st["last_prompt"], st["aspect"])
               if st["mode"] == "gen_photo" and st.get("last_image_url")
               else submit_veo_job_text(st["last_prompt"], st["aspect"]))
        if res["ok"]:
            await q.edit_message_text(
                f"✅ Задача отправлена! ID: `{res['id']}`\nОбычно рендер 2–5 мин.",
                parse_mode=ParseMode.MARKDOWN, reply_markup=RUN_KB
            )
        else:
            msg = res["error"] or "Неизвестная ошибка."
            if "whitelist" in msg or "IP" in msg:
                msg += "\n\n⚙️ Админу: добавьте исходящие IP Render в whitelist Kie."
            await q.edit_message_text(f"❌ Не удалось создать задачу:\n{msg}", reply_markup=RUN_KB)
        return

    if data == "faq":
        await q.edit_message_text(
            "📖 FAQ\n• Примеры: https://t.me/bestveo3promts\n• Форматы: 16:9 и 9:16\n"
            "• Рендер обычно 2–5 мин.\n• В кадре без текста/логотипов.",
            reply_markup=RUN_KB
        )
        return

async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    st = state(ctx); text = (update.message.text or "").strip()

    # чат
    if st["mode"] == "chat":
        try:
            st["chat_history"] = st.get("chat_history", [])[-8:]
            st["chat_history"].append({"role":"user","content": text})
            answer = oai_chat(
                [{"role":"system","content":"Ты дружелюбный ассистент. Коротко и по делу."}]
                + st["chat_history"], temperature=0.6, max_tokens=500
            )
            st["chat_history"].append({"role":"assistant","content": answer})
            await update.message.reply_text(answer)
        except Exception as e:
            await update.message.reply_text(f"Ошибка чата: {e}")
        return

    # Prompt-Master: показываем процесс И отправляем сам промпт
    if st["mode"] == "prompt_master":
        working = await update.message.reply_text("⌛ Начинаю писать промпт…")
        try:
            prompt = oai_chat([SYSTEM_PM, {"role":"user","content": text}], temperature=0.7, max_tokens=900)
            st["last_prompt"] = prompt
            await working.edit_text("🧠 Готовый промпт для Veo3:\n\n" + prompt)
            await update.message.reply_text("Выбери дальнейшее действие:", reply_markup=AFTER_PM_ACTIONS)
        except Exception as e:
            await working.edit_text(f"❌ Ошибка при создании промпта: {e}")
        return

    # генерация по тексту / дефолт
    if st["mode"] in (None, "gen_text", "gen_photo"):
        if st["mode"] == "gen_photo" and not st.get("last_image_url"):
            await update.message.reply_text("Нужна фотография. Пришли изображение (с подписью — по желанию).")
            return

        if looks_like_ready_prompt(text):
            st["last_prompt"] = text
            await update.message.reply_text("✅ Принял промпт. Готов к запуску.", reply_markup=RUN_KB)
            return

        working = await update.message.reply_text("⌛ Формулирую кинематографический промпт…")
        try:
            prompt = oai_chat([SYSTEM_PM, {"role":"user","content": text}], temperature=0.7, max_tokens=900)
            st["last_prompt"] = prompt
            # здесь НЕ присылаем обратно весь промпт — только подтверждение
            await working.edit_text("✅ Промпт готов и сохранён. Нажми «🚀 Запустить генерацию».",
                                    reply_markup=RUN_KB)
        except Exception as e:
            await working.edit_text(f"❌ Ошибка при подготовке промпта: {e}")
        return

async def on_photo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    st = state(ctx)
    try:
        photo = update.message.photo[-1]
        f = await update.get_bot().get_file(photo.file_id)
        image_url = f"https://api.telegram.org/file/bot{TELEGRAM_TOKEN}/{f.file_path}"
        st["last_image_url"] = image_url
        caption = (update.message.caption or "").strip()
        if caption:
            working = await update.message.reply_text("📸 Фото получено. ⌛ Формулирую промпт…")
            try:
                prompt = oai_chat([SYSTEM_PM, {"role":"user","content": caption}], temperature=0.7, max_tokens=900)
                st["last_prompt"] = prompt
                await working.edit_text("✅ Фото и промпт готовы. Нажми «🚀 Запустить генерацию».",
                                        reply_markup=RUN_KB)
            except Exception as e:
                await working.edit_text(f"❌ Ошибка при подготовке промпта: {e}")
        else:
            st["mode"] = "gen_photo"
            await update.message.reply_text(
                "📸 Фото получено. Напиши короткое **описание сцены** — я доработаю промпт.",
                reply_markup=format_kb(st["aspect"])
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

# ---------- MAIN ----------
def main():
    if not TELEGRAM_TOKEN:
        raise RuntimeError("TELEGRAM_TOKEN (или BOT_TOKEN) не задан.")
    app: Application = ApplicationBuilder().token(TELEGRAM_TOKEN).concurrent_updates(True).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("exit",  exit_cmd))
    app.add_handler(CallbackQueryHandler(cb, pattern=r"^(mode_.+|fmt_16x9|fmt_9x16|run|back_menu|faq)$"))
    app.add_handler(MessageHandler(filters.PHOTO, on_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_error_handler(error_handler)

    log.info("Bot started.")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()

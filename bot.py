# -*- coding: utf-8 -*-
# BEST VEO3 BOT — Background Worker (polling)
# Работает только через polling, без webhook
# 2025-09-06

import os, logging, traceback, requests, json, asyncio
from typing import Optional, Dict, Any

from dotenv import load_dotenv
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardRemove
from telegram.constants import ParseMode, ChatAction
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters
)

# ----------------- ENV -----------------
load_dotenv()
TELEGRAM_TOKEN  = os.getenv("TELEGRAM_TOKEN", "")
KIE_API_KEY     = os.getenv("KIE_API_KEY", "")
KIE_BASE_URL    = os.getenv("KIE_BASE_URL", "https://api.kie.ai")
LOG_LEVEL       = os.getenv("LOG_LEVEL", "INFO").upper()

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)
log = logging.getLogger("best-veo3")

# ----------------- UI -----------------
MAIN_MENU = InlineKeyboardMarkup([
    [InlineKeyboardButton("🎬 Сгенерировать видео по тексту", callback_data="gen_text")],
    [InlineKeyboardButton("🖼️ Сгенерировать видео по фото", callback_data="gen_photo")],
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
        ctx.user_data["state"] = {"mode": None, "aspect": "16:9", "last_prompt": None, "last_image_url": None}
    return ctx.user_data["state"]

# ----------------- Kie / Veo3 -----------------
def _submit_kie(payload: dict) -> dict:
    if not (KIE_API_KEY and KIE_BASE_URL):
        return {"ok": False, "id": None, "error": "Нет API ключа KIE"}
    headers = {"Authorization": f"Bearer {KIE_API_KEY}", "Content-Type": "application/json"}
    try:
        r = requests.post(f"{KIE_BASE_URL.rstrip('/')}/v1/veo3/generations",
                          headers=headers, data=json.dumps(payload), timeout=30)
        if r.status_code == 200:
            data = r.json()
            return {"ok": True, "id": data.get("id") or data.get("task_id")}
        return {"ok": False, "id": None, "error": f"Ошибка API {r.status_code}: {r.text[:200]}"}
    except Exception as e:
        return {"ok": False, "id": None, "error": str(e)}

def submit_veo_job_text(prompt: str, aspect: str) -> dict:
    return _submit_kie({"model": "veo3", "prompt": prompt,
                        "aspect_ratio": "16:9" if aspect == "16:9" else "9:16"})

def submit_veo_job_photo(image_url: str, prompt: str, aspect: str) -> dict:
    return _submit_kie({"model": "veo3", "prompt": prompt,
                        "image_url": image_url,
                        "aspect_ratio": "16:9" if aspect == "16:9" else "9:16"})

# ----------------- Handlers -----------------
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    st = state(ctx); st["mode"] = None
    await update.message.reply_text("👋 Привет! Это бот Veo3.", reply_markup=MAIN_MENU)
    await update.message.reply_text("Выбери формат:", reply_markup=FORMAT_KB)

async def cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    st = state(ctx)
    data = q.data

    if data == "back_menu":
        await q.edit_message_text("Главное меню:", reply_markup=MAIN_MENU)
        return

    if data.startswith("fmt_"):
        st["aspect"] = "16:9" if data == "fmt_16x9" else "9:16"
        await q.edit_message_text(f"✅ Формат выбран: {st['aspect']}", reply_markup=RUN_KB)
        return

    if data == "gen_text":
        st["mode"] = "gen_text"
        await q.edit_message_text("✍️ Пришли текстовый промпт.", reply_markup=FORMAT_KB)
        return

    if data == "gen_photo":
        st["mode"] = "gen_photo"
        await q.edit_message_text("📸 Пришли фото + описание.", reply_markup=FORMAT_KB)
        return

    if data == "run":
        if not st["last_prompt"]:
            await q.answer("Нет промпта.", show_alert=True)
            return
        if st["mode"] == "gen_photo" and st.get("last_image_url"):
            res = submit_veo_job_photo(st["last_image_url"], st["last_prompt"], st["aspect"])
        else:
            res = submit_veo_job_text(st["last_prompt"], st["aspect"])
        if res["ok"]:
            await q.edit_message_text(f"✅ Задача отправлена! ID: `{res['id']}`", parse_mode=ParseMode.MARKDOWN)
        else:
            await q.edit_message_text(f"❌ Ошибка: {res['error']}", reply_markup=RUN_KB)

async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    st = state(ctx); text = (update.message.text or "").strip()
    if st["mode"] in ("gen_text", "gen_photo"):
        st["last_prompt"] = text
        await update.message.reply_text("✅ Промпт сохранён. Жми «🚀 Запустить генерацию».", reply_markup=RUN_KB)

async def on_photo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    st = state(ctx)
    try:
        photo = update.message.photo[-1]
        f = await ctx.bot.get_file(photo.file_id)
        st["last_image_url"] = f"https://api.telegram.org/file/bot{TELEGRAM_TOKEN}/{f.file_path}"
        caption = update.message.caption or ""
        st["last_prompt"] = caption if caption else None
        await update.message.reply_text("📸 Фото принято. Жми «🚀 Запустить генерацию».", reply_markup=RUN_KB)
    except Exception as e:
        await update.message.reply_text(f"Ошибка: {e}")

async def error_handler(update: Optional[Update], ctx: ContextTypes.DEFAULT_TYPE):
    log.error("Exception:\n%s", traceback.format_exc())
    try:
        if update and update.effective_chat:
            await update.effective_chat.send_message("⚠️ Что-то пошло не так.")
    except:
        pass

# ----------------- MAIN -----------------
def main():
    if not TELEGRAM_TOKEN:
        raise RuntimeError("Нет TELEGRAM_TOKEN")
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).concurrent_updates(True).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(cb))
    app.add_handler(MessageHandler(filters.PHOTO, on_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_error_handler(error_handler)

    log.info("Bot started (polling).")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()

# -*- coding: utf-8 -*-
# BEST VEO3 BOT — Veo3 Fast + Prompt-Master (PTB 21.6)
# Семплы: текст/фото → KIE /api/v1/veo/generate  (model=veo3_fast)
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

# ----------------- ENV -----------------
load_dotenv()

TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN", "")
KIE_API_KEY      = os.getenv("KIE_API_KEY", "")
OPENAI_API_KEY   = os.getenv("OPENAI_API_KEY", "") or os.getenv("OPENAI_KEY", "")
KIE_BASE_URL     = (os.getenv("KIE_BASE_URL") or "https://api.kie.ai").strip().rstrip("/")
KIE_GEN_PATH_RAW = (os.getenv("KIE_GEN_PATH") or os.getenv("KIE_GENERATE_PATH") or "/api/v1/veo/generate").strip()
PROMPTS_CHANNEL  = os.getenv("BOT_CHANNEL_URL", "https://t.me/bestveo3promts")

def _norm_path(p: str) -> str:
    if not p.startswith("/"):
        p = "/" + p
    if p.startswith("/v1/"):
        p = "/api" + p
    return p

KIE_GEN_PATH = _norm_path(KIE_GEN_PATH_RAW)

# ----------------- LOGGING -----------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)
log = logging.getLogger("best-veo3-fast")
log.info(f"KIE endpoint: {KIE_BASE_URL}{KIE_GEN_PATH}")

# ----------------- UI -----------------
def main_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🎬 Сгенерировать видео по тексту", callback_data="gen_text")],
        [InlineKeyboardButton("🖼️ Сгенерировать видео по фото",  callback_data="gen_photo")],
        [InlineKeyboardButton("🧠 Промпт-мастер (ChatGPT)",       callback_data="prompt_master")],
        [InlineKeyboardButton("💬 Обычный чат (ChatGPT)",         callback_data="chat")],
        [InlineKeyboardButton("❓ FAQ", callback_data="faq"),
         InlineKeyboardButton("📈 Канал с промптами", url=PROMPTS_CHANNEL)],
    ])

def kb_format(aspect: str) -> InlineKeyboardMarkup:
    b16  = f"{'✅ ' if aspect=='16:9' else ''}🎬 16:9"
    b916 = f"{'✅ ' if aspect=='9:16' else ''}📱 9:16"
    return InlineKeyboardMarkup([[InlineKeyboardButton(b16,  callback_data="fmt_16x9"),
                                  InlineKeyboardButton(b916, callback_data="fmt_9x16")]])

def kb_run(aspect: str) -> InlineKeyboardMarkup:
    b16  = f"{'✅ ' if aspect=='16:9' else ''}🎬 16:9"
    b916 = f"{'✅ ' if aspect=='9:16' else ''}📱 9:16"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(b16,  callback_data="fmt_16x9"),
         InlineKeyboardButton(b916, callback_data="fmt_9x16")],
        [InlineKeyboardButton("🚀 Запустить генерацию (Veo3 Fast)", callback_data="run")],
        [InlineKeyboardButton("⬅️ Назад в меню", callback_data="back_menu")],
    ])

# ----------------- STATE -----------------
def st(ctx: ContextTypes.DEFAULT_TYPE) -> Dict[str, Any]:
    if "state" not in ctx.user_data:
        ctx.user_data["state"] = {
            "mode": None,              # gen_text | gen_photo | prompt_master | chat
            "aspect": "16:9",
            "last_prompt": None,
            "last_image_url": None,
            "chat_history": []
        }
    return ctx.user_data["state"]

# ----------------- PROMPT MASTER (OpenAI) -----------------
SYSTEM_PM = {
    "role": "system",
    "content": (
        "You are a senior film director and prompt-writer for Google Veo 3. "
        "Return ONE ready-to-copy cinematic prompt in English, 500–900 characters long. "
        "No follow-up questions. Enrich the user's idea with: composition, lens (mm/anamorphic), "
        "camera moves (push-in, dolly, glide, rack focus), lighting & color palette, micro-details, "
        "atmosphere, and sound cues. No brands, logos, or on-screen text. Natural, vivid, not florid."
    )
}

def oai_prompt(idea: str) -> str:
    if not OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY not set for Prompt-Master.")
    import openai
    openai.api_key = OPENAI_API_KEY
    resp = openai.ChatCompletion.create(
        model="gpt-4o-mini",
        temperature=0.7,
        max_tokens=900,
        messages=[SYSTEM_PM, {"role": "user", "content": idea}],
    )
    return resp.choices[0].message["content"].strip()

def html_escape(s: str) -> str:
    return s.replace("&","&amp;").replace("<","&lt;").replace(">","&gt;")

def looks_like_prompt(text: str) -> bool:
    if not text: return False
    score = 0
    for kw in ["fps","lens","85mm","35mm","anamorphic","rack focus","dolly","glide",
               "color palette","lighting","bokeh","DOF","sound","audio","score","cinematic"]:
        if kw.lower() in text.lower():
            score += 1
    return score >= 2 or len(text) > 400

# ----------------- KIE (Veo3 Fast) -----------------
def _kie_url() -> str:
    url = f"{KIE_BASE_URL}{KIE_GEN_PATH}"
    url = url.replace("://","§§").replace("//","/").replace("§§","://")
    return url

def submit_kie(prompt: str, aspect: str, image_url: Optional[str]) -> Dict[str, Any]:
    if not (KIE_API_KEY and KIE_BASE_URL):
        return {"ok": False, "error": "KIE_API_KEY или KIE_BASE_URL не заданы."}
    payload = {
        "model": "veo3_fast",  # фиксируем Fast
        "prompt": prompt,
        "aspectRatio": "16:9" if aspect == "16:9" else "9:16"
    }
    if image_url:
        payload["imageUrls"] = [image_url]

    headers = {"Authorization": f"Bearer {KIE_API_KEY}", "Content-Type": "application/json"}
    url = _kie_url()
    try:
        log.info(f"KIE POST -> {url} | aspect={payload['aspectRatio']} | img={'yes' if image_url else 'no'} | prompt_len={len(prompt)}")
        r = requests.post(url, headers=headers, data=json.dumps(payload), timeout=60)
        body = r.text
        if r.status_code == 200:
            try:
                data = r.json()
            except Exception:
                return {"ok": False, "error": f"Bad JSON from API: {body[:300]}"}
            # KIE обычно -> {"code":200,...,"data":{"taskId": "..."}}
            code = int(data.get("code", 0))
            if code == 200:
                tid = (data.get("data") or {}).get("taskId") or data.get("taskId") or data.get("id") or "unknown"
                return {"ok": True, "task_id": tid}
            if code == 402:
                return {"ok": False, "error": "Недостаточно кредитов на KIE (402)."}
            return {"ok": False, "error": f"API code {code}: {data.get('msg') or 'Unknown'}"}

        if r.status_code == 402:
            return {"ok": False, "error": "Недостаточно кредитов на KIE (402)."}
        if r.status_code in (401, 403) or "Illegal IP" in body:
            return {"ok": False, "error": "Доступ API запрещён: проверьте ключ/whitelist IP."}
        return {"ok": False, "error": f"HTTP {r.status_code}: {body[:300]}"}
    except Exception as e:
        return {"ok": False, "error": f"Network error: {e}"}

# ----------------- typing indicator -----------------
async def typing_on(chat_id: int, ctx: ContextTypes.DEFAULT_TYPE, stop: asyncio.Event):
    try:
        while not stop.is_set():
            await ctx.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
            await asyncio.sleep(4)
    except Exception:
        pass

# ----------------- Handlers -----------------
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    s = st(ctx); s["mode"] = None
    await update.effective_chat.send_message("Главное меню:", reply_markup=main_menu())
    await update.effective_chat.send_message("Выбери формат кадра:", reply_markup=kb_format(s["aspect"]))

async def cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    s = st(ctx); data = q.data

    if data == "back_menu":
        s["mode"] = None
        await q.edit_message_text("Главное меню:", reply_markup=main_menu()); return

    if data.startswith("fmt_"):
        s["aspect"] = "16:9" if data == "fmt_16x9" else "9:16"
        await q.edit_message_text(f"✅ Формат: {s['aspect']}", reply_markup=kb_run(s["aspect"])); return

    if data == "gen_text":
        s["mode"] = "gen_text"; s["last_image_url"] = None
        await q.edit_message_text("✍️ Пришли идею или готовый промпт (англ.).", reply_markup=kb_format(s["aspect"])); return

    if data == "gen_photo":
        s["mode"] = "gen_photo"
        await q.edit_message_text("📸 Пришли фото (опционально добавь подпись-идею).", reply_markup=kb_format(s["aspect"])); return

    if data == "prompt_master":
        s["mode"] = "prompt_master"; s["last_image_url"] = None
        await q.edit_message_text("🧠 Режим «Промпт-мастер» активирован. Пришли идею одной-двумя фразами — я сразу верну готовый PROMPT (EN).",
                                  reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад в меню", callback_data="back_menu")]]))
        return

    if data == "chat":
        s["mode"] = "chat"
        await q.edit_message_text("💬 Обычный чат включён. /exit — выход.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад в меню", callback_data="back_menu")]])); return

    if data == "faq":
        await q.edit_message_text("FAQ:\n• Форматы: 16:9 / 9:16\n• Модель: Veo3 Fast\n• Видео без логотипов и текста в кадре.",
                                  reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад в меню", callback_data="back_menu")]]))
        return

    if data == "run":
        if not s.get("last_prompt"):
            await q.answer("Нет готового промпта.", show_alert=True); return
        await q.edit_message_text("🚀 Отправляю задачу в Veo3 Fast…")
        res = submit_kie(s["last_prompt"], s["aspect"], s.get("last_image_url"))
        if res.get("ok"):
            await q.edit_message_text(
                f"✅ Задача создана (Veo3 Fast)! ID: `{res['task_id']}`\nОжидайте рендер.",
                parse_mode=ParseMode.MARKDOWN, reply_markup=kb_run(s["aspect"])
            )
        else:
            await q.edit_message_text(f"❌ Ошибка запуска генерации: {res.get('error')}", reply_markup=kb_run(s["aspect"]))
        return

async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    s = st(ctx)
    txt = (update.message.text or "").strip()
    chat_id = update.effective_chat.id

    # CHAT
    if s["mode"] == "chat":
        await update.message.reply_text("Я здесь для генерации и промптов. Для идей — включи «Промпт-мастер».")
        return

    # PROMPT MASTER
    if s["mode"] == "prompt_master":
        notice = await update.message.reply_text("⌛ Writing your cinematic prompt…")
        stop = asyncio.Event()
        asyncio.create_task(typing_on(chat_id, ctx, stop))
        try:
            prompt_en = oai_prompt(txt)
            s["last_prompt"] = prompt_en
            stop.set()
            # показываем в <pre>, плюс появится кнопка запуска
            await notice.edit_text(
                f"🧠 Готовый промпт для Veo3 Fast:\n<pre>{html_escape(prompt_en)}</pre>",
                parse_mode=ParseMode.HTML,
                reply_markup=kb_run(s["aspect"])
            )
        except Exception as e:
            stop.set()
            await notice.edit_text(f"❌ Prompt-Master error: {e}")
        return

    # GEN BY TEXT / PHOTO
    if s["mode"] in (None, "gen_text", "gen_photo"):
        # если фото-режим, но фото нет
        if s["mode"] == "gen_photo" and not s.get("last_image_url"):
            await update.message.reply_text("Мне нужно фото. Пришли изображение (подпись — по желанию).")
            return

        if looks_like_prompt(txt):
            s["last_prompt"] = txt
            await update.message.reply_text("✅ Промпт принят. Нажми «🚀 Запустить генерацию».", reply_markup=kb_run(s["aspect"]))
            return

        # Иначе — прокачаем идею через Prompt-Master
        notice = await update.message.reply_text("⌛ Формулирую кинематографический промпт…")
        stop = asyncio.Event()
        asyncio.create_task(typing_on(chat_id, ctx, stop))
        try:
            prompt_en = oai_prompt(txt)
            s["last_prompt"] = prompt_en
            stop.set()
            await notice.edit_text(
                f"🧠 Готово. Проверь и запускай:\n<pre>{html_escape(prompt_en)}</pre>",
                parse_mode=ParseMode.HTML,
                reply_markup=kb_run(s["aspect"])
            )
        except Exception as e:
            stop.set()
            await notice.edit_text(f"❌ Ошибка при подготовке промпта: {e}")
        return

async def on_photo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    s = st(ctx); chat_id = update.effective_chat.id
    try:
        photo = update.message.photo[-1]
        f = await ctx.bot.get_file(photo.file_id)
        s["last_image_url"] = f"https://api.telegram.org/file/bot{TELEGRAM_TOKEN}/{f.file_path}"

        caption = (update.message.caption or "").strip()
        if caption:
            notice = await update.message.reply_text("📸 Фото получено. ⌛ Подготавливаю промпт…")
            stop = asyncio.Event()
            asyncio.create_task(typing_on(chat_id, ctx, stop))
            try:
                prompt_en = oai_prompt(caption)
                s["last_prompt"] = prompt_en
                stop.set()
                await notice.edit_text(
                    f"✅ Фото и промпт готовы:\n<pre>{html_escape(prompt_en)}</pre>",
                    parse_mode=ParseMode.HTML,
                    reply_markup=kb_run(s["aspect"])
                )
            except Exception as e:
                stop.set()
                await notice.edit_text(f"❌ Ошибка при подготовке промпта: {e}")
        else:
            s["mode"] = "gen_photo"
            await update.message.reply_text("📸 Фото есть. Пришли короткую идею — я сделаю промпт.",
                                            reply_markup=kb_run(s["aspect"]))
    except Exception as e:
        await update.message.reply_text(f"❌ Не удалось обработать фото: {e}")

async def cmd_exit(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data.clear()
    await update.message.reply_text("Ок, выхожу из режима. Открываю меню…", reply_markup=ReplyKeyboardRemove())
    await cmd_start(update, ctx)

async def on_error(update: Optional[Update], ctx: ContextTypes.DEFAULT_TYPE):
    log.error("Exception:\n%s", traceback.format_exc())
    try:
        if update and update.effective_chat:
            await update.effective_chat.send_message("⚠️ Что-то пошло не так. Попробуйте ещё раз.")
    except:
        pass

def main():
    if not TELEGRAM_TOKEN:
        raise RuntimeError("TELEGRAM_TOKEN is empty")
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).concurrent_updates(True).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("exit",  cmd_exit))

    app.add_handler(CallbackQueryHandler(cb,
        pattern=r"^(gen_text|gen_photo|prompt_master|chat|faq|back_menu|fmt_16x9|fmt_9x16|run)$"))

    app.add_handler(MessageHandler(filters.PHOTO, on_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    app.add_error_handler(on_error)
    log.info("Bot started (polling).")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()

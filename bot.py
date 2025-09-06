# -*- coding: utf-8 -*-
# BEST VEO3 BOT — text & photo generation + Prompt-Master
# PTB v20+, requests

import os
import json
import logging
import traceback
import requests
from typing import Optional, Dict, Any

from dotenv import load_dotenv
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardRemove
from telegram.constants import ParseMode
from telegram.ext import (
    ApplicationBuilder, Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters
)

# ======================= ENV & LOG =======================
load_dotenv()

BOT_TOKEN       = os.getenv("TELEGRAM_TOKEN") or os.getenv("BOT_TOKEN") or ""
OPENAI_API_KEY  = os.getenv("OPENAI_API_KEY") or os.getenv("OPENAI_KEY") or ""
KIE_API_KEY     = os.getenv("KIE_API_KEY", "")
KIE_BASE_URL    = (os.getenv("KIE_BASE_URL") or "https://api.kie.ai").strip().rstrip("/")
_raw_path       = (os.getenv("KIE_GENERATE_PATH") or os.getenv("KIE_GEN_PATH") or "/api/v1/veo/generate").strip()

def _normalize_path(p: str) -> str:
    """Ensure correct API path: starts with /api..., even if given as /v1..."""
    if not p.startswith("/"):
        p = "/" + p
    if p.startswith("/v1/"):
        p = "/api" + p
    return p

KIE_GENERATE_PATH = _normalize_path(_raw_path)

LOG_LEVEL = (os.getenv("LOG_LEVEL") or "INFO").upper()
logging.basicConfig(level=getattr(logging, LOG_LEVEL, logging.INFO),
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("best-veo3")

log.info(f"KIE endpoint: {KIE_BASE_URL}{KIE_GENERATE_PATH}")

# ======================= UI: KEYBOARDS =======================
MAIN_MENU = InlineKeyboardMarkup([
    [InlineKeyboardButton("🎬 Сгенерировать видео по тексту", callback_data="mode_gen_text")],
    [InlineKeyboardButton("🖼️ Сгенерировать видео по фото",  callback_data="mode_gen_photo")],
    [InlineKeyboardButton("🧠 Промпт-мастер (ChatGPT)",       callback_data="mode_prompt_master")],
    [InlineKeyboardButton("💬 Обычный чат (ChatGPT)",         callback_data="mode_chat")],
    [InlineKeyboardButton("❓ FAQ", callback_data="faq"),
     InlineKeyboardButton("📚 Канал с промптами", url="https://t.me/bestveo3promts")]
])

def kb_format_only(aspect: str) -> InlineKeyboardMarkup:
    b16  = f"{'✅ ' if aspect=='16:9' else ''}🎬 16:9"
    b916 = f"{'✅ ' if aspect=='9:16' else ''}📱 9:16"
    return InlineKeyboardMarkup([[InlineKeyboardButton(b16,  callback_data="fmt_16x9"),
                                  InlineKeyboardButton(b916, callback_data="fmt_9x16")]])

def kb_run_with_format(aspect: str) -> InlineKeyboardMarkup:
    b16  = f"{'✅ ' if aspect=='16:9' else ''}🎬 16:9"
    b916 = f"{'✅ ' if aspect=='9:16' else ''}📱 9:16"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(b16,  callback_data="fmt_16x9"),
         InlineKeyboardButton(b916, callback_data="fmt_9x16")],
        [InlineKeyboardButton("🚀 Запустить генерацию", callback_data="run")],
        [InlineKeyboardButton("⬅️ Назад в меню", callback_data="back_menu")]
    ])

AFTER_PM_ACTIONS = InlineKeyboardMarkup([
    [InlineKeyboardButton("🎬 Сгенерировать по тексту", callback_data="mode_gen_text_from_pm"),
     InlineKeyboardButton("🖼️ Сгенерировать по фото",  callback_data="mode_gen_photo_from_pm")],
])

# ======================= STATE =======================
def state(ctx: ContextTypes.DEFAULT_TYPE) -> Dict[str, Any]:
    if "state" not in ctx.user_data:
        ctx.user_data["state"] = {
            "mode": None,              # "gen_text" | "gen_photo" | "prompt_master" | "chat"
            "aspect": "16:9",
            "last_prompt": None,
            "last_image_url": None,
            "chat_history": []
        }
    return ctx.user_data["state"]

# ======================= HELPERS =======================
def looks_like_ready_prompt(text: str) -> bool:
    if not text:
        return False
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

def oai_chat(messages, temperature=0.7, max_tokens=900) -> str:
    if not OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY not set.")
    import openai
    openai.api_key = OPENAI_API_KEY
    resp = openai.ChatCompletion.create(
        model="gpt-4o-mini",
        messages=messages,
        temperature=temperature,
        max_tokens=max_tokens,
    )
    return resp.choices[0].message["content"].strip()

# Prepare system prompt for Prompt-Master mode (for OpenAI)
SYSTEM_PM = {
    "role": "system",
    "content": (
        "Ты — режиссёр-постановщик/промпт-сценарист для Veo3. "
        "Не меняй идею пользователя, а усиливай её: композиция, оптика (мм/анаморф), "
        "движение камеры (push-in, dolly, glide, rack focus), свет/палитра, темп/ритм, "
        "микро-детали (пыль, пар, блики), звук (музыка/шум/микс). "
        "Пиши кинематографично, живым английским, 3–6 абзацев (500–900 символов). "
        "Никакого текста/логотипов/субтитров в кадре."
    )
}

# ======================= KIE / VEO3 =======================
def _kie_url() -> str:
    # Build full endpoint URL safely
    url = f"{KIE_BASE_URL}{KIE_GENERATE_PATH}"
    # Avoid double slashes
    url = url.replace("://", "§§").replace("//", "/").replace("§§", "://")
    return url

def _submit_kie(payload: dict) -> dict:
    """Submit video generation task to KIE. Model is fixed to veo3."""
    if not (KIE_API_KEY and KIE_BASE_URL):
        return {"ok": False, "id": None, "error": "KIE_API_KEY or KIE_BASE_URL not set."}
    payload = dict(payload or {})
    payload["model"] = "veo3"
    try:
        url = _kie_url()
        headers = {"Authorization": f"Bearer {KIE_API_KEY}", "Content-Type": "application/json"}
        log.info(f"KIE POST -> {url} | payload: {{'model':'{payload.get('model')}','aspectRatio':'{payload.get('aspectRatio')}',"
                 f"'image':{'yes' if payload.get('imageUrls') else 'no'}, 'prompt_len':{len(payload.get('prompt',''))}}}")
        r = requests.post(url, headers=headers, data=json.dumps(payload), timeout=60)
    except Exception as e:
        log.error(f"Exception during KIE request: {e}")
        return {"ok": False, "id": None, "error": f"Request error: {e}"}
    if r.status_code == 200:
        try:
            data = r.json()
        except Exception:
            data = {}
        if isinstance(data, dict) and data.get("code") and int(data.get("code")) != 0:
            return {"ok": False, "id": None, "error": f"API code {data.get('code')}: {data.get('msg')}"}
        # Return task ID if available
        task_id = None
        if isinstance(data, dict):
            task_id = data.get("taskId") or data.get("taskid") or data.get("id") or data.get("task_id")
        if not task_id:
            task_id = "unknown"
        return {"ok": True, "id": task_id, "error": None}
    else:
        # HTTP error handling
        body_preview = r.text[:400]
        if r.status_code == 402:
            return {"ok": False, "id": None, "error": "Недостаточно кредитов на KIE аккаунте."}
        if "Illegal IP" in body_preview or r.status_code in (401, 403):
            return {"ok": False, "id": None, "error": "Доступ API запрещён: IP платформы не в whitelist KIE."}
        return {"ok": False, "id": None, "error": f"API error {r.status_code}: {body_preview}"}

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /start command: reset state and show main menu."""
    s = state(context)
    # Reset state fields for a fresh start
    s["mode"] = None
    s["last_prompt"] = None
    s["last_image_url"] = None
    s["chat_history"] = []
    # Send main menu
    await update.message.reply_text(
        "Привет! Это бот Best VEO3.\nВыберите режим:",
        reply_markup=MAIN_MENU
    )
    # Clear stored active message id (if any)
    context.user_data.pop("active_message_id", None)

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle all inline button presses (CallbackQuery)."""
    query = update.callback_query
    data = query.data
    s = state(context)
    try:
        if data == "mode_gen_text":
            # Switch to text prompt mode
            s["mode"] = "gen_text"
            s["last_prompt"] = None
            s["last_image_url"] = None
            aspect = s.get("aspect", "16:9")
            await query.answer()
            await query.edit_message_text(
                "✏️ Отправьте текстовое описание для видео:",
                reply_markup=kb_format_only(aspect)
            )
            context.user_data["active_message_id"] = query.message.message_id

        elif data == "mode_gen_photo":
            # Switch to photo prompt mode
            s["mode"] = "gen_photo"
            s["last_prompt"] = None
            s["last_image_url"] = None
            aspect = s.get("aspect", "16:9")
            await query.answer()
            await query.edit_message_text(
                "🖼️ Пришлите фотографию для генерации видео:",
                reply_markup=kb_format_only(aspect)
            )
            context.user_data["active_message_id"] = query.message.message_id

        elif data == "mode_prompt_master":
            # Switch to Prompt-Master mode
            s["mode"] = "prompt_master"
            s["last_prompt"] = None
            s["last_image_url"] = None
            await query.answer()
            await query.edit_message_text(
                "🧠 Режим Промпт-мастера.\nОпишите идею видео, а я сделаю кинематографичный промпт.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад в меню", callback_data="back_menu")]])
            )
            context.user_data["active_message_id"] = query.message.message_id

        elif data == "mode_chat":
            # Switch to normal chat mode
            s["mode"] = "chat"
            s["last_prompt"] = None
            s["last_image_url"] = None
            # Start a new chat history
            s["chat_history"] = []
            s["chat_history"].append({"role": "system", "content": "You are ChatGPT, a helpful assistant."})
            await query.answer()
            await query.edit_message_text(
                "💬 Чат-режим активирован. Задайте любой вопрос.\n(Чтобы выйти, нажмите /start)",
                reply_markup=ReplyKeyboardRemove()
            )
            context.user_data.pop("active_message_id", None)

        elif data == "faq":
            await query.answer()
            faq_text = (
                "❓ **FAQ**\n\n"
                "• *Что умеет этот бот?* Бот может генерировать короткие видеоролики по вашему текстовому описанию или фотографии с помощью ИИ (модель Veo3). "
                "Также есть режим Промпт-мастера для улучшения ваших описаний и обычный чат с ИИ.\n\n"
                "• *Сколько это стоит?* Генерация видео использует кредиты KIE.AI. Убедитесь, что на вашем аккаунте KIE.AI достаточно кредитов.\n\n"
                "• *Сколько времени занимает генерация?* Обычно 30-60 секунд. Если запрос сложный, может потребоваться больше времени.\n\n"
                "• *Видео не генерируется или выдаётся ошибка.* Это может произойти из-за нарушения контентных правил или нехватки кредитов. Попробуйте изменить запрос или проверьте баланс."
            )
            await query.edit_message_text(faq_text, parse_mode=ParseMode.MARKDOWN,
                                          reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад в меню", callback_data="back_menu")]]))

        elif data in ("fmt_16x9", "fmt_9x16"):
            # Aspect ratio toggled
            new_aspect = "16:9" if data == "fmt_16x9" else "9:16"
            s["aspect"] = new_aspect
            # Determine which keyboard to show (with or without run)
            if s.get("last_prompt") or s.get("last_image_url"):
                new_kb = kb_run_with_format(new_aspect)
            else:
                new_kb = kb_format_only(new_aspect)
            await query.answer()
            try:
                await query.edit_message_reply_markup(reply_markup=new_kb)
            except Exception as e:
                log.warning(f"Failed to edit inline keyboard on format toggle: {e}")
                try:
                    await query.edit_message_text(text=query.message.text, reply_markup=new_kb)
                except Exception as e2:
                    log.error(f"Failed to edit message text on format toggle: {e2}")

        elif data == "run":
            # Start generation
            if not s.get("last_prompt") and not s.get("last_image_url"):
                await query.answer("❗ Нет описания или фото для генерации.", show_alert=True)
                return
            await query.answer()
            try:
                await query.edit_message_text("⏳ Видео генерируется, пожалуйста, подождите...")
            except Exception as e:
                log.error(f"Failed to edit message to 'generating': {e}")
            payload = {
                "prompt": s.get("last_prompt") or "",
                "imageUrls": [s.get("last_image_url")] if s.get("last_image_url") else [],
                "aspectRatio": s.get("aspect", "16:9"),
                "enableFallback": True
            }
            result = _submit_kie(payload)
            if not result["ok"] or not result["id"] or result["id"] == "unknown":
                err_msg = result.get("error") or "Неизвестная ошибка"
                await query.edit_message_text(f"❌ Ошибка запуска генерации: {err_msg}",
                                              reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад в меню", callback_data="back_menu")]]))
                s["mode"] = None
                return
            task_id = result["id"]
            log.info(f"Generation task submitted, id={task_id}")
            context.job_queue.run_repeating(poll_result, interval=5, first=5,
                                            data={"task_id": task_id, "chat_id": update.effective_chat.id,
                                                  "message_id": query.message.message_id, "tries": 0},
                                            name=str(task_id))

        elif data == "back_menu":
            # Return to main menu from any sub-mode
            # Cancel any ongoing generation polling jobs (by removing all jobs for this chat)
            for job in context.job_queue.jobs():
                if job.data and job.data.get("chat_id") == update.effective_chat.id:
                    job.schedule_removal()
            s["mode"] = None
            s["last_prompt"] = None
            s["last_image_url"] = None
            await query.answer()
            await query.edit_message_text("Выберите режим:", reply_markup=MAIN_MENU)

        elif data == "mode_gen_text_from_pm":
            if not s.get("last_prompt"):
                await query.answer()
                await query.edit_message_text("❗ Промпт не найден. Попробуйте снова.",
                                              reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад в меню", callback_data="back_menu")]]))
                return
            s["mode"] = "gen_text"
            s["last_image_url"] = None
            aspect = s.get("aspect", "16:9")
            escaped_prompt = html_escape(s["last_prompt"])
            text_html = f"<pre>{escaped_prompt}</pre>\n\n✅ Промпт готов. Выберите формат и нажмите \"Запустить генерацию\"."
            await query.answer()
            try:
                await query.edit_message_text(text_html, parse_mode=ParseMode.HTML, reply_markup=kb_run_with_format(aspect))
            except Exception as e:
                log.warning(f"Failed to edit prompt message for gen_text_from_pm: {e}")
                await query.message.reply_text("Промпт получен. Выберите формат и нажмите 'Запустить генерацию'.",
                                               reply_markup=kb_run_with_format(aspect), parse_mode=ParseMode.HTML)
            context.user_data["active_message_id"] = query.message.message_id

        elif data == "mode_gen_photo_from_pm":
            if not s.get("last_prompt"):
                await query.answer()
                await query.edit_message_text("❗ Промпт не найден. Попробуйте снова.",
                                              reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад в меню", callback_data="back_menu")]]))
                return
            s["mode"] = "gen_photo"
            s["last_image_url"] = None
            aspect = s.get("aspect", "16:9")
            escaped_prompt = html_escape(s["last_prompt"])
            text_html = f"<pre>{escaped_prompt}</pre>\n\n📷 Промпт готов. Теперь отправьте фото для генерации видео по этому описанию."
            await query.answer()
            try:
                await query.edit_message_text(text_html, parse_mode=ParseMode.HTML, reply_markup=kb_format_only(aspect))
            except Exception as e:
                log.warning(f"Failed to edit prompt message for gen_photo_from_pm: {e}")
                await query.message.reply_text("Отправьте фотографию для генерации видео по этому описанию.",
                                               reply_markup=kb_format_only(aspect))
            context.user_data["active_message_id"] = query.message.message_id

    except Exception as e:
        log.error(f"Error in handle_callback: {e}\n{traceback.format_exc()}")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle incoming text messages and photos depending on the current mode."""
    s = state(context)
    mode = s.get("mode")
    if mode == "gen_text":
        text = update.message.text
        if not text:
            return
        s["last_prompt"] = text.strip()
        s["last_image_url"] = None
        aspect = s.get("aspect", "16:9")
        msg_id = context.user_data.get("active_message_id")
        if msg_id:
            try:
                await context.bot.edit_message_text(chat_id=update.effective_chat.id, message_id=msg_id,
                                                    text="✏️ Описание получено. Нажмите \"Запустить генерацию\" когда будете готовы.",
                                                    reply_markup=kb_run_with_format(aspect))
            except Exception as e:
                log.warning(f"Failed to edit prompt message in gen_text: {e}")
                await update.message.reply_text("Описание получено. Выберите формат и нажмите 'Запустить генерацию'.",
                                                reply_markup=kb_run_with_format(aspect))
        else:
            await update.message.reply_text("Описание получено. Выберите формат и нажмите 'Запустить генерацию'.",
                                            reply_markup=kb_run_with_format(aspect))

    elif mode == "gen_photo":
        if update.message.photo:
            photo = update.message.photo[-1]
            file_id = photo.file_id
            file = await context.bot.get_file(file_id)
            image_url = file.file_path
            s["last_image_url"] = image_url
            # If caption was provided with the photo, use as prompt
            caption = update.message.caption
            if caption:
                s["last_prompt"] = caption.strip()
            aspect = s.get("aspect", "16:9")
            msg_id = context.user_data.get("active_message_id")
            if msg_id:
                base_text = ""
                if s.get("last_prompt"):
                    base_text = f"<pre>{html_escape(s['last_prompt'])}</pre>\n\n"
                new_text = base_text + "👍 Фото получено. Нажмите \"Запустить генерацию\"."
                try:
                    await context.bot.edit_message_text(chat_id=update.effective_chat.id, message_id=msg_id,
                                                        text=new_text, parse_mode=ParseMode.HTML,
                                                        reply_markup=kb_run_with_format(aspect))
                except Exception as e:
                    log.warning(f"Failed to edit message on photo received: {e}")
                    await update.message.reply_text("Фото получено. Выберите формат и нажмите 'Запустить генерацию'.",
                                                    reply_markup=kb_run_with_format(aspect))
            else:
                await update.message.reply_text("Фото получено. Выберите формат и нажмите 'Запустить генерацию'.",
                                                reply_markup=kb_run_with_format(aspect))
        elif update.message.text:
            text = update.message.text.strip()
            if not s.get("last_image_url"):
                await update.message.reply_text("❗ Пожалуйста, отправьте изображение для генерации видео.")
            else:
                s["last_prompt"] = text
                aspect = s.get("aspect", "16:9")
                msg_id = context.user_data.get("active_message_id")
                if msg_id:
                    base_text = ""
                    if s.get("last_prompt"):
                        base_text = f"<pre>{html_escape(s['last_prompt'])}</pre>\n\n"
                    new_text = base_text + "✏️ Описание добавлено к фото. Нажмите \"Запустить генерацию\"."
                    try:
                        await context.bot.edit_message_text(chat_id=update.effective_chat.id, message_id=msg_id,
                                                            text=new_text, parse_mode=ParseMode.HTML,
                                                            reply_markup=kb_run_with_format(aspect))
                    except Exception as e:
                        log.warning(f"Failed to edit message on prompt added to photo: {e}")
                        await update.message.reply_text("Описание получено. Нажмите 'Запустить генерацию'.",
                                                        reply_markup=kb_run_with_format(aspect))
                else:
                    await update.message.reply_text("Описание для фото получено. Нажмите 'Запустить генерацию'.",
                                                    reply_markup=kb_run_with_format(aspect))
    elif mode == "prompt_master":
        user_text = update.message.text
        if not user_text:
            return
        if looks_like_ready_prompt(user_text):
            log.info("User prompt looks like a ready prompt, processing anyway.")
        messages = [SYSTEM_PM, {"role": "user", "content": user_text.strip()}]
        try:
            result_prompt = await context.application.run_in_thread(lambda: oai_chat(messages))
        except Exception as e:
            log.error(f"OpenAI API error: {e}")
            await update.message.reply_text("❌ Ошибка при генерации промпта. Попробуйте позже.")
            s["mode"] = None
            return
        s["last_prompt"] = result_prompt
        escaped = html_escape(result_prompt)
        await update.message.reply_text(f"<pre>{escaped}</pre>", parse_mode=ParseMode.HTML, reply_markup=AFTER_PM_ACTIONS)
        # Mode remains prompt_master until user chooses next action

    elif mode == "chat":
        user_text = update.message.text
        if not user_text:
            return
        history = s["chat_history"]
        if not history or history[-1].get("role") != "assistant":
            if not history or history[0]["role"] != "system":
                history.insert(0, {"role": "system", "content": "You are ChatGPT, a large language model."})
        history.append({"role": "user", "content": user_text})
        if len(history) > 20:
            if history[0]["role"] == "system" and len(history) > 2:
                history.pop(1)
                history.pop(1)
            else:
                history.pop(0)
        try:
            response_text = await context.application.run_in_thread(lambda: oai_chat(history))
        except Exception as e:
            log.error(f"OpenAI chat API error: {e}")
            await update.message.reply_text("❌ Ошибка при получении ответа от ChatGPT.")
            return
        history.append({"role": "assistant", "content": response_text})
        await update.message.reply_text(response_text)
    else:
        if update.message.text and update.message.text.startswith('/'):
            return
        await update.message.reply_text("Для начала работы используйте команду /start и выберите режим.")
        return

async def poll_result(context: ContextTypes.DEFAULT_TYPE):
    """Background job: poll KIE for generation result."""
    job = context.job
    data = job.data
    task_id = data.get("task_id")
    chat_id = data.get("chat_id")
    msg_id = data.get("message_id")
    tries = data.get("tries", 0)
    data["tries"] = tries + 1
    status_url = f"{KIE_BASE_URL}/api/v1/veo/record-info?taskId={task_id}"
    status_url = status_url.replace("://", "§§").replace("//", "/").replace("§§", "://")
    try:
        headers = {"Authorization": f"Bearer {KIE_API_KEY}"}
        resp = requests.get(status_url, headers=headers, timeout=10)
    except Exception as e:
        log.error(f"Error polling result for {task_id}: {e}")
        return
    if resp.status_code != 200:
        log.warning(f"Polling HTTP error {resp.status_code} for task {task_id}: {resp.text[:200]}")
        return
    try:
        result = resp.json()
    except Exception as e:
        log.error(f"Failed to parse JSON for task {task_id}: {e}")
        return
    code = result.get("code")
    if code != 200:
        # If API returned an error code in JSON
        if code in [0, 422]:
            log.info(f"Task {task_id} status code {code}, still processing or fallback in progress.")
            return
        else:
            msg = result.get("msg", "Unknown error")
            try:
                context.application.create_task(context.bot.send_message(chat_id, f"❌ Генерация не удалась: {msg}"))
            except Exception as e:
                log.error(f"Failed to send failure message for task {task_id}: {e}")
            job.schedule_removal()
            user_data = context.job_queue.application.user_data.get(chat_id, {})
            if user_data:
                st = user_data.get("state", {})
                st["mode"] = None
                st["last_prompt"] = None
                st["last_image_url"] = None
            try:
                context.application.create_task(context.bot.edit_message_text(chat_id=chat_id, message_id=msg_id,
                                                                              text="Выберите режим:", reply_markup=MAIN_MENU))
            except Exception as e:
                log.warning(f"Could not edit message to main menu after failure: {e}")
            return
    data_obj = result.get("data", {})
    success_flag = data_obj.get("successFlag")
    if success_flag is None or success_flag == 0:
        if data["tries"] == 12:
            try:
                context.application.create_task(context.bot.edit_message_text(chat_id=chat_id, message_id=msg_id,
                                                                              text="⏳ Генерация занимает больше времени, пожалуйста, ожидайте..."))
            except Exception as e:
                log.info(f"Failed to edit message for long wait notice: {e}")
        return
    if success_flag == 1:
        response = data_obj.get("response", {})
        result_urls = []
        if "resultUrls" in response:
            result_urls = response.get("resultUrls", [])
        elif "info" in data_obj and isinstance(data_obj["info"], dict):
            info = data_obj["info"]
            result_urls = info.get("resultUrls", []) or []
        video_url = None
        if result_urls:
            video_url = result_urls[0]
        else:
            origin_urls = response.get("originUrls") or data_obj.get("originUrls") or []
            if origin_urls:
                video_url = origin_urls[0]
        if not video_url:
            try:
                context.application.create_task(context.bot.send_message(chat_id, "❌ Не удалось получить ссылку на видео."))
            except:
                pass
            job.schedule_removal()
            user_data = context.job_queue.application.user_data.get(chat_id, {})
            if user_data:
                st = user_data.get("state", {})
                st["mode"] = None
                st["last_prompt"] = None
                st["last_image_url"] = None
            try:
                context.application.create_task(context.bot.edit_message_text(chat_id=chat_id, message_id=msg_id,
                                                                              text="Выберите режим:", reply_markup=MAIN_MENU))
            except:
                pass
            return
        try:
            context.application.create_task(context.bot.send_video(chat_id, video=video_url, caption="🎬 Ваше видео готово."))
        except Exception as e:
            log.error(f"Failed to send video from URL, will try downloading. Error: {e}")
            try:
                video_data = requests.get(video_url, timeout=60)
                if video_data.status_code == 200:
                    context.application.create_task(context.bot.send_video(chat_id, video=video_data.content, filename="video.mp4", caption="🎬 Ваше видео готово."))
                else:
                    context.application.create_task(context.bot.send_message(chat_id, f"Видео готово, но загрузка не удалась (HTTP {video_data.status_code}). Ссылка: {video_url}"))
            except Exception as e2:
                log.error(f"Failed to download video: {e2}")
                context.application.create_task(context.bot.send_message(chat_id, f"Видео сгенерировано, но не удалось отправить файл.\nСсылка для загрузки: {video_url}"))
        job.schedule_removal()
        user_data = context.job_queue.application.user_data.get(chat_id, {})
        if user_data:
            st = user_data.get("state", {})
            st["mode"] = None
            st["last_prompt"] = None
            st["last_image_url"] = None
        try:
            context.application.create_task(context.bot.edit_message_text(chat_id=chat_id, message_id=msg_id,
                                                                          text="Выберите режим:", reply_markup=MAIN_MENU))
        except Exception as e:
            log.warning(f"Failed to edit message to main menu after success: {e}")
    else:
        err_code = data_obj.get("errorCode") or "Ошибка"
        err_msg = data_obj.get("errorMessage") or result.get("msg", "Generation failed.")
        try:
            context.application.create_task(context.bot.send_message(chat_id, f"❌ Генерация не удалась. Код ошибки: {err_code}\nСообщение: {err_msg}"))
        except Exception as e:
            log.error(f"Failed to send failure message to user: {e}")
        job.schedule_removal()
        user_data = context.job_queue.application.user_data.get(chat_id, {})
        if user_data:
            st = user_data.get("state", {})
            st["mode"] = None
            st["last_prompt"] = None
            st["last_image_url"] = None
        try:
            context.application.create_task(context.bot.edit_message_text(chat_id=chat_id, message_id=msg_id,
                                                                          text="Выберите режим:", reply_markup=MAIN_MENU))
        except Exception as e:
            log.warning(f"Failed to edit message to main menu after failure2: {e}")

# Initialize and run the bot (not shown here, ensure to add handlers and call app.run_polling())

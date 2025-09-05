# -*- coding: utf-8 -*-
# BEST VEO3 BOT — text & photo generation + Prompt-Master (PTB v20+)

import os, json, logging, traceback, requests, asyncio
from typing import Optional, Dict, Any

from dotenv import load_dotenv
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardRemove
from telegram.constants import ParseMode
from telegram.ext import (
    ApplicationBuilder, Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters
)

# =============== ENV & LOG ===============
load_dotenv()

BOT_TOKEN       = os.getenv("TELEGRAM_TOKEN") or os.getenv("BOT_TOKEN") or ""
OPENAI_API_KEY  = os.getenv("OPENAI_API_KEY") or os.getenv("OPENAI_KEY") or ""
KIE_API_KEY     = os.getenv("KIE_API_KEY", "")
KIE_BASE_URL    = (os.getenv("KIE_BASE_URL") or "https://api.kie.ai").strip().rstrip("/")
_raw_path       = (os.getenv("KIE_GENERATE_PATH") or os.getenv("KIE_GEN_PATH") or "/api/v1/veo/generate").strip()

LOG_LEVEL       = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(level=getattr(logging, LOG_LEVEL, logging.INFO),
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("best-veo3")

def _normalize_path(p: str) -> str:
    """Гарантируем корректный маршрут: начинается с /api..., даже если задали /v1..."""
    if not p.startswith("/"):
        p = "/" + p
    if p.startswith("/v1/"):
        p = "/api" + p
    # унифицируем популярные варианты
    p = p.replace("//", "/")
    return p

KIE_GEN_PATH = _normalize_path(_raw_path)

log.info("KIE endpoint: %s%s", KIE_BASE_URL, KIE_GEN_PATH)

# =============== UI: KEYBOARDS ===============
MAIN_MENU = InlineKeyboardMarkup([
    [InlineKeyboardButton("🎬 Сгенерировать видео по тексту", callback_data="mode_gen_text")],
    [InlineKeyboardButton("🖼️ Сгенерировать видео по фото",  callback_data="mode_gen_photo")],
    [InlineKeyboardButton("🧠 Промпт-мастер (ChatGPT)",       callback_data="mode_prompt_master")],
    [InlineKeyboardButton("💬 Обычный чат (ChatGPT)",         callback_data="mode_chat")],
    [InlineKeyboardButton("❓ FAQ", callback_data="faq"),
     InlineKeyboardButton("📚 Канал с промптами", url="https://t.me/bestveo3promts")],
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
        [InlineKeyboardButton("⬅️ Назад в меню", callback_data="back_menu")],
    ])

AFTER_PM_ACTIONS = InlineKeyboardMarkup([
    [InlineKeyboardButton("🎬 Сгенерировать по тексту", callback_data="mode_gen_text_from_pm")],
    [InlineKeyboardButton("🖼️ Сгенерировать по фото",  callback_data="mode_gen_photo_from_pm")],
])

# =============== STATE ===============
def state(ctx: ContextTypes.DEFAULT_TYPE) -> Dict[str, Any]:
    if "state" not in ctx.user_data:
        ctx.user_data["state"] = {
            "mode": None,              # gen_text | gen_photo | prompt_master | chat
            "aspect": "16:9",
            "last_prompt": None,
            "last_image_url": None,
            "chat_history": []
        }
    return ctx.user_data["state"]

# =============== HELPERS ===============
def looks_like_ready_prompt(text: str) -> bool:
    if not text: return False
    if text.strip().startswith("{") and "}" in text:
        return True
    score = 0
    for kw in ["fps","anamorphic","85mm","35mm","lens","DOF","bokeh","rack focus",
               "color palette","lighting","camera","glide","push-in","tone","sound",
               "\"shot\"","\"scene\"","\"audio\"","cinematic"]:
        if kw.lower() in text.lower(): score += 1
    return score >= 3 or len(text) > 400

def html_escape(s: str) -> str:
    return (s.replace("&","&amp;").replace("<","&lt;").replace(">","&gt;"))

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
    "role":"system",
    "content":(
        "Ты — режиссёр-постановщик/промпт-сценарист для Veo3. "
        "Не меняй идею пользователя, а усиливай её: композиция, оптика (мм/анаморф), "
        "движение камеры (push-in, dolly, glide, rack focus), свет/палитра, темп/ритм, "
        "микро-детали (пыль, пар, блики), звук (музыка/шум/микс). "
        "Пиши кинематографично, живым английским, 3–6 абзацев (500–900 символов). "
        "Никакого текста/логотипов/субтитров в кадре."
    )
}

# =============== KIE / VEO3 ===============
KIE_FALLBACK_GEN_PATHS = [
    KIE_GEN_PATH,                   # из ENV (нормализованный)
    "/api/v1/generations",          # общий
    "/v1/veo3/generations",         # старый
]

def _extract_task_id(data: dict) -> Optional[str]:
    return (
        data.get("task_id") or data.get("taskId") or data.get("id") or
        (data.get("data") or {}).get("task_id") or
        (data.get("data") or {}).get("taskId") or
        (data.get("result") or {}).get("task_id") or
        (data.get("result") or {}).get("taskId")
    )

def _extract_result_url(data: dict) -> Optional[str]:
    return (
        data.get("result_url") or data.get("video_url") or data.get("url") or
        (data.get("data") or {}).get("result_url") or
        (data.get("data") or {}).get("video_url") or
        (data.get("result") or {}).get("url")
    )

def _post_json(url: str, payload: dict, headers: dict) -> requests.Response:
    log.info("HTTP POST %s", url)
    r = requests.post(url, headers=headers, json=payload, timeout=45)
    body_preview = (r.text or "")[:500]
    log.warning("KIE %s -> %s | payload=%s | body=%s", r.status_code, url, payload, body_preview)
    return r

def _submit_kie(payload: dict) -> dict:
    if not (KIE_API_KEY and KIE_BASE_URL):
        return {"ok": False, "id": None, "error": "KIE_API_KEY или KIE_BASE_URL не заданы."}

    headers = {"Authorization": f"Bearer {KIE_API_KEY}", "Content-Type":"application/json"}

    # Если выставлены лимиты в ENV — подставим, но по умолчанию не трогаем.
    if os.getenv("KIE_CREDITS"):
        try: payload.setdefault("credits", int(os.getenv("KIE_CREDITS")))
        except: pass
    if os.getenv("KIE_SECONDS"):
        try: payload.setdefault("seconds", int(os.getenv("KIE_SECONDS")))
        except: pass

    last_err = None
    for path in KIE_FALLBACK_GEN_PATHS:
        url = f"{KIE_BASE_URL}{path}"
        try:
            r = _post_json(url, payload, headers)
        except Exception as e:
            last_err = f"Network error: {e}"
            continue

        if r.status_code in (200, 201, 202):
            try:
                data = r.json() if r.text else {}
            except Exception:
                data = {}
            task_id = _extract_task_id(data)
            result_url = _extract_result_url(data)
            return {"ok": True, "id": task_id or "unknown", "result_url": result_url, "raw": data, "error": None}

        if r.status_code in (401, 403) or "Illegal IP" in (r.text or ""):
            return {"ok": False, "id": None, "error": "Доступ API запрещён: IP Render не в whitelist Kie."}

        if r.status_code == 404:
            last_err = f"API 404 по адресу {url}. Тело: {(r.text or '')[:300]}"
            continue

        last_err = f"API {r.status_code} по адресу {url}. Тело: {(r.text or '')[:300]}"
        break

    return {"ok": False, "id": None, "error": last_err or "Не удалось связаться с API."}

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
        "image_url": image_url,
        "aspect_ratio": "16:9" if aspect == "16:9" else "9:16"
    })

async def poll_and_send_result(ctx: ContextTypes.DEFAULT_TYPE, chat_id: int, task_id: str):
    """Пуллим результат и отправляем его как только появится (мягкий фоллбэк на разные пути)."""
    if not task_id or task_id == "unknown":
        return
    headers = {"Authorization": f"Bearer {KIE_API_KEY}"}
    session = requests.Session()

    RESULT_PATH_CANDIDATES = [
        "/api/v1/tasks/{id}",
        "/api/v1/veo/result/{id}",
        "/api/v1/result/{id}",
    ]

    for attempt in range(45):  # ~9 минут (45 * 12 сек)
        for tmpl in RESULT_PATH_CANDIDATES:
            url = f"{KIE_BASE_URL}{tmpl.format(id=task_id)}"
            try:
                r = session.get(url, headers=headers, timeout=20)
                log.info("HTTP GET %s -> %s", url, r.status_code)
                if r.status_code in (200, 201):
                    try:
                        data = r.json()
                    except Exception:
                        data = {}
                    result_url = _extract_result_url(data)
                    status = (data.get("status") or (data.get("data") or {}).get("status") or "").lower()
                    if result_url:
                        try:
                            await ctx.bot.send_video(chat_id, result_url, caption=f"✅ Готово! task_id: `{task_id}`",
                                                     parse_mode=ParseMode.MARKDOWN)
                        except Exception:
                            await ctx.bot.send_message(chat_id, f"✅ Результат: {result_url}\n(task_id: `{task_id}`)",
                                                       parse_mode=ParseMode.MARKDOWN)
                        return
                    if status in ("failed", "error"):
                        await ctx.bot.send_message(chat_id, f"❌ Генерация не удалась (task_id: `{task_id}`).",
                                                   parse_mode=ParseMode.MARKDOWN)
                        return
            except Exception as e:
                log.warning("Polling error %s: %s", url, e)
        await asyncio.sleep(12)

    await ctx.bot.send_message(chat_id,
        f"⌛ Пока нет ссылки на видео (task_id: `{task_id}`). Оно ещё рендерится — проверь позже в логах KIE.",
        parse_mode=ParseMode.MARKDOWN
    )

# =============== HANDLERS ===============
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    st = state(ctx); st["mode"] = None
    await update.effective_chat.send_message(
        "👋 Привет! Это бот Google Veo3. Выбери режим ниже.",
        reply_markup=MAIN_MENU
    )

async def cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    st = state(ctx); data = q.data

    # выбор формата — живём в том же сообщении
    if data in ("fmt_16x9","fmt_9x16"):
        st["aspect"] = "16:9" if data == "fmt_16x9" else "9:16"
        markup = kb_run_with_format(st["aspect"]) if st.get("last_prompt") else kb_format_only(st["aspect"])
        try:
            await q.edit_message_reply_markup(reply_markup=markup)
        except:
            pass
        return

    if data == "back_menu":
        st["mode"] = None
        await q.edit_message_text("Главное меню:", reply_markup=MAIN_MENU)
        return

    if data == "mode_gen_text":
        st.update({"mode":"gen_text","last_image_url":None,"last_prompt":None})
        await q.edit_message_text("✍️ Пришли идею **или готовый промпт**.\n\nВыбери формат:",
                                  reply_markup=kb_format_only(st["aspect"]))
        return

    if data == "mode_gen_photo":
        st.update({"mode":"gen_photo","last_prompt":None})
        await q.edit_message_text("📸 Пришли **фото** с подписью (краткое описание).\n\nВыбери формат:",
                                  reply_markup=kb_format_only(st["aspect"]))
        return

    if data == "mode_prompt_master":
        st.update({"mode":"prompt_master","last_image_url":None,"last_prompt":None})
        await q.edit_message_text(
            "🧠 Промпт-мастер включён. Опиши идею 1–2 фразами — **начну писать промпт**…",
            reply_markup=None  # Без выбора формата в этом экране
        )
        return

    if data == "mode_chat":
        st["mode"] = "chat"
        await q.edit_message_text(
            "💬 Обычный чат. Пиши сообщения. /exit — выход.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад в меню", callback_data="back_menu")]])
        )
        return

    if data == "mode_gen_text_from_pm":
        st["mode"] = "gen_text"
        await q.edit_message_text("Режим «по тексту». Измени формат ниже или жми «🚀».",
                                  reply_markup=kb_run_with_format(st["aspect"]))
        return

    if data == "mode_gen_photo_from_pm":
        st["mode"] = "gen_photo"
        await q.edit_message_text("Режим «по фото». Отправь изображение и подпись (если нужно).",
                                  reply_markup=kb_run_with_format(st["aspect"]))
        return

    if data == "run":
        if not st.get("last_prompt"):
            await q.answer("Нет подготовленного промпта.", show_alert=True); return
        await q.edit_message_text("🚀 Отправляю задачу в Veo3…")
        res = (submit_veo_job_photo(st["last_image_url"], st["last_prompt"], st["aspect"])
               if st["mode"]=="gen_photo" and st.get("last_image_url")
               else submit_veo_job_text(st["last_prompt"], st["aspect"]))
        if res["ok"]:
            task_id = res.get("id") or "unknown"
            await q.edit_message_text(
                f"✅ Задача отправлена! ID: `{task_id}`\nОбычно рендер 2–5 минут.",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад в меню", callback_data="back_menu")]])
            )
            # фоновый пуллинг результата
            try:
                ctx.application.create_task(poll_and_send_result(ctx, q.message.chat_id, task_id))
            except Exception as e:
                log.warning("Cannot schedule polling: %s", e)
        else:
            msg = res["error"] or "Неизвестная ошибка."
            if "whitelist" in msg or "IP" in msg:
                msg += "\n\n⚙️ Админу: добавьте исходящие IP Render в whitelist Kie."
            await q.edit_message_text(f"❌ Не удалось создать задачу:\n{msg}",
                                      reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад в меню", callback_data="back_menu")]]))
        return

    if data == "faq":
        await q.edit_message_text(
            "📖 FAQ\n• Примеры: https://t.me/bestveo3promts\n• Форматы: 16:9 и 9:16\n"
            "• Рендер обычно 2–5 мин.\n• В кадре без текста/логотипов.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад в меню", callback_data="back_menu")]])
        )
        return

async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    st = state(ctx); text = (update.message.text or "").strip()

    if st["mode"] == "chat":
        try:
            st["chat_history"] = st.get("chat_history", [])[-8:]
            st["chat_history"].append({"role":"user","content": text})
            answer = oai_chat([{"role":"system","content":"Ты дружелюбный ассистент. Коротко и по делу."}]
                              + st["chat_history"], temperature=0.6, max_tokens=500)
            st["chat_history"].append({"role":"assistant","content": answer})
            await update.message.reply_text(answer)
        except Exception as e:
            await update.message.reply_text(f"Ошибка чата: {e}")
        return

    if st["mode"] == "prompt_master":
        working = await update.message.reply_text("⌛ Начинаю писать промпт…")
        try:
            prompt = oai_chat([SYSTEM_PM, {"role":"user","content": text}], temperature=0.7, max_tokens=900)
            st["last_prompt"] = prompt

            await working.edit_text("🧠 Готовый промпт для Veo3:")
            prompt_block = f"<pre>{html_escape(prompt)}</pre>"
            await update.message.reply_html(prompt_block, disable_web_page_preview=True)

            await update.message.reply_text("Выбери дальнейшее действие:", reply_markup=AFTER_PM_ACTIONS)
        except Exception as e:
            await working.edit_text(f"❌ Ошибка при создании промпта: {e}")
        return

    if st["mode"] in (None, "gen_text", "gen_photo"):
        if st["mode"] == "gen_photo" and not st.get("last_image_url"):
            await update.message.reply_text("Нужна фотография. Пришли изображение (с подписью — по желанию).")
            return

        if looks_like_ready_prompt(text):
            st["last_prompt"] = text
            await update.message.reply_text("✅ Принял промпт. Готов к запуску.",
                                            reply_markup=kb_run_with_format(st["aspect"]))
            return

        working = await update.message.reply_text("⌛ Формулирую кинематографический промпт…")
        try:
            prompt = oai_chat([SYSTEM_PM, {"role":"user","content": text}], temperature=0.7, max_tokens=900)
            st["last_prompt"] = prompt
            await working.edit_text("✅ Промпт готов и сохранён. Измени формат ниже или жми «🚀».",
                                    reply_markup=kb_run_with_format(st["aspect"]))
        except Exception as e:
            await working.edit_text(f"❌ Ошибка при подготовке промпта: {e}")
        return

async def on_photo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    st = state(ctx)
    try:
        photo = update.message.photo[-1]
        f = await update.get_bot().get_file(photo.file_id)
        image_url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{f.file_path}"
        st["last_image_url"] = image_url
        caption = (update.message.caption or "").strip()

        if caption:
            working = await update.message.reply_text("📸 Фото получено. ⌛ Формулирую промпт…")
            try:
                prompt = oai_chat([SYSTEM_PM, {"role":"user","content": caption}], temperature=0.7, max_tokens=900)
                st["last_prompt"] = prompt
                await working.edit_text("✅ Фото и промпт готовы. Измени формат ниже или жми «🚀».",
                                        reply_markup=kb_run_with_format(st["aspect"]))
            except Exception as e:
                await working.edit_text(f"❌ Ошибка при подготовке промпта: {e}")
        else:
            st["mode"] = "gen_photo"
            await update.message.reply_text(
                "📸 Фото получено. Напиши короткое **описание сцены** — я доработаю промпт.",
                reply_markup=kb_format_only(st["aspect"])
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

# =============== MAIN ===============
def main():
    if not BOT_TOKEN:
        raise RuntimeError("TELEGRAM_TOKEN (или BOT_TOKEN) не задан.")
    app: Application = ApplicationBuilder().token(BOT_TOKEN).concurrent_updates(True).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("exit",  exit_cmd))

    app.add_handler(CallbackQueryHandler(
        cb, pattern=r"^(mode_.+|fmt_16x9|fmt_9x16|run|back_menu|faq)$"
    ))

    app.add_handler(MessageHandler(filters.PHOTO, on_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    app.add_error_handler(error_handler)

    log.info("Bot started.")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()

# -*- coding: utf-8 -*-
# BEST VEO3 BOT — text & photo generation + Prompt-Master (Webhook edition for Render)
# PTB v20+

import os, json, logging, traceback, requests
from typing import Optional, Dict, Any

from dotenv import load_dotenv
from telegram import (
    Update, InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardRemove
)
from telegram.constants import ParseMode
from telegram.ext import (
    ApplicationBuilder, Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters
)

# ---------------- ENV & LOG ----------------
load_dotenv()

BOT_TOKEN       = os.getenv("TELEGRAM_TOKEN") or os.getenv("BOT_TOKEN") or ""
OPENAI_API_KEY  = os.getenv("OPENAI_API_KEY") or os.getenv("OPENAI_KEY") or ""
KIE_API_KEY     = os.getenv("KIE_API_KEY", "")

# База и путь берём из окружения и НОРМАЛИЗУЕМ
KIE_BASE_URL    = (os.getenv("KIE_BASE_URL") or "https://api.kie.ai").strip().rstrip("/")
_raw_path       = (os.getenv("KIE_GENERATE_PATH") or os.getenv("KIE_GEN_PATH") or "/api/v1/veo/generate").strip()

def _normalize_path(p: str) -> str:
    """Маршрут должен быть вида /api/...  (даже если задали /v1/...)"""
    if not p.startswith("/"):
        p = "/" + p
    # если случайно указали "/v1/..." — добавим префикс /api
    if p.startswith("/v1/"):
        p = "/api" + p
    return p

KIE_GEN_PATH = _normalize_path(_raw_path)

LOG_LEVEL       = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(level=getattr(logging, LOG_LEVEL, logging.INFO),
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("best-veo3")

# ---------- WEBHOOK настройки (Render) ----------
# Render сам выдаёт публичный URL сервиса. Задай его в переменной:
# PUBLIC_URL или RENDER_EXTERNAL_URL (что удобнее). Пример: https://best-veo3-bot.onrender.com
PUBLIC_URL = (os.getenv("PUBLIC_URL") or os.getenv("RENDER_EXTERNAL_URL") or "").strip().rstrip("/")
PORT       = int(os.getenv("PORT", "8000"))

# --------------- UI: KEYBOARDS ---------------
def kb_format_speed(aspect: str, speed: str) -> InlineKeyboardMarkup:
    # speed: fast | quality
    b16  = f"{'✅ ' if aspect=='16:9' else ''}🎬 16:9"
    b916 = f"{'✅ ' if aspect=='9:16' else ''}📱 9:16"
    bfast = f"{'✅ ' if speed=='fast' else ''}⚡ Fast"
    bqual = f"{'✅ ' if speed=='quality' else ''}🎞️ Quality"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(b16,  callback_data="fmt_16x9"),
         InlineKeyboardButton(b916, callback_data="fmt_9x16")],
        [InlineKeyboardButton(bfast, callback_data="spd_fast"),
         InlineKeyboardButton(bqual, callback_data="spd_quality")],
    ])

def kb_run_with_format_speed(aspect: str, speed: str) -> InlineKeyboardMarkup:
    km = kb_format_speed(aspect, speed).inline_keyboard
    km += [
        [InlineKeyboardButton("🚀 Запустить генерацию", callback_data="run")],
        [InlineKeyboardButton("⬅️ Назад в меню", callback_data="back_menu")],
    ]
    return InlineKeyboardMarkup(km)

AFTER_PM_ACTIONS = InlineKeyboardMarkup([
    [InlineKeyboardButton("🎬 Сгенерировать по тексту", callback_data="mode_gen_text_from_pm")],
    [InlineKeyboardButton("🖼️ Сгенерировать по фото",  callback_data="mode_gen_photo_from_pm")],
])

MAIN_MENU = InlineKeyboardMarkup([
    [InlineKeyboardButton("🎬 Сгенерировать видео по тексту", callback_data="mode_gen_text")],
    [InlineKeyboardButton("🖼️ Сгенерировать видео по фото",  callback_data="mode_gen_photo")],
    [InlineKeyboardButton("🧠 Промпт-мастер (ChatGPT)",       callback_data="mode_prompt_master")],
    [InlineKeyboardButton("💬 Обычный чат (ChatGPT)",         callback_data="mode_chat")],
    [InlineKeyboardButton("❓ FAQ", callback_data="faq"),
     InlineKeyboardButton("📚 Канал с промптами", url="https://t.me/bestveo3promts")],
])

# ---------------- STATE ----------------
def state(ctx: ContextTypes.DEFAULT_TYPE) -> Dict[str, Any]:
    if "state" not in ctx.user_data:
        ctx.user_data["state"] = {
            "mode": None,              # gen_text | gen_photo | prompt_master | chat
            "aspect": "16:9",
            "speed": "fast",           # fast | quality
            "last_prompt": None,
            "last_image_url": None,
            "chat_history": []
        }
    return ctx.user_data["state"]

# ---------------- HELPERS ----------------
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

# ---------------- KIE / VEO3 ----------------
def _submit_kie(payload: dict) -> dict:
    """
    Возвращает: {"ok": bool, "id": str|None, "error": str|None}
    """
    if not (KIE_API_KEY and KIE_BASE_URL):
        return {"ok": False, "id": None, "error": "KIE_API_KEY или KIE_BASE_URL не заданы."}

    headers = {"Authorization": f"Bearer {KIE_API_KEY}", "Content-Type":"application/json"}
    url = f"{KIE_BASE_URL}{KIE_GEN_PATH}"
    # Лог для диагностики
    log.info("KIE endpoint: %s", url)
    log.info("Payload: %s", json.dumps(payload, ensure_ascii=False))

    try:
        r = requests.post(url, headers=headers, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"), timeout=40)
        # Kie.ai форматирует тело как JSON с полями code/msg/data
        body_txt = r.text[:500]
        log.info("KIE HTTP %s -> %s", r.status_code, body_txt)

        # HTTP 200 — это и «успех», и «в процессе 1080p» у них; дальше читаем JSON
        data = {}
        try:
            data = r.json()
        except Exception:
            pass

        # Разные ветки
        if r.status_code == 200:
            code = data.get("code", 200)
            if code == 200:
                task_id = (data.get("data") or {}).get("taskId") or data.get("taskId")
                return {"ok": True, "id": task_id or "unknown", "error": None}
            elif code == 400:
                return {"ok": False, "id": None, "error": "1080p сейчас обрабатывается. Повтори запрос через минуту."}
            elif code == 402:
                return {"ok": False, "id": None, "error": "Недостаточно кредитов в Kie.ai. Пополните баланс."}
            else:
                return {"ok": False, "id": None, "error": f"API code {code}: {data.get('msg') or 'Неизвестная ошибка'}"}

        if r.status_code in (401,403):
            # Частая причина — IP белый список
            msg = "Доступ API запрещён. Проверь API-ключ и whitelist исходящих IP Render в панели Kie."
            return {"ok": False, "id": None, "error": msg}

        if r.status_code == 404:
            return {"ok": False, "id": None, "error": "Маршрут не найден (404). Проверь KIE_BASE_URL и KIE_GENERATE_PATH."}

        if r.status_code == 422:
            return {"ok": False, "id": None, "error": f"Параметры не прошли валидацию: {body_txt}"}

        if r.status_code == 429:
            return {"ok": False, "id": None, "error": "Превышен лимит запросов (429). Подожди немного."}

        if r.status_code >= 500:
            return {"ok": False, "id": None, "error": "Сервер Kie.ai временно недоступен. Попробуйте позже."}

        return {"ok": False, "id": None, "error": f"API {r.status_code}: {body_txt}"}

    except Exception as e:
        return {"ok": False, "id": None, "error": f"Network error: {e}"}

def _model_for_speed(speed: str) -> str:
    # veo3 — качественная; veo3_fast — дешёвая/быстрая
    return "veo3_fast" if speed == "fast" else "veo3"

def submit_veo_job_text(prompt: str, aspect: str, speed: str) -> dict:
    payload = {
        "model": _model_for_speed(speed),
        "prompt": prompt,
        "aspect_ratio": "16:9" if aspect == "16:9" else "9:16"
        # можно добавить "enableFallback": True при желании
    }
    return _submit_kie(payload)

def submit_veo_job_photo(image_url: str, prompt: str, aspect: str, speed: str) -> dict:
    payload = {
        "model": _model_for_speed(speed),
        "prompt": prompt,
        "imageUrls": [image_url],
        "aspect_ratio": "16:9" if aspect == "16:9" else "9:16"
    }
    return _submit_kie(payload)

# ---------------- HANDLERS ----------------
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    st = state(ctx); st["mode"] = None
    await update.effective_chat.send_message(
        "👋 Привет! Это бот Veo 3. Выбери режим ниже.",
        reply_markup=MAIN_MENU
    )

async def cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    st = state(ctx); data = q.data

    # выбор формата/скорости
    if data in ("fmt_16x9","fmt_9x16","spd_fast","spd_quality"):
        if data == "fmt_16x9": st["aspect"] = "16:9"
        if data == "fmt_9x16": st["aspect"] = "9:16"
        if data == "spd_fast": st["speed"] = "fast"
        if data == "spd_quality": st["speed"] = "quality"

        markup = (kb_run_with_format_speed(st["aspect"], st["speed"])
                  if st.get("last_prompt") else
                  kb_format_speed(st["aspect"], st["speed"]))
        try:
            await q.edit_message_reply_markup(reply_markup=markup)
        except:
            pass
        return

    # назад
    if data == "back_menu":
        st["mode"] = None
        await q.edit_message_text("Главное меню:", reply_markup=MAIN_MENU)
        return

    # режимы
    if data == "mode_gen_text":
        st.update({"mode":"gen_text","last_image_url":None,"last_prompt":None})
        await q.edit_message_text("✍️ Пришли идею **или готовый промпт**.\n\nВыбери формат/скорость:",
                                  reply_markup=kb_format_speed(st["aspect"], st["speed"]))
        return

    if data == "mode_gen_photo":
        st.update({"mode":"gen_photo","last_prompt":None})
        await q.edit_message_text("📸 Пришли **фото** с подписью (краткое описание).\n\nВыбери формат/скорость:",
                                  reply_markup=kb_format_speed(st["aspect"], st["speed"]))
        return

    if data == "mode_prompt_master":
        st.update({"mode":"prompt_master","last_image_url":None,"last_prompt":None})
        await q.edit_message_text(
            "🧠 Промпт-мастер включён. Опиши идею 1–2 фразами — **начну писать промпт**…"
        )
        return

    if data == "mode_chat":
        st["mode"] = "chat"
        await q.edit_message_text("💬 Обычный чат. Пиши сообщения. /exit — выход.",
                                  reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад в меню", callback_data="back_menu")]]))
        return

    # быстрые действия после PromptMaster
    if data == "mode_gen_text_from_pm":
        st["mode"] = "gen_text"
        await q.edit_message_text("Режим «по тексту». Измени формат/скорость или жми «🚀».",
                                  reply_markup=kb_run_with_format_speed(st["aspect"], st["speed"]))
        return

    if data == "mode_gen_photo_from_pm":
        st["mode"] = "gen_photo"
        await q.edit_message_text("Режим «по фото». Отправь изображение и подпись (если нужно).",
                                  reply_markup=kb_run_with_format_speed(st["aspect"], st["speed"]))
        return

    # запуск генерации
    if data == "run":
        if not st.get("last_prompt"):
            await q.answer("Нет подготовленного промпта.", show_alert=True); return
        await q.edit_message_text("🚀 Отправляю задачу в Veo3…")
        res = (submit_veo_job_photo(st["last_image_url"], st["last_prompt"], st["aspect"], st["speed"])
               if st["mode"]=="gen_photo" and st.get("last_image_url")
               else submit_veo_job_text(st["last_prompt"], st["aspect"], st["speed"]))
        if res["ok"]:
            await q.edit_message_text(
                f"✅ Задача отправлена! ID: `{res['id'] or 'unknown'}`\nОбычно рендер 2–5 минут.",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад в меню", callback_data="back_menu")]])
            )
        else:
            msg = res["error"] or "Неизвестная ошибка."
            if ("whitelist" in msg.lower()) or ("ip" in msg.lower()):
                msg += "\n\n⚙️ Админу: добавьте исходящие IP Render в whitelist Kie (Settings → Scaling → Outbound IPs)."
            await q.edit_message_text(f"❌ Не удалось создать задачу:\n{msg}",
                                      reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад в меню", callback_data="back_menu")]]))
        return

    # FAQ
    if data == "faq":
        await q.edit_message_text(
            "📖 FAQ\n• Примеры: https://t.me/bestveo3promts\n• Форматы: 16:9 и 9:16\n"
            "• Режимы: ⚡ Fast (дешевле/быстрее) и 🎞️ Quality (выше качество)\n"
            "• Рендер обычно 2–5 мин.\n• В кадре без текста/логотипов.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад в меню", callback_data="back_menu")]])
        )
        return

async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    st = state(ctx); text = (update.message.text or "").strip()

    # обычный чат
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

    # Prompt-Master
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

    # Генерация по тексту (или дефолт)
    if st["mode"] in (None, "gen_text", "gen_photo"):
        # если включён режим по фото, но фото ещё нет
        if st["mode"] == "gen_photo" and not st.get("last_image_url"):
            await update.message.reply_text("Нужна фотография. Пришли изображение (с подписью — по желанию).")
            return

        # готовый промпт — просто принимаем
        if looks_like_ready_prompt(text):
            st["last_prompt"] = text
            await update.message.reply_text(
                "✅ Принял промпт. Готов к запуску.",
                reply_markup=kb_run_with_format_speed(st["aspect"], st["speed"])
            )
            return

        # идея — усиливаем и молча сохраняем
        working = await update.message.reply_text("⌛ Формулирую кинематографический промпт…")
        try:
            prompt = oai_chat([SYSTEM_PM, {"role":"user","content": text}], temperature=0.7, max_tokens=900)
            st["last_prompt"] = prompt
            await working.edit_text("✅ Промпт готов и сохранён. Измени формат/скорость ниже или жми «🚀».",
                                    reply_markup=kb_run_with_format_speed(st["aspect"], st["speed"]))
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
                await working.edit_text("✅ Фото и промпт готовы. Измени формат/скорость или жми «🚀».",
                                        reply_markup=kb_run_with_format_speed(st["aspect"], st["speed"]))
            except Exception as e:
                await working.edit_text(f"❌ Ошибка при подготовке промпта: {e}")
        else:
            st["mode"] = "gen_photo"
            await update.message.reply_text(
                "📸 Фото получено. Напиши короткое **описание сцены** — я доработаю промпт.",
                reply_markup=kb_format_speed(st["aspect"], st["speed"])
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

# ---------------- MAIN (WEBHOOK) ----------------
def main():
    if not BOT_TOKEN:
        raise RuntimeError("TELEGRAM_TOKEN (или BOT_TOKEN) не задан.")
    if not PUBLIC_URL:
        raise RuntimeError("PUBLIC_URL (или RENDER_EXTERNAL_URL) не задан. Укажи публичный https URL сервиса Render.")

    app: Application = ApplicationBuilder().token(BOT_TOKEN).concurrent_updates(True).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("exit",  exit_cmd))

    app.add_handler(CallbackQueryHandler(
        cb, pattern=r"^(mode_.+|fmt_16x9|fmt_9x16|spd_fast|spd_quality|run|back_menu|faq)$"
    ))

    app.add_handler(MessageHandler(filters.PHOTO, on_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    app.add_error_handler(error_handler)

    # ВАЖНО: Webhook вместо polling — никакого getUpdates → никаких 409 конфликтов
    hook_path = f"/{BOT_TOKEN}"
    webhook_url = f"{PUBLIC_URL}{hook_path}"
    log.info("Starting webhook on 0.0.0.0:%s path=%s → %s", PORT, hook_path, webhook_url)

    app.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        url_path=BOT_TOKEN,
        webhook_url=webhook_url,
        # drop_pending_updates полезно при рестарте
        drop_pending_updates=True,
    )

if __name__ == "__main__":
    main()

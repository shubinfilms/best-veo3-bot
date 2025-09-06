# -*- coding: utf-8 -*-
# BEST VEO3 BOT — text + photo + Prompt-Master + авто-получение результата из KIE
# Версия: 2025-09-06

import os, json, logging, asyncio, traceback, io
from typing import Dict, Any, Optional, List

import requests
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardRemove, InputFile
from telegram.constants import ParseMode, ChatAction
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler, CallbackQueryHandler,
    ContextTypes, filters
)

# ===================== ENV & LOG =====================
load_dotenv()

TELEGRAM_TOKEN  = os.getenv("TELEGRAM_TOKEN", "")
OPENAI_API_KEY  = os.getenv("OPENAI_API_KEY", "") or os.getenv("OPENAI_KEY", "")
KIE_API_KEY     = os.getenv("KIE_API_KEY", "")
KIE_BASE_URL    = (os.getenv("KIE_BASE_URL") or "https://api.kie.ai").strip().rstrip("/")

# Путь создания задачи (документация KIE: /api/v1/veo/generate)
KIE_GEN_PATH    = (os.getenv("KIE_GEN_PATH") or os.getenv("KIE_GENERATE_PATH") or "/api/v1/veo/generate").strip()
# Путь получения статуса задачи (в KIE встречаются разные маршруты — попробуем несколько)
KIE_GET_PATHS   = [
    (os.getenv("KIE_GET_TASK_PATH") or "/api/v1/common/get_task").strip(),
    "/api/v1/veo/get_task",
    "/api/common/get_task",
]

LOG_LEVEL = (os.getenv("LOG_LEVEL") or "INFO").upper()
logging.basicConfig(level=getattr(logging, LOG_LEVEL, logging.INFO),
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("best-veo3")

def _join_url(base: str, path: str) -> str:
    u = f"{base.rstrip('/')}/{path.lstrip('/')}"
    return u.replace("://", "§§").replace("//", "/").replace("§§", "://")

GEN_URL = _join_url(KIE_BASE_URL, KIE_GEN_PATH)
GET_URLS = [_join_url(KIE_BASE_URL, p) for p in KIE_GET_PATHS]

log.info(f"KIE generate endpoint: {GEN_URL}")
log.info(f"KIE get-task endpoints: {', '.join(GET_URLS)}")

# ===================== UI =====================
MAIN_MENU = InlineKeyboardMarkup([
    [InlineKeyboardButton("🎬 Сгенерировать видео по тексту", callback_data="mode_gen_text")],
    [InlineKeyboardButton("🖼️ Сгенерировать видео по фото",  callback_data="mode_gen_photo")],
    [InlineKeyboardButton("🧠 Промпт-мастер (ChatGPT)",       callback_data="mode_prompt_master")],
    [InlineKeyboardButton("💬 Обычный чат (ChatGPT)",         callback_data="mode_chat")],
    [InlineKeyboardButton("❓ FAQ", callback_data="faq"),
     InlineKeyboardButton("📈 Канал с промптами", url="https://t.me/bestveo3promts")],
])

def kb_formats(aspect: str, show_run: bool=False) -> InlineKeyboardMarkup:
    b16  = f"{'✅ ' if aspect=='16:9' else ''}🎬 16:9"
    b916 = f"{'✅ ' if aspect=='9:16' else ''}📱 9:16"
    rows = [[InlineKeyboardButton(b16, callback_data="fmt_16x9"),
             InlineKeyboardButton(b916, callback_data="fmt_9x16")]]
    if show_run:
        rows.append([InlineKeyboardButton("🚀 Запустить генерацию", callback_data="run")])
        rows.append([InlineKeyboardButton("⬅️ Назад в меню", callback_data="back_menu")])
    return InlineKeyboardMarkup(rows)

# ===================== STATE =====================
def state(ctx: ContextTypes.DEFAULT_TYPE) -> Dict[str, Any]:
    if "state" not in ctx.user_data:
        ctx.user_data["state"] = {
            "mode": None,            # gen_text | gen_photo | prompt_master | chat
            "aspect": "16:9",
            "last_prompt": None,
            "last_image_url": None,
            "chat_history": [],
        }
    return ctx.user_data["state"]

# ===================== Prompt-Master (OpenAI 0.28 API) =====================
def oai_prompt_master(idea_text: str) -> str:
    """
    Делает готовый кинематографичный промпт 500–900 символов, сразу на английском.
    Используем старый openai==0.28.* (ChatCompletion).
    """
    if not OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY не задан.")

    import openai  # из пакета 0.28.*
    openai.api_key = OPENAI_API_KEY

    system = {
        "role": "system",
        "content": (
            "You are a film director and prompt-writer for Google Veo3. "
            "Write a single cinematic prompt in English (500–900 characters), "
            "no follow-up questions. Keep user's idea intact; enrich with optics (mm/anamorphic), "
            "camera motion (push-in, dolly, glide, rack focus), light/palette, rhythm, "
            "micro-details (dust, vapor, lens flare), and audio cues. "
            "No brands/logos/subtitles. No meta talk. Output only the prompt text."
        )
    }
    user = {"role": "user", "content": idea_text}

    resp = openai.ChatCompletion.create(
        model="gpt-4o-mini",
        messages=[system, user],
        temperature=0.7,
        max_tokens=900,
    )
    return resp.choices[0].message["content"].strip()

# ===================== KIE API =====================
def _submit_kie(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Создать задачу. Модель фиксируем на 'veo3_fast'."""
    if not (KIE_API_KEY and KIE_BASE_URL):
        return {"ok": False, "error": "KIE_API_KEY или KIE_BASE_URL не заданы.", "task_id": None}

    payload = dict(payload or {})
    payload["model"] = "veo3_fast"          # ← ЖЁСТКО: всегда fast
    headers = {"Authorization": f"Bearer {KIE_API_KEY}", "Content-Type": "application/json"}

    # короткий лог без секрета
    safe_log = {
        "model": payload.get("model"),
        "aspect_ratio": payload.get("aspect_ratio"),
        "has_image": bool(payload.get("image_url")),
        "prompt_len": len(payload.get("prompt", "")),
    }
    log.info(f"KIE POST {GEN_URL} | payload: {safe_log}")

    try:
        r = requests.post(GEN_URL, headers=headers, data=json.dumps(payload), timeout=60)
    except Exception as e:
        return {"ok": False, "error": f"Network error: {e}", "task_id": None}

    txt = r.text[:500]
    if r.status_code != 200:
        if r.status_code == 402:
            return {"ok": False, "error": "Недостаточно кредитов на KIE.", "task_id": None}
        if r.status_code in (401, 403) or "Illegal IP" in txt:
            return {"ok": False, "error": "Доступ запрещён (ключ/whitelist/IP).", "task_id": None}
        return {"ok": False, "error": f"HTTP {r.status_code}: {txt}", "task_id": None}

    # JSON с разными ключами
    try:
        data = r.json()
    except Exception:
        data = {}

    # если провайдер возвращает код в body
    if isinstance(data, dict) and "code" in data and int(data.get("code")) != 0:
        return {"ok": False, "error": f"KIE code {data.get('code')}: {data.get('msg')}", "task_id": None}

    task_id = data.get("taskid") or data.get("task_id") or data.get("id")
    if not task_id:
        return {"ok": False, "error": f"Не удалось получить task_id: {data}", "task_id": None}
    return {"ok": True, "error": None, "task_id": task_id}

def _extract_status_and_urls(data: Dict[str, Any]) -> (str, List[str]):
    """
    Универсальный парсер ответа get_task.
    Возвращает (status, urls).
    """
    # возможная вложенность
    payload = data.get("data") if isinstance(data, dict) and "data" in data else data
    if not isinstance(payload, dict):
        payload = {}

    # статусы встречаются разные
    status = (
        payload.get("status")
        or payload.get("state")
        or payload.get("taskStatus")
        or payload.get("task_status")
        or ""
    )
    status = str(status).lower()

    # ссылки результата
    urls: List[str] = []
    if "result_urls" in payload:
        ru = payload.get("result_urls")
        if isinstance(ru, list):
            urls = [str(u) for u in ru if u]
        elif isinstance(ru, str):
            urls = [ru]
    for key in ("url", "video_url", "result_url"):
        if payload.get(key):
            urls.append(str(payload[key]))

    # уникализируем
    urls = [u for i, u in enumerate(urls) if u and u not in urls[:i]]
    return status, urls

async def poll_kie_and_send(chat_id: int, task_id: str, ctx: ContextTypes.DEFAULT_TYPE,
                            max_minutes: int = 20, interval_sec: int = 8):
    """
    Пуллим KIE до результата и отправляем видео или ссылку.
    """
    headers = {"Authorization": f"Bearer {KIE_API_KEY}", "Content-Type": "application/json"}
    deadline = asyncio.get_event_loop().time() + max_minutes * 60

    await ctx.bot.send_message(chat_id, "⏳ Жду результат от Veo3…")

    while asyncio.get_event_loop().time() < deadline:
        for url in GET_URLS:
            try:
                r = requests.post(url, headers=headers, data=json.dumps({"taskid": task_id}), timeout=30)
                if r.status_code != 200:
                    continue
                data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
                # если API оборачивает в {code:0,data:{...}}
                if isinstance(data, dict) and "code" in data and int(data.get("code")) != 0:
                    # ошибка со стороны KIE
                    await ctx.bot.send_message(chat_id, f"❌ Ошибка KIE: {data.get('msg')}")
                    return

                status, urls = _extract_status_and_urls(data)
                log.info(f"poll[{task_id}] {url} -> status={status} urls={len(urls)}")

                if status in ("success", "succeed", "finished", "done", "complete", "completed", "ok"):
                    if urls:
                        # Попробуем отправить как видео по URL. Если не удастся — скачиваем и шлём файлом.
                        for u in urls:
                            try:
                                await ctx.bot.send_video(chat_id, u, supports_streaming=True)
                            except Exception:
                                # fallback — скачать и отправить
                                try:
                                    resp = requests.get(u, timeout=60)
                                    bio = io.BytesIO(resp.content)
                                    bio.name = "result.mp4"
                                    await ctx.bot.send_video(chat_id, InputFile(bio), supports_streaming=True)
                                except Exception as e:
                                    await ctx.bot.send_message(chat_id, f"🔗 Результат: {u}\n(не удалось отправить файлом: {e})")
                        return
                    else:
                        await ctx.bot.send_message(chat_id, "✅ Готово, но URL не получен. Проверьте задачу в кабинете KIE.")
                        return

                # продолжаем ждать
            except Exception as e:
                log.warning(f"poll error[{task_id}] {url}: {e}")

        await asyncio.sleep(interval_sec)

    await ctx.bot.send_message(chat_id, "⌛ Время ожидания вышло. Видео ещё рендерится — проверьте позже в журналах KIE.")

# ===================== ВСПОМОГАТЕЛЬНОЕ =====================
def looks_like_ready_prompt(text: str) -> bool:
    if not text:
        return False
    t = text.strip()
    if t.startswith("{") and "}" in t:
        return True
    score = 0
    for kw in ("fps", "anamorphic", "85mm", "35mm", "lens", "DOF", "bokeh", "rack focus",
               "color palette", "lighting", "camera", "glide", "push-in", "tone", "sound",
               "\"shot\"", "\"scene\"", "\"audio\"", "cinematic"):
        if kw in t.lower():
            score += 1
    return score >= 3 or len(t) > 400

async def typing(ctx: ContextTypes.DEFAULT_TYPE, chat_id: int, sec: float = 3.0):
    try:
        await ctx.bot.send_chat_action(chat_id, ChatAction.TYPING)
        await asyncio.sleep(sec)
    except:  # noqa
        pass

# ===================== HANDLERS =====================
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    st = state(ctx); st["mode"] = None
    await update.effective_chat.send_message(
        "👋 Привет! Это бот Google Veo 3. Выберите режим и формат кадра.",
        reply_markup=MAIN_MENU
    )
    await update.effective_chat.send_message("Выбери формат:", reply_markup=kb_formats(st["aspect"]))

async def cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    st = state(ctx)
    data = q.data

    if data == "back_menu":
        st["mode"] = None
        st["last_prompt"] = None
        st["last_image_url"] = None
        await q.edit_message_text("Главное меню:", reply_markup=MAIN_MENU)
        return

    if data.startswith("fmt_"):
        st["aspect"] = "16:9" if data == "fmt_16x9" else "9:16"
        show_run = bool(st.get("last_prompt"))
        try:
            await q.edit_message_reply_markup(reply_markup=kb_formats(st["aspect"], show_run=show_run))
        except Exception:
            # если сообщение без клавы — перепишем текст
            await q.edit_message_text(f"✅ Формат: {st['aspect']}", reply_markup=kb_formats(st["aspect"], show_run=show_run))
        return

    if data == "mode_gen_text":
        st["mode"] = "gen_text"
        st["last_image_url"] = None
        st["last_prompt"] = None
        await q.edit_message_text(
            "✍️ Пришлите **идею** или **готовый промпт**.\n"
            "Если это идея, Prompt-Master автоматически оформит её в кинематографический промпт.",
            reply_markup=kb_formats(st["aspect"])
        )
        return

    if data == "mode_gen_photo":
        st["mode"] = "gen_photo"
        st["last_prompt"] = None
        await q.edit_message_text(
            "📸 Пришлите **фото** (можно с подписью). По подписи я сделаю кинематографический промпт.",
            reply_markup=kb_formats(st["aspect"])
        )
        return

    if data == "mode_prompt_master":
        st["mode"] = "prompt_master"
        st["last_prompt"] = None
        await q.edit_message_text(
            "🧠 Режим «Промпт-мастер» активирован.\n"
            "Отправьте **идею** 1–2 фразами — я сразу верну готовый **англоязычный кинематографичный** промпт (500–900 симв.).",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад в меню", callback_data="back_menu")]])
        )
        return

    if data == "mode_chat":
        st["mode"] = "chat"
        await q.edit_message_text("💬 Обычный чат включён. /exit — выход.", reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("⬅️ Назад в меню", callback_data="back_menu")]
        ]))
        return

    if data == "faq":
        await q.edit_message_text(
            "📖 FAQ\n• Форматы: 16:9 и 9:16\n• Рендер обычно 2–5 мин.\n• Без логотипов/текста в кадре.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⬅️ Назад в меню", callback_data="back_menu"),
                 InlineKeyboardButton("📈 Канал с промптами", url="https://t.me/bestveo3promts")]
            ])
        )
        return

    if data == "run":
        if not st.get("last_prompt"):
            await q.answer("Сначала подготовьте промпт.", show_alert=True)
            return
        await q.edit_message_text("🚀 Отправляю задачу в Veo3 Fast…")

        payload = {
            "prompt": st["last_prompt"],
            "aspect_ratio": "16:9" if st["aspect"] == "16:9" else "9:16",
        }
        if st["mode"] == "gen_photo" and st.get("last_image_url"):
            payload["image_url"] = st["last_image_url"]

        res = _submit_kie(payload)
        if not res["ok"]:
            await q.edit_message_text(f"❌ Ошибка запуска генерации: {res['error']}",
                                      reply_markup=kb_formats(st["aspect"], show_run=True))
            return

        task_id = res["task_id"]
        await q.edit_message_text(
            f"✅ Задача создана. ID: `{task_id}`\nОбычно рендер 2–5 мин.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=kb_formats(st["aspect"], show_run=True)
        )

        # фоновый опрос и отправка результата
        asyncio.create_task(poll_kie_and_send(update.effective_chat.id, task_id, ctx))
        return

async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    st = state(ctx)
    text = (update.message.text or "").strip()
    chat_id = update.effective_chat.id

    # Обычный чат
    if st["mode"] == "chat":
        await typing(ctx, chat_id)
        await update.message.reply_text("👋 (чат ассистента сейчас минимален, упор на генерацию видео)")
        return

    # Prompt-Master
    if st["mode"] == "prompt_master":
        try:
            await typing(ctx, chat_id, 2.5)
            prompt = oai_prompt_master(text)
            st["last_prompt"] = prompt
            await update.message.reply_text(
                f"🧠 Готовый промпт для Veo3:\n<pre>{prompt}</pre>",
                parse_mode=ParseMode.HTML,
                reply_markup=kb_formats(st["aspect"], show_run=True)
            )
        except Exception as e:
            await update.message.reply_text(
                "❌ Prompt-Master error:\n" + str(e),
                disable_web_page_preview=True
            )
        return

    # Генерация по тексту/фото
    if st["mode"] in (None, "gen_text", "gen_photo"):
        if st["mode"] == "gen_photo" and not st.get("last_image_url"):
            await update.message.reply_text("Нужно фото. Пришлите изображение (подпись — по желанию).")
            return

        # если прислали готовый промпт — не переделываем
        if looks_like_ready_prompt(text):
            st["last_prompt"] = text
            await update.message.reply_text("✅ Принял промпт. Готов к запуску.",
                                            reply_markup=kb_formats(st["aspect"], show_run=True))
            return

        # иначе делаем кинопромпт через PM
        try:
            await typing(ctx, chat_id, 2.5)
            prompt = oai_prompt_master(text)
            st["last_prompt"] = prompt
            await update.message.reply_text(
                "✅ Промпт сформирован и сохранён. Нажмите «🚀 Запустить генерацию».",
                reply_markup=kb_formats(st["aspect"], show_run=True)
            )
        except Exception as e:
            await update.message.reply_text("❌ Не удалось сформировать промпт:\n" + str(e))
        return

async def on_photo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    st = state(ctx)
    chat_id = update.effective_chat.id
    try:
        photo = update.message.photo[-1]
        f = await ctx.bot.get_file(photo.file_id)
        image_url = f"https://api.telegram.org/file/bot{TELEGRAM_TOKEN}/{f.file_path}"
        st["last_image_url"] = image_url

        caption = (update.message.caption or "").strip()
        if caption:
            await typing(ctx, chat_id)
            try:
                prompt = oai_prompt_master(caption)
                st["last_prompt"] = prompt
                await update.message.reply_text(
                    "📸 Фото и промпт готовы. Нажмите «🚀 Запустить генерацию».",
                    reply_markup=kb_formats(st["aspect"], show_run=True)
                )
            except Exception as e:
                await update.message.reply_text("❌ Ошибка при подготовке промпта: " + str(e))
        else:
            st["mode"] = "gen_photo"
            await update.message.reply_text(
                "📸 Фото получено. Напишите короткое описание сцены — я оформлю промпт.",
                reply_markup=kb_formats(st["aspect"], show_run=False)
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

# ===================== MAIN =====================
def main():
    if not TELEGRAM_TOKEN:
        raise RuntimeError("TELEGRAM_TOKEN не задан.")
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).concurrent_updates(True).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("exit", exit_cmd))

    app.add_handler(CallbackQueryHandler(
        cb, pattern=r"^(mode_gen_text|mode_gen_photo|mode_prompt_master|mode_chat|faq|back_menu|fmt_16x9|fmt_9x16|run)$"
    ))
    app.add_handler(MessageHandler(filters.PHOTO, on_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_error_handler(error_handler)

    log.info("Bot started.")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()

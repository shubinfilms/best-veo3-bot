# -*- coding: utf-8 -*-
# BEST VEO3 BOT — text & photo generation + Prompt-Master + Fast/Quality
# PTB v20+

import os, json, time, logging, traceback, requests, tempfile
from typing import Optional, Dict, Any, Tuple, Iterable

from dotenv import load_dotenv
from telegram import (
    Update, InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardRemove
)
from telegram.constants import ParseMode
from telegram.ext import (
    ApplicationBuilder, Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters
)

# ========== ENV & LOG ==========
load_dotenv()
BOT_TOKEN       = os.getenv("TELEGRAM_TOKEN") or os.getenv("BOT_TOKEN") or ""
OPENAI_API_KEY  = os.getenv("OPENAI_API_KEY") or os.getenv("OPENAI_KEY") or ""
KIE_API_KEY     = os.getenv("KIE_API_KEY", "")
KIE_BASE_URL    = (os.getenv("KIE_BASE_URL") or "https://api.kie.ai").rstrip("/")
LOG_LEVEL       = os.getenv("LOG_LEVEL", "INFO").upper()

logging.basicConfig(level=getattr(logging, LOG_LEVEL, logging.INFO),
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("best-veo3")

# --------- CONSTANTS (Kie endpoints & timings) ---------
KIE_GEN_URL      = f"{KIE_BASE_URL}/api/v1/veo/generate"
KIE_STATUS_URL   = f"{KIE_BASE_URL}/api/v1/veo/record-info"
KIE_GET1080P_URL = f"{KIE_BASE_URL}/api/v1/veo/get-1080p-video"

POLL_INTERVAL_SEC      = 8          # опрос статуса
URL_CHECK_INTERVAL_SEC = 10         # проверка появления ссылок после success
TIMER_EDIT_STEP_SEC    = 3          # обновление «⏳ … N сек» каждые 3 сек
WAIT_MAX_SEC           = 30 * 60    # общий таймаут ожидания (30 мин)

# ========== UI: KEYBOARDS ==========
MAIN_MENU = InlineKeyboardMarkup([
    [InlineKeyboardButton("🎬 Сгенерировать видео по тексту", callback_data="mode_gen_text")],
    [InlineKeyboardButton("🖼️ Сгенерировать видео по фото",  callback_data="mode_gen_photo")],
    [InlineKeyboardButton("🧠 Промпт-мастер (ChatGPT)",       callback_data="mode_prompt_master")],
    [InlineKeyboardButton("💬 Обычный чат (ChatGPT)",         callback_data="mode_chat")],
    [InlineKeyboardButton("❓ FAQ", callback_data="faq"),
     InlineKeyboardButton("📚 Канал с промптами", url="https://t.me/bestveo3promts")],
])

def kb_format(aspect: str) -> InlineKeyboardMarkup:
    b16  = f"{'✅ ' if aspect=='16:9' else ''}🎬 16:9"
    b916 = f"{'✅ ' if aspect=='9:16' else ''}📱 9:16"
    return InlineKeyboardMarkup([[InlineKeyboardButton(b16, callback_data="fmt_16x9"),
                                  InlineKeyboardButton(b916, callback_data="fmt_9x16")]])

def kb_tier(tier: str) -> InlineKeyboardMarkup:
    # tier: 'quality' | 'fast'
    q = f"{'✅ ' if tier=='quality' else ''}💎 Quality"
    f = f"{'✅ ' if tier=='fast' else ''}⚡ Fast"
    return InlineKeyboardMarkup([[InlineKeyboardButton(q, callback_data="tier_quality"),
                                  InlineKeyboardButton(f, callback_data="tier_fast")]])

def kb_run_panel(aspect: str, tier: str) -> InlineKeyboardMarkup:
    # формат + модель + запуск
    b16  = f"{'✅ ' if aspect=='16:9' else ''}🎬 16:9"
    b916 = f"{'✅ ' if aspect=='9:16' else ''}📱 9:16"
    q = f"{'✅ ' if tier=='quality' else ''}💎 Quality"
    f = f"{'✅ ' if tier=='fast' else ''}⚡ Fast"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(b16,  callback_data="fmt_16x9"),
         InlineKeyboardButton(b916, callback_data="fmt_9x16")],
        [InlineKeyboardButton(q, callback_data="tier_quality"),
         InlineKeyboardButton(f, callback_data="tier_fast")],
        [InlineKeyboardButton("🚀 Запустить генерацию", callback_data="run")],
        [InlineKeyboardButton("⬅️ В меню", callback_data="back_menu")],
    ])

AFTER_PM_ACTIONS = InlineKeyboardMarkup([
    [InlineKeyboardButton("🎬 Сгенерировать по тексту", callback_data="mode_gen_text_from_pm")],
    [InlineKeyboardButton("🖼️ Сгенерировать по фото",  callback_data="mode_gen_photo_from_pm")],
])

ONLY_BACK_KB = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ В меню", callback_data="back_menu")]])

# ========== STATE ==========
def state(ctx: ContextTypes.DEFAULT_TYPE) -> Dict[str, Any]:
    if "state" not in ctx.user_data:
        ctx.user_data["state"] = {
            "mode": None,              # gen_text | gen_photo | prompt_master | chat
            "aspect": "16:9",
            "tier": "fast",            # 'fast' (дешевле) по умолчанию
            "last_prompt": None,
            "last_image_url": None,
            "chat_history": []
        }
    return ctx.user_data["state"]

# ========== HELPERS ==========
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

# ========== KIE / VEO3: HTTP ==========
def _http_post_json(url: str, payload: dict, timeout=40) -> Tuple[int, dict]:
    headers = {"Authorization": f"Bearer {KIE_API_KEY}", "Content-Type":"application/json"}
    r = requests.post(url, headers=headers, data=json.dumps(payload), timeout=timeout)
    try:
        data = r.json()
    except Exception:
        data = {"raw": r.text}
    return r.status_code, data

def _http_get_json(url: str, params: dict = None, timeout=40) -> Tuple[int, dict]:
    headers = {"Authorization": f"Bearer {KIE_API_KEY}"}
    r = requests.get(url, headers=headers, params=params or {}, timeout=timeout)
    try:
        data = r.json()
    except Exception:
        data = {"raw": r.text}
    return r.status_code, data

def _pick_first_video_url(result_urls_field: str) -> Optional[str]:
    try:
        arr = json.loads(result_urls_field or "[]")
        for u in arr:
            if isinstance(u, str) and (u.endswith(".mp4") or u.endswith(".mov") or ".m3u8" in u):
                return u
    except Exception:
        pass
    return None

def _download_to_temp(url: str) -> str:
    resp = requests.get(url, stream=True, timeout=180)
    resp.raise_for_status()
    suffix = ".mp4" if ".m3u8" not in url else ".ts"
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as f:
        for chunk in resp.iter_content(chunk_size=1_048_576):
            if chunk:
                f.write(chunk)
        return f.name

# ========== KIE / VEO3: GENERATE & POLL ==========
def kie_generate(prompt: str, aspect: str, tier: str,
                 image_url: Optional[str] = None,
                 seed: Optional[int] = None,
                 enable_fallback: bool = True) -> Tuple[Optional[str], Optional[str]]:
    """
    Возвращает (task_id, error). Поля по оф. доке:
    POST /api/v1/veo/generate
      model: 'veo3' | 'veo3_fast'
      prompt: str
      aspect: '16:9' | '9:16'
      image_url: str (optional)
      callback: str (optional) — не используем здесь
      seed: int 10000..99999 (optional)
      enableFallback: bool (optional; для 16:9)
    """
    model = "veo3_fast" if tier == "fast" else "veo3"
    payload = {
        "model": model,
        "prompt": prompt,
        "aspect": aspect
    }
    if image_url:
        payload["image_url"] = image_url
    if isinstance(seed, int) and 10000 <= seed <= 99999:
        payload["seed"] = seed
    # fallback работает только на 16:9; включаем умно
    if enable_fallback and aspect == "16:9":
        payload["enableFallback"] = True

    status, data = _http_post_json(KIE_GEN_URL, payload)
    if status == 200 and data.get("code") == 200:
        task_id = (data.get("data") or {}).get("taskId") or data.get("taskId")
        if task_id:
            return str(task_id), None
        return None, "taskId не найден в ответе API"
    # дружественные сообщения
    msg = data.get("msg") or f"HTTP {status}"
    return None, msg

def kie_status(task_id: str) -> Tuple[Optional[dict], Optional[str]]:
    status, data = _http_get_json(KIE_STATUS_URL, params={"taskId": task_id})
    if status == 200 and data.get("code") == 200:
        return data.get("data"), None
    return None, data.get("msg") or f"HTTP {status}"

# ========== HANDLERS ==========
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    st = state(ctx); st["mode"] = None
    await update.effective_chat.send_message(
        "👋 Привет! Это бот Google Veo3. Выбери режим ниже.",
        reply_markup=MAIN_MENU
    )

async def cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    st = state(ctx); data = q.data

    # формат
    if data in ("fmt_16x9","fmt_9x16"):
        st["aspect"] = "16:9" if data == "fmt_16x9" else "9:16"
        # оставляем существующую панель; если уже есть промпт — показываем панель запуска
        try:
            await q.edit_message_reply_markup(reply_markup=kb_run_panel(st["aspect"], st["tier"])
                                              if st.get("last_prompt") else kb_format(st["aspect"]))
        except: pass
        return

    # модель
    if data in ("tier_quality","tier_fast"):
        st["tier"] = "quality" if data == "tier_quality" else "fast"
        try:
            await q.edit_message_reply_markup(reply_markup=kb_run_panel(st["aspect"], st["tier"])
                                              if st.get("last_prompt") else kb_tier(st["tier"]))
        except: pass
        return

    # назад
    if data == "back_menu":
        st["mode"] = None
        await q.edit_message_text("Главное меню:", reply_markup=MAIN_MENU)
        return

    # режимы
    if data == "mode_gen_text":
        st.update({"mode":"gen_text","last_image_url":None,"last_prompt":None})
        await q.edit_message_text(
            "✍️ Пришли идею **или готовый промпт**.\n\nВыбери формат и модель:",
            reply_markup=kb_run_panel(st["aspect"], st["tier"])
        )
        return

    if data == "mode_gen_photo":
        st.update({"mode":"gen_photo","last_prompt":None})
        await q.edit_message_text(
            "📸 Пришли **фото** с подписью (краткое описание).\n\nВыбери формат и модель:",
            reply_markup=kb_run_panel(st["aspect"], st["tier"])
        )
        return

    if data == "mode_prompt_master":
        st.update({"mode":"prompt_master","last_image_url":None,"last_prompt":None})
        # В PM НЕ спрашиваем формат — просто пишем промпт
        await q.edit_message_text(
            "🧠 Промпт-мастер включён. Опиши идею 1–2 фразами — **начну писать промпт**…",
            reply_markup=ONLY_BACK_KB
        )
        return

    if data == "mode_chat":
        st["mode"] = "chat"
        await q.edit_message_text("💬 Обычный чат. Пиши сообщения. /exit — выход.", reply_markup=ONLY_BACK_KB)
        return

    if data == "mode_gen_text_from_pm":
        st["mode"] = "gen_text"
        await q.edit_message_text("Режим «по тексту». Измени формат/модель ниже или жми «🚀».",
                                  reply_markup=kb_run_panel(st["aspect"], st["tier"]))
        return

    if data == "mode_gen_photo_from_pm":
        st["mode"] = "gen_photo"
        await q.edit_message_text("Режим «по фото». Отправь изображение и подпись (если нужно).",
                                  reply_markup=kb_run_panel(st["aspect"], st["tier"]))
        return

    # запуск
    if data == "run":
        if not st.get("last_prompt"):
            await q.answer("Нет подготовленного промпта.", show_alert=True); return
        await q.edit_message_text("🚀 Отправляю задачу в Veo3…")
        await _run_generation_pipeline(update.effective_chat.id, ctx, st)
        return

    # FAQ
    if data == "faq":
        await q.edit_message_text(
            "📖 FAQ\n"
            "• Примеры: https://t.me/bestveo3promts\n"
            "• Форматы: 16:9 и 9:16\n"
            "• Модели: 💎 Quality (дороже), ⚡ Fast (дешевле)\n"
            "• Рендер обычно 2–5 мин.\n"
            "• В кадре без текста/логотипов.",
            reply_markup=ONLY_BACK_KB
        )
        return

async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    st = state(ctx); text = (update.message.text or "").strip()

    # CHAT
    if st["mode"] == "chat":
        try:
            st["chat_history"] = st.get("chat_history", [])[-8:]
            st["chat_history"].append({"role":"user","content": text})
            ans = oai_chat([{"role":"system","content":"Ты дружелюбный ассистент. Коротко и по делу."}]
                           + st["chat_history"], temperature=0.6, max_tokens=500)
            st["chat_history"].append({"role":"assistant","content": ans})
            await update.message.reply_text(ans)
        except Exception as e:
            await update.message.reply_text(f"Ошибка чата: {e}")
        return

    # PROMPT-MASTER
    if st["mode"] == "prompt_master":
        working = await update.message.reply_text("⌛ Начинаю писать промпт…")
        try:
            prompt = oai_chat([SYSTEM_PM, {"role":"user","content": text}], temperature=0.7, max_tokens=900)
            st["last_prompt"] = prompt
            await working.edit_text("🧠 Готовый промпт для Veo3:")
            await update.message.reply_html(f"<pre>{html_escape(prompt)}</pre>",
                                            disable_web_page_preview=True)
            await update.message.reply_text("Выбери дальнейшее действие:", reply_markup=AFTER_PM_ACTIONS)
        except Exception as e:
            await working.edit_text(f"❌ Ошибка при создании промпта: {e}")
        return

    # GEN BY TEXT / DEFAULT
    if st["mode"] in (None, "gen_text", "gen_photo"):
        if st["mode"] == "gen_photo" and not st.get("last_image_url"):
            await update.message.reply_text("Нужна фотография. Пришли изображение (с подписью — по желанию).")
            return

        if looks_like_ready_prompt(text):
            st["last_prompt"] = text
            await update.message.reply_text("✅ Принял промпт. Готов к запуску.",
                                            reply_markup=kb_run_panel(st["aspect"], st["tier"]))
            return

        working = await update.message.reply_text("⌛ Формулирую кинематографический промпт…")
        try:
            prompt = oai_chat([SYSTEM_PM, {"role":"user","content": text}], temperature=0.7, max_tokens=900)
            st["last_prompt"] = prompt
            # здесь НЕ присылаем весь промпт — только подтверждение
            await working.edit_text("✅ Промпт готов и сохранён. Измени формат/модель ниже или жми «🚀».",
                                    reply_markup=kb_run_panel(st["aspect"], st["tier"]))
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
                await working.edit_text("✅ Фото и промпт готовы. Измени формат/модель ниже или жми «🚀».",
                                        reply_markup=kb_run_panel(st["aspect"], st["tier"]))
            except Exception as e:
                await working.edit_text(f"❌ Ошибка при подготовке промпта: {e}")
        else:
            st["mode"] = "gen_photo"
            await update.message.reply_text(
                "📸 Фото получено. Напиши короткое **описание сцены** — я доработаю промпт.",
                reply_markup=kb_run_panel(st["aspect"], st["tier"])
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

# ========== CORE PIPELINE: submit → poll → download → send ==========
async def _run_generation_pipeline(chat_id: int, ctx: ContextTypes.DEFAULT_TYPE, st: Dict[str, Any]):
    prompt = st.get("last_prompt")
    aspect = st.get("aspect", "16:9")
    tier   = st.get("tier", "fast")
    image_url = st.get("last_image_url")

    # 1) создать задачу
    task_msg = await ctx.bot.send_message(chat_id, "⏳ Генерация идёт…")
    t0 = time.time()
    shown = 0

    def _tick_msg():
        nonlocal shown
        sec = int(time.time() - t0)
        if sec - shown >= TIMER_EDIT_STEP_SEC:
            shown = sec
            try:
                ctx.application.create_task(
                    ctx.bot.edit_message_text(chat_id=chat_id, message_id=task_msg.id,
                                              text=f"⏳ Генерация идёт… *{sec} сек*",
                                              parse_mode=ParseMode.MARKDOWN)
                )
            except Exception:
                pass

    task_id, err = kie_generate(prompt=prompt, aspect=aspect, tier=tier,
                                image_url=image_url, enable_fallback=True)
    if err or not task_id:
        try:
            await ctx.bot.edit_message_text(chat_id=chat_id, message_id=task_msg.id,
                                            text=f"❌ Не удалось создать задачу: {err}")
        except Exception:
            await ctx.bot.send_message(chat_id, f"❌ Не удалось создать задачу: {err}")
        return

    await ctx.bot.send_message(chat_id, f"🧾 Задача создана. ID: `{task_id}`", parse_mode=ParseMode.MARKDOWN)

    # 2) ждём successFlag=1
    deadline = time.time() + WAIT_MAX_SEC
    last_flag = None
    while time.time() < deadline:
        info, serr = kie_status(task_id)
        _tick_msg()
        if serr:
            await _sleep(POLL_INTERVAL_SEC)
            continue
        flag = (info or {}).get("successFlag")
        last_flag = flag
        if flag == 0:
            await _sleep(POLL_INTERVAL_SEC)
            continue
        if flag in (2, 3):
            await ctx.bot.edit_message_text(chat_id=chat_id, message_id=task_msg.id,
                                            text="❌ Генерация не удалась на стороне провайдера.")
            return
        if flag == 1:
            break

    if last_flag != 1:
        await ctx.bot.edit_message_text(chat_id=chat_id, message_id=task_msg.id,
                                        text="⛔️ Таймаут ожидания результата. Повторите попытку.")
        return

    # 3) ждём появления resultUrls
    url = None
    while time.time() < deadline:
        info, serr = kie_status(task_id)
        _tick_msg()
        if serr:
            await _sleep(URL_CHECK_INTERVAL_SEC)
            continue
        url = _pick_first_video_url((info or {}).get("resultUrls") or "[]")
        if url:
            break
        await _sleep(URL_CHECK_INTERVAL_SEC)

    if not url:
        await ctx.bot.edit_message_text(chat_id=chat_id, message_id=task_msg.id,
                                        text="⚠️ Видео готово, но ссылка пока недоступна. Попробуйте позже.")
        return

    # 4) качаем и отправляем видеофайл
    try:
        path = _download_to_temp(url)
        try:
            await ctx.bot.edit_message_text(chat_id=chat_id, message_id=task_msg.id,
                                            text="📥 Загружаю видео в Telegram…")
        except Exception:
            pass
        with open(path, "rb") as f:
            await ctx.bot.send_video(chat_id, f, caption=f"✅ Готово! Формат: *{aspect}*, модель: *{'Fast' if tier=='fast' else 'Quality'}*",
                                     parse_mode=ParseMode.MARKDOWN, supports_streaming=True)
    except Exception as e:
        try:
            await ctx.bot.edit_message_text(chat_id=chat_id, message_id=task_msg.id,
                                            text=f"✅ Видео готово, но не удалось загрузить файл ({e}).")
        except Exception:
            pass
        await ctx.bot.send_message(chat_id, "Попробуйте ещё раз. Если повторится — проверим размер файла/лимиты Telegram.")
    finally:
        try:
            if 'path' in locals() and os.path.exists(path):
                os.remove(path)
        except Exception:
            pass

async def _sleep(sec: int):
    # маленькая обёртка под awaitable sleep
    import asyncio
    await asyncio.sleep(sec)

# ========== MAIN ==========
def main():
    if not BOT_TOKEN:
        raise RuntimeError("TELEGRAM_TOKEN (или BOT_TOKEN) не задан.")
    app: Application = ApplicationBuilder().token(BOT_TOKEN).concurrent_updates(True).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("exit",  exit_cmd))

    app.add_handler(CallbackQueryHandler(
        cb, pattern=r"^(mode_.+|fmt_16x9|fmt_9x16|tier_quality|tier_fast|run|back_menu|faq)$"
    ))

    app.add_handler(MessageHandler(filters.PHOTO, on_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    app.add_error_handler(error_handler)

    log.info("Bot started.")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()

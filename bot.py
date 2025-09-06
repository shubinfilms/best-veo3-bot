# -*- coding: utf-8 -*-
"""
BEST VEO3 bot — Webhook + выбор Fast/Quality + 16:9/9:16 + отправка в KIE API
PTB v20+

ENV (Render → Environment):
  TELEGRAM_TOKEN=xxx
  PUBLIC_URL=https://best-veo3-bot-xxxx.onrender.com
  KIE_API_KEY=xxx
  KIE_BASE_URL=https://api.kie.ai
  KIE_GENERATE_PATH=/api/v1/veo/generate
  BOT_MODEL=veo3_fast   # либо 'veo3' (Quality)
"""

import os
import json
import logging
from typing import Dict, Any, List, Optional

import requests
from telegram import (
    Update, InlineKeyboardMarkup, InlineKeyboardButton, MessageEntity, InputFile
)
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ContextTypes, filters
)

# ---------------- LOG ----------------
logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("best-veo3")

# ---------------- ENV ----------------
TOKEN        = os.getenv("TELEGRAM_TOKEN", "")
PUBLIC_URL   = os.getenv("PUBLIC_URL", "").rstrip("/")
KIE_API_KEY  = os.getenv("KIE_API_KEY", "")
KIE_BASE_URL = os.getenv("KIE_BASE_URL", "https://api.kie.ai").rstrip("/")
RAW_PATH     = os.getenv("KIE_GENERATE_PATH", "/api/v1/veo/generate").strip()
DEFAULT_MODEL= os.getenv("BOT_MODEL", "veo3_fast").strip()  # 'veo3' or 'veo3_fast'

PORT         = int(os.getenv("PORT", "5000"))

if not TOKEN or not PUBLIC_URL:
    log.error("TELEGRAM_TOKEN или PUBLIC_URL не заданы — бот не поднимется.")

# --------- helpers: normalize API path ----------
def _normalize_path(p: str) -> str:
    """Гарантируем корректный маршрут вида /api/.... (на случай опечаток)"""
    p = p.strip()
    if not p.startswith("/"):
        p = "/" + p
    if p.startswith("/v1/") or p.startswith("/veo/") or p.startswith("/api/"):
        # если пользователь дал '/v1/...' — добавим '/api' спереди
        if p.startswith("/v1/") or p.startswith("/veo/"):
            p = "/api" + p
    return p

API_PATH = _normalize_path(RAW_PATH)
KIE_URL  = f"{KIE_BASE_URL}{API_PATH}"

# --------- per-user state ----------
# format: { user_id: {"aspect": "16:9"|"9:16", "model": "veo3_fast"|"veo3"} }
STATE: Dict[int, Dict[str, str]] = {}

def get_state(user_id: int) -> Dict[str, str]:
    st = STATE.setdefault(user_id, {"aspect": "16:9", "model": DEFAULT_MODEL})
    return st

# --------- keyboards ----------
def kb_aspect(current: str) -> InlineKeyboardMarkup:
    b16 = "✅ 16:9" if current == "16:9" else "16:9"
    b916 = "✅ 9:16" if current == "9:16" else "9:16"
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(b16, callback_data="aspect:16:9"),
          InlineKeyboardButton(b916, callback_data="aspect:9:16")]]
    )

def kb_model(current: str) -> InlineKeyboardMarkup:
    # veo3 = Quality; veo3_fast = Fast
    fast = "✅ Fast (veo3_fast)" if current == "veo3_fast" else "Fast (veo3_fast)"
    qual = "✅ Quality (veo3)" if current == "veo3" else "Quality (veo3)"
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(fast, callback_data="model:veo3_fast"),
          InlineKeyboardButton(qual, callback_data="model:veo3")]]
    )

def kb_main(current_aspect: str, current_model: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(f"Формат: {current_aspect}", callback_data="open:aspect")],
            [InlineKeyboardButton(
                "Режим: Fast ⚡" if current_model == "veo3_fast" else "Режим: Quality 🎬",
                callback_data="open:model"
            )],
        ]
    )

# --------- KIE call ----------
def send_to_kie(prompt: str, aspect: str, model: str,
                image_urls: Optional[List[str]] = None,
                seed: Optional[int] = None,
                enable_fallback: bool = False) -> Dict[str, Any]:
    """
    Возвращает JSON KIE. Поднимаем читабельные ошибки.
    """
    payload: Dict[str, Any] = {
        "prompt": prompt,
        "model": model,                  # 'veo3' (quality) или 'veo3_fast' (fast)
        "aspectRatio": aspect,           # "16:9" | "9:16" (у них camelCase)
        "enableFallback": bool(enable_fallback),
    }
    if image_urls:
        payload["imageUrls"] = image_urls
    if seed is not None:
        payload["seed"] = int(seed)

    headers = {
        "Authorization": f"Bearer {KIE_API_KEY}",
        "Content-Type": "application/json",
    }

    log.info("KIE POST %s | payload: %s", KIE_URL, json.dumps(payload, ensure_ascii=False))
    r = requests.post(KIE_URL, headers=headers, json=payload, timeout=60)
    txt = r.text
    log.info("KIE %s -> %s", r.status_code, txt[:500])

    # Попробуем распарсить тело даже при не-200
    try:
        data = r.json()
    except Exception:
        data = {"code": r.status_code, "msg": txt}

    # Нормализуем известные ошибки
    if r.status_code == 401:
        raise RuntimeError("API 401: неверный или отсутствует KIE_API_KEY.")
    if r.status_code == 402:
        raise RuntimeError("API 402: недостаточно кредитов на KIE.")
    if r.status_code == 404:
        raise RuntimeError("API 404: эндпойнт не найден (проверь KIE_BASE_URL и KIE_GENERATE_PATH).")
    if r.status_code >= 500:
        raise RuntimeError(f"API {r.status_code}: серверная ошибка KIE.")

    # В их контракте «успех» бывает внутри JSON: {"code":200, data:{taskId:...}}
    code = data.get("code", r.status_code)
    if code != 200:
        msg = data.get("msg") or data.get("message") or "Ошибка генерации"
        raise RuntimeError(f"API code {code}: {msg}")

    return data

# --------- handlers ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    st = get_state(u.id)
    text = (
        "👋 Привет! Пришли видео-идею (промпт) или фото с подписью — "
        "а я отправлю задачу в VEO3.\n\n"
        "Сначала проверь параметры ниже:"
    )
    await update.message.reply_text(
        text,
        reply_markup=kb_main(st["aspect"], st["model"])
    )

async def open_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Открываем выбор аспектов/модели"""
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    st = get_state(uid)

    _, what = q.data.split(":", 1)
    if what == "aspect":
        await q.edit_message_text(
            "Выбери формат:",
            reply_markup=kb_aspect(st["aspect"])
        )
    elif what == "model":
        await q.edit_message_text(
            "Выбери модель:",
            reply_markup=kb_model(st["model"])
        )

async def set_aspect(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    _, val = q.data.split(":", 1)  # "aspect:16:9" -> val="16:9"
    get_state(uid)["aspect"] = val
    st = get_state(uid)
    await q.edit_message_text(
        f"Формат установлен: {val}\nТеперь пришли промпт.",
        reply_markup=kb_main(st["aspect"], st["model"])
    )

async def set_model(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    _, val = q.data.split(":", 1)  # "model:veo3_fast"
    get_state(uid)["model"] = val
    st = get_state(uid)
    lab = "Fast ⚡ (veo3_fast)" if val == "veo3_fast" else "Quality 🎬 (veo3)"
    await q.edit_message_text(
        f"Режим установлен: {lab}\nТеперь пришли промпт.",
        reply_markup=kb_main(st["aspect"], st["model"])
    )

async def menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    st = get_state(uid)
    await update.message.reply_text(
        "Текущие параметры:",
        reply_markup=kb_main(st["aspect"], st["model"])
    )

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Любой текст — считаем промптом и шлем в KIE"""
    uid = update.effective_user.id
    st = get_state(uid)
    prompt = update.message.text.strip()
    if not prompt:
        return

    await update.message.reply_text(
        f"🚀 Отправляю задачу...\n"
        f"Формат: {st['aspect']} • Режим: {'Fast' if st['model']=='veo3_fast' else 'Quality'}"
    )
    try:
        data = send_to_kie(prompt=prompt, aspect=st["aspect"], model=st["model"])
        task_id = (data.get("data") or {}).get("taskId") or "unknown"
        await update.message.reply_text(
            f"✅ Задача отправлена! ID: `{task_id}`\n"
            f"Обычно рендер занимает 2–5 минут.",
            parse_mode="Markdown"
        )
    except Exception as e:
        await update.message.reply_text(f"❌ Не удалось создать задачу:\n{e}")

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Фото + подпись — пытаемся дернуть file URL телеги и дать как imageUrls"""
    uid = update.effective_user.id
    st = get_state(uid)

    caption = update.message.caption or ""
    if not caption.strip():
        await update.message.reply_text("Добавь подпись к фото — это будет промпт.")
        return

    # Берём самую большую версию фото
    photo = update.message.photo[-1]
    f = await context.bot.get_file(photo.file_id)
    # Прямой URL к файлу телеги
    tg_file_url = f"https://api.telegram.org/file/bot{TOKEN}/{f.file_path}"

    await update.message.reply_text(
        f"🚀 Отправляю (img2vid)...\n"
        f"Формат: {st['aspect']} • Режим: {'Fast' if st['model']=='veo3_fast' else 'Quality'}"
    )
    try:
        data = send_to_kie(
            prompt=caption.strip(),
            aspect=st["aspect"],
            model=st["model"],
            image_urls=[tg_file_url]
        )
        task_id = (data.get("data") or {}).get("taskId") or "unknown"
        await update.message.reply_text(
            f"✅ Задача отправлена! ID: `{task_id}`\n"
            f"Обычно рендер занимает 2–5 минут.",
            parse_mode="Markdown"
        )
    except Exception as e:
        await update.message.reply_text(f"❌ Не удалось создать задачу:\n{e}")

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "/start — меню параметров\n"
        "/menu — показать текущие параметры\n"
        "Пришли текст — запущу текст→видео\n"
        "Пришли фото с подписью — запущу image→видео"
    )

# --------- main / webhook ----------
def main():
    app = Application.builder().token(TOKEN).build()

    # команды
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("menu", menu))
    app.add_handler(CommandHandler("help", help_cmd))

    # выбор в инлайн-кнопках
    app.add_handler(CallbackQueryHandler(open_panel, pattern=r"^open:(aspect|model)$"))
    app.add_handler(CallbackQueryHandler(set_aspect, pattern=r"^aspect:(16:9|9:16)$"))
    app.add_handler(CallbackQueryHandler(set_model,  pattern=r"^model:(veo3_fast|veo3)$"))

    # контент
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    # запускаем как Webhook (Render Web Service)
    app.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        url_path=TOKEN,
        webhook_url=f"{PUBLIC_URL}/{TOKEN}",
    )

if __name__ == "__main__":
    main()

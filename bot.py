# -*- coding: utf-8 -*-
# SHUBIN AI VIDEO — Veo3 Fast + ChatGPT (prompt-master & chat)

import os, json, time, tempfile, requests, traceback
from typing import Optional, Tuple, Any, Iterable
from dotenv import load_dotenv
import telebot
from telebot import types

# ============ ENV ============
load_dotenv()
TOKEN = os.getenv("TELEGRAM_TOKEN")
KIE_API_KEY = os.getenv("KIE_API_KEY")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")  # опционально, но нужен для ChatGPT
PROMPTS_CHANNEL_URL = os.getenv("PROMPTS_CHANNEL_URL", "https://t.me/bestveo3promts")

if not TOKEN:
    raise RuntimeError("TELEGRAM_TOKEN не найден в .env")
if not KIE_API_KEY:
    raise RuntimeError("KIE_API_KEY не найден в .env")

# ============ BOT / STATE ============
bot = telebot.TeleBot(TOKEN, parse_mode="Markdown")
USERS_FILE = "users.json"
users = set(json.load(open(USERS_FILE, "r", encoding="utf-8"))) if os.path.exists(USERS_FILE) else set()
STATE = {}  # chat_id -> {phase, prompt, ratio, mode}

# ============ KIE API ============
BASE = "https://api.kie.ai"
HDRS = {"Authorization": f"Bearer {KIE_API_KEY}", "Content-Type": "application/json"}
MODEL = "veo3_fast"

WAIT_MAX = 30 * 60            # общий лимит ожидания, сек
POLL_INTERVAL = 7             # опрос статуса, сек
TIMER_EDIT_STEP = 3           # как часто обновлять «⏳ ... N сек»
URL_CHECK_INTERVAL = 8        # частота проверки появления resultUrls

# ============ OpenAI ============
try:
    from openai import OpenAI
    client = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None
except Exception:
    client = None

def _choose_model() -> str:
    # Можно заменить на gpt-5-mini, если хочешь дешевле
    return "gpt-5"

def _chat_completion(messages: list[dict]) -> str:
    """
    Вызов OpenAI (с фоллбэком на Responses API).
    Важно: модель принимает только temperature=1. input — список сообщений.
    """
    if not client:
        raise RuntimeError("Нет OPENAI_API_KEY в .env")
    model = _choose_model()
    try:
        # классическое chat.completions
        r = client.chat.completions.create(
            model=model,
            messages=messages,
            temperature=1,   # фикс: поддерживается только 1 у новых моделей
        )
        return (r.choices[0].message.content or "").strip()
    except Exception as e1:
        # фолбэк на Responses API
        try:
            r = client.responses.create(
                model=model,
                input=messages  # фикс: просто список сообщений
            )
            if hasattr(r, "output_text") and r.output_text:
                return r.output_text.strip()
            return ""
        except Exception as e2:
            raise RuntimeError(f"OpenAI error: {e1} | fallback: {e2}")

# ============ сеть / ретраи ============
def _with_retries(fn, tries=5, delay=2, backoff=2):
    last_err = None
    d = delay
    for _ in range(tries):
        try:
            return fn()
        except Exception as e:
            last_err = e
            time.sleep(d)
            d *= backoff
    if last_err:
        raise last_err

def _post_json(url: str, payload: dict, timeout=60) -> dict:
    return _with_retries(lambda: {"status": (r := requests.post(url, headers=HDRS, json=payload, timeout=timeout)).status_code,
                                  "json": r.json()})

def _get_json(url: str, params: dict, timeout=40) -> dict:
    return _with_retries(lambda: {"status": (r := requests.get(url, headers=HDRS, params=params, timeout=timeout)).status_code,
                                  "json": r.json()})

def _download_to_temp(url: str, tries: int = 5) -> str:
    last = None
    for attempt in range(tries):
        try:
            with requests.get(url, stream=True, timeout=180) as resp:
                resp.raise_for_status()
                suffix = ".mp4" if ".m3u8" not in url else ".ts"
                f = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
                with f as fh:
                    for chunk in resp.iter_content(chunk_size=1_048_576):
                        if chunk:
                            fh.write(chunk)
                return f.name
        except Exception as e:
            last = e
            time.sleep(2 + attempt * 2)
    raise last if last else RuntimeError("download failed")

# ============ KIE endpoints ============
def kie_generate(prompt: str, ratio: str, enable_fallback: bool = True) -> Tuple[Optional[str], Optional[str]]:
    try:
        res = _post_json(f"{BASE}/api/v1/veo/generate", {
            "prompt": prompt,
            "model": MODEL,
            "aspectRatio": ratio,
            "enableFallback": enable_fallback
        })
        data = res.get("json") or {}
        if res.get("status") == 200 and data.get("code") == 200:
            task_id = (
                (data.get("data") or {}).get("taskId")
                or data.get("taskId")
                or (data.get("data") or {}).get("id")
                or (data.get("data") or {}).get("task_id")
            )
            if task_id:
                return str(task_id), None
            return None, "taskId не найден в ответе API"
        return None, data.get("msg") or f"HTTP {res.get('status')}"
    except Exception as e:
        return None, str(e)

def kie_status_raw(task_id: str) -> Tuple[Optional[dict], Optional[str]]:
    try:
        res = _get_json(f"{BASE}/api/v1/veo/record-info", params={"taskId": task_id})
        data = res.get("json") or {}
        if res.get("status") == 200 and data.get("code") == 200:
            return data.get("data"), None
        return None, data.get("msg") or f"HTTP {res.get('status')}"
    except Exception as e:
        return None, str(e)

# ============ парсинг ответов ============
def _iter_values(obj: Any) -> Iterable[Any]:
    if isinstance(obj, dict):
        for v in obj.values():
            yield from _iter_values(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _iter_values(v)
    else:
        yield obj

def _find_success_flag(data: dict) -> Optional[int]:
    def _walk(d: Any) -> Optional[int]:
        if isinstance(d, dict):
            for k, v in d.items():
                lk = str(k).lower()
                if lk in ("successflag", "success_flag", "flag"):
                    try:
                        iv = int(v)
                        if iv in (0, 1, 2, 3):
                            return iv
                    except Exception:
                        pass
                if isinstance(v, (dict, list)):
                    ans = _walk(v)
                    if ans is not None:
                        return ans
        elif isinstance(d, list):
            for it in d:
                ans = _walk(it)
                if ans is not None:
                    return ans
        return None
    return _walk(data)

def _extract_video_urls(data: dict) -> list:
    urls = []
    for v in _iter_values(data):
        if isinstance(v, str):
            s = v.strip()
            if s.startswith("[") and s.endswith("]"):
                try:
                    arr = json.loads(s)
                    if isinstance(arr, list):
                        for u in arr:
                            if isinstance(u, str) and ("http" in u) and (u.endswith(".mp4") or u.endswith(".mov") or ".m3u8" in u):
                                urls.append(u)
                except Exception:
                    pass
            if ("http" in s) and (s.endswith(".mp4") or s.endswith(".mov") or ".m3u8" in s):
                urls.append(s)
    # уникализируем
    seen, out = set(), []
    for u in urls:
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out

# ============ UI ============
def _menu_kb() -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(types.InlineKeyboardButton("🎬 Сгенерировать видео по тексту", callback_data="go_text"))
    kb.add(types.InlineKeyboardButton("📸 Сгенерировать видео по фото (скоро)", callback_data="photo_soon"))
    kb.add(types.InlineKeyboardButton("✍️ Промпт-мастер (ChatGPT)", callback_data="prompt_master"))
    kb.add(types.InlineKeyboardButton("💬 Обычный чат (ChatGPT)", callback_data="free_chat"))
    kb.add(types.InlineKeyboardButton("💎 Купить генерации (скоро)", callback_data="buy_soon"))
    kb.add(types.InlineKeyboardButton("❓ FAQ", callback_data="faq"))
    kb.add(types.InlineKeyboardButton("💡 Канал с промптами", url=PROMPTS_CHANNEL_URL))
    return kb

def _after_success_kb() -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        types.InlineKeyboardButton("🎬 Сгенерировать ещё", callback_data="go_text"),
        types.InlineKeyboardButton("💡 Вдохновиться промптами", url=PROMPTS_CHANNEL_URL),
    )
    return kb

def main_menu(chat_id: int):
    bot.send_message(
        chat_id,
        "👋 Привет! Добро пожаловать в *SHUBIN AI VIDEO*.\n"
        f"С нами уже *{len(users)}* пользователей.\n\n"
        f"💡 Вдохновляйся примерами и находи готовые промпты в нашем канале 👉 [Канал с промптами]({PROMPTS_CHANNEL_URL})",
        reply_markup=_menu_kb(),
        disable_web_page_preview=True,
    )

@bot.message_handler(commands=["start", "menu"])
def start_cmd(m):
    users.add(m.from_user.id)
    json.dump(list(users), open(USERS_FILE, "w", encoding="utf-8"))
    STATE[m.chat.id] = {"phase": None, "mode": None}
    main_menu(m.chat.id)

@bot.callback_query_handler(func=lambda c: True)
def on_cb(c):
    cid, data = c.message.chat.id, c.data
    st = STATE.get(cid) or {}
    if data == "go_text":
        STATE[cid] = {"phase": "await_prompt", "mode": None}
        bot.answer_callback_query(c.id)
        bot.send_message(cid, "✍️ Напиши промпт (описание видео одной фразой или несколькими).")
    elif data == "photo_soon":
        bot.answer_callback_query(c.id)
        bot.send_message(cid, "📸 Режим по фото появится позже.")
    elif data == "prompt_master":
        bot.answer_callback_query(c.id)
        STATE[cid] = {"phase": "chat", "mode": "master"}
        bot.send_message(cid, "🧠 *Промпт-мастер*: опиши идею — верну идеальный промпт для Veo3.\n`/exit` — выход.", parse_mode="Markdown")
    elif data == "free_chat":
        bot.answer_callback_query(c.id)
        STATE[cid] = {"phase": "chat", "mode": "chat"}
        bot.send_message(cid, "💬 *Режим обычного чата*. Пиши сообщения. `/exit` — выход.", parse_mode="Markdown")
    elif data == "buy_soon":
        bot.answer_callback_query(c.id)
        bot.send_message(cid, "💎 Скоро добавим оплату Stars и картой.")
    elif data == "faq":
        bot.answer_callback_query(c.id)
        bot.send_message(cid,
            "❓ *FAQ*\n"
            "• Где брать идеи? В нашем канале с примерами: " + PROMPTS_CHANNEL_URL + "\n"
            "• Форматы: 16:9 и 9:16 (вертикаль).\n"
            "• Видео приходит сюда готовым файлом.\n"
            "• Если долго нет видео — просто запусти ещё раз (иногда ссылка появляется с задержкой).",
            disable_web_page_preview=True)
    elif data in ("ratio_16_9", "ratio_9_16"):
        if st.get("phase") != "await_ratio":
            return
        ratio = "16:9" if data == "ratio_16_9" else "9:16"
        st.update({"ratio": ratio, "phase": "ready"})
        STATE[cid] = st
        try:
            bot.edit_message_text(f"✅ Выбран формат: *{ratio}*.\nНажми «🚀 Запустить генерацию».",
                                  chat_id=cid, message_id=c.message.id, parse_mode="Markdown")
        except Exception:
            pass
        kb = types.InlineKeyboardMarkup()
        kb.add(types.InlineKeyboardButton("🚀 Запустить генерацию", callback_data="run_generation"))
        bot.send_message(cid, "Готово!", reply_markup=kb)
    elif data == "run_generation":
        if st.get("phase") != "ready":
            bot.answer_callback_query(c.id, "Сначала выбери формат.")
            return
        bot.answer_callback_query(c.id)
        _run_generation(cid, st["prompt"], st["ratio"])

@bot.message_handler(commands=["exit"])
def exit_mode(m):
    STATE[m.chat.id] = {"phase": None, "mode": None}
    bot.send_message(m.chat.id, "Вышел из текущего режима. Открываю меню.")
    main_menu(m.chat.id)

# ——— ввод промпта для видео
@bot.message_handler(func=lambda m: (STATE.get(m.chat.id) or {}).get("phase") == "await_prompt", content_types=["text"])
def on_prompt(m):
    prompt = (m.text or "").strip()
    STATE[m.chat.id] = {"phase": "await_ratio", "prompt": prompt, "mode": None}
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(types.InlineKeyboardButton("🎞 16:9", callback_data="ratio_16_9"),
           types.InlineKeyboardButton("📱 9:16", callback_data="ratio_9_16"))
    bot.send_message(m.chat.id, "🎚 Отлично! Выбери формат и запускай генерацию.", reply_markup=kb)
    bot.send_message(m.chat.id, f"✅ Принял промпт:\n«{prompt}»")

# ——— ChatGPT режимы
@bot.message_handler(func=lambda m: (STATE.get(m.chat.id) or {}).get("phase") == "chat", content_types=["text"])
def chat_modes(m):
    mode = (STATE.get(m.chat.id) or {}).get("mode")
    user_text = (m.text or "").strip()
    try:
        if mode == "master":
            sys = {
                "role": "system",
                "content": (
                    "Ты эксперт по написанию чётких, кинематографичных промптов для Google Veo 3. "
                    "Возвращай только сам промпт на английском языке, без лишних комментариев. "
                    "Подчёркивай кинодвижение, свет, оптику, композицию, стиль, без текста в кадре."
                )
            }
            messages = [sys, {"role": "user", "content": user_text}]
            out = _chat_completion(messages)
            bot.send_message(m.chat.id, f"📝 Готовый промпт для *Veo3*:\n```\n{out}\n```", parse_mode="Markdown")
        else:
            # обычный чат
            sys = {"role": "system", "content": "Ты дружелюбный помощник."}
            messages = [sys, {"role": "user", "content": user_text}]
            out = _chat_completion(messages)
            bot.send_message(m.chat.id, out or "…")
    except Exception as e:
        bot.send_message(m.chat.id, f"❌ Ошибка ChatGPT: {e}")

# ============ CORE Veo3 ============
def _run_generation(chat_id: int, prompt: str, ratio: str):
    t0 = time.time()
    timer_msg = bot.send_message(chat_id, "⏳ Генерация идёт…")
    shown_sec = 0

    def tick():
        nonlocal shown_sec
        sec = int(time.time() - t0)
        if sec - shown_sec >= TIMER_EDIT_STEP:
            shown_sec = sec
            try:
                bot.edit_message_text(f"⏳ Генерация идёт… *{sec} сек*", chat_id=chat_id,
                                      message_id=timer_msg.id, parse_mode="Markdown")
            except Exception:
                pass

    # 1) создать задачу
    task_id, err = kie_generate(prompt=prompt, ratio=ratio, enable_fallback=True)
    if err or not task_id:
        try:
            bot.edit_message_text(f"❌ Не удалось создать задачу: {err}", chat_id=chat_id, message_id=timer_msg.id)
        except Exception:
            bot.send_message(chat_id, f"❌ Не удалось создать задачу: {err}")
        return
    bot.send_message(chat_id, "🧾 Задача создана.")

    # 2) ждём статус и появление ссылок
    deadline = time.time() + WAIT_MAX
    urls: list[str] = []
    last_flag = None

    while time.time() < deadline:
        info, serr = kie_status_raw(task_id)
        tick()
        if serr:
            time.sleep(POLL_INTERVAL)
            continue

        flag = _find_success_flag(info or {})
        last_flag = flag

        if flag in (2, 3):
            try:
                bot.edit_message_text("❌ Генерация не удалась на стороне провайдера.",
                                      chat_id=chat_id, message_id=timer_msg.id)
            except Exception:
                bot.send_message(chat_id, "❌ Генерация не удалась на стороне провайдера.")
            return

        urls = _extract_video_urls(info or {})
        if urls:
            break

        time.sleep(URL_CHECK_INTERVAL)

    if not urls:
        try:
            bot.edit_message_text("⚠️ Видео готово, но ссылка пока недоступна. Попробуйте позже.",
                                  chat_id=chat_id, message_id=timer_msg.id)
        except Exception:
            bot.send_message(chat_id, "⚠️ Видео готово, но ссылка пока недоступна. Попробуйте позже.")
        return

    # 3) скачиваем и отправляем файл
    video_url = urls[0]
    try:
        path = _download_to_temp(video_url)
        try:
            bot.edit_message_text("📥 Загружаю видео в Telegram…", chat_id=chat_id, message_id=timer_msg.id)
        except Exception:
            pass

        with open(path, "rb") as f:
            bot.send_video(
                chat_id, f,
                caption=f"✅ Готово! Формат: *{ratio}*",
                parse_mode="Markdown",
                supports_streaming=True,
                reply_markup=_after_success_kb()
            )
        # мягкое напоминание про канал
        bot.send_message(chat_id, f"🔥 Хочешь больше идей промптов? Подписывайся: {PROMPTS_CHANNEL_URL}",
                         disable_web_page_preview=True)
    except Exception as e:
        try:
            bot.edit_message_text(f"✅ Видео готово, но не удалось загрузить файл (сеть/лимиты).",
                                  chat_id=chat_id, message_id=timer_msg.id)
        except Exception:
            pass
        bot.send_message(chat_id, "Попробуй ещё раз. Если повторится — проверим размер файла/лимиты Telegram.")
    finally:
        try:
            if 'path' in locals() and os.path.exists(path):
                os.remove(path)
        except Exception:
            pass

# ============ Fallback ============
@bot.message_handler(content_types=["text", "photo", "document", "video", "sticker", "audio", "voice"])
def fallback(m):
    # Если в чате активирован режим, а пришло что-то не текст — просто напомним
    st = STATE.get(m.chat.id) or {}
    if st.get("phase") == "chat" and m.content_type != "text":
        bot.send_message(m.chat.id, "Пожалуйста, напиши текстовое сообщение. `/exit` — выход.")
        return
    bot.send_message(m.chat.id, "Открой меню: /menu")

# ============ RUN ============
if __name__ == "__main__":
    print("✅ Бот запущен. Жду сообщения…")
    # фикс вылета по таймауту
    bot.polling(none_stop=True, long_polling_timeout=60)

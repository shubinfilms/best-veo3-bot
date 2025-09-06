import os
import logging
import requests
import asyncio

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, CallbackQueryHandler, ConversationHandler, filters, ContextTypes

from dotenv import load_dotenv

# Загружаем переменные окружения из файла .env (если файл присутствует)
load_dotenv()

# Получаем необходимые токены/ключи из окружения
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
KIE_API_KEY = os.getenv("KIE_API_KEY")
KIE_BASE_URL = os.getenv("KIE_BASE_URL", "https://api.kie.ai")
KIE_GEN_PATH = os.getenv("KIE_GEN_PATH", "/api/v1/veo/generate")

# Формируем полный URL API для генерации видео
KIE_GENERATE_URL = KIE_BASE_URL.rstrip("/") + KIE_GEN_PATH
# URL для проверки статуса задачи (получение результата)
KIE_STATUS_URL = KIE_BASE_URL.rstrip("/") + "/api/v1/veo/record-info"

# Настраиваем логирование
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Определяем константы состояний для диалогов (ConversationHandler)
# Для генерации видео по тексту:
T_PROMPT, T_RATIO, T_CONFIRM = range(3)
# Для генерации видео по фото:
P_PHOTO, P_PROMPT, P_RATIO, P_CONFIRM = range(4)

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Обработчик команды /start. Отправляет главное меню с кнопками.
    """
    # Очищаем сохраненные данные пользователя (режимы и т.д.)
    context.user_data.clear()
    # Формируем клавиатуру главного меню
    keyboard = [
        [InlineKeyboardButton("🎥 Сгенерировать видео по тексту", callback_data="gen_text")],
        [InlineKeyboardButton("🖼️ Сгенерировать видео по фото", callback_data="gen_photo")],
        [InlineKeyboardButton("💭 Промпт-мастер (ChatGPT)", callback_data="mode_prompt")],
        [InlineKeyboardButton("💬 Обычный чат (ChatGPT)", callback_data="mode_chat")],
        [InlineKeyboardButton("❓ FAQ", callback_data="faq"), InlineKeyboardButton("📈 Канал с промптами", url="https://t.me/your_channel")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    # Отправляем сообщение с меню (используем разметку Markdown для эмодзи, поэтому parse_mode="Markdown")
    await update.message.reply_text("🏠 *Главное меню:*", reply_markup=reply_markup, parse_mode="Markdown")

async def faq_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Обработчик кнопки FAQ. Отправляет сообщение с часто задаваемыми вопросами.
    """
    # Подтверждаем нажатие кнопки, чтобы убрать «часики» на кнопке
    await update.callback_query.answer()
    faq_text = (
        "❓ *FAQ:*\n"
        "- *Что делает этот бот?* Генерирует короткие AI-видео (5–8 сек) с помощью модели Google Veo3 (через API сервиса KIE.ai).\n"
        "- *Как пользоваться?* Выберите режим генерации: либо отправьте текстовое описание сцены, либо фото. Бот создаст видео на основе вашего запроса.\n"
        "- *Сколько времени занимает генерация?* Около нескольких минут. Бот сообщит о прогрессе и пришлёт видео, когда оно будет готово.\n"
        "- *Что за режим Промпт-мастер?* В этом режиме бот (ChatGPT) поможет вам составить или улучшить текстовый промпт (описание) для генерации видео.\n"
        "- *Можно ли просто пообщаться?* Да, в режиме Обычный чат бот отвечает на любые вопросы как ChatGPT.\n"
        "\nℹ️ *Совет:* Во время генерации видео вы можете вернуться в меню с помощью кнопки «Назад в меню». Для нового запроса используйте меню команд."
    )
    await update.callback_query.message.reply_text(faq_text, parse_mode="Markdown")

async def enter_prompt_mode(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Вход в режим «Промпт-мастер». Настраивает режим и отправляет приветственное сообщение с кнопкой выхода.
    """
    await update.callback_query.answer()
    # Устанавливаем режим «prompt»
    context.user_data["mode"] = "prompt"
    # Инициализируем историю сообщений для этого режима с системным сообщением (роль «system» для ChatGPT)
    context.user_data["prompt_history"] = [
        {"role": "system", "content": "Ты – эксперт по созданию подробных описаний (промптов) для генерации видео с помощью ИИ (Google Veo3). Помогаешь пользователю улучшить или придумать креативный промпт на основе его идей. Отвечай четко, по делу, на том же языке, на котором задан вопрос."}
    ]
    # Кнопка для выхода обратно в меню
    back_button = InlineKeyboardMarkup([[InlineKeyboardButton("↩️ Назад в меню", callback_data="back_to_menu")]])
    msg = (
        "💡 *Режим «Промпт-мастер» активирован.* Теперь вы можете отправить идею или черновой промпт, а бот (ChatGPT) поможет сформулировать из него отличный запрос для видео.\n"
        "_Когда захотите вернуться, нажмите кнопку ниже «Назад в меню»._"
    )
    sent_msg = await update.callback_query.message.reply_text(msg, reply_markup=back_button, parse_mode="Markdown")
    # Сохраняем ID отправленного сообщения (чтобы при выходе убрать кнопку, если нужно)
    context.user_data["mode_msg_id"] = sent_msg.message_id

async def enter_chat_mode(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Вход в режим обычного чата с ChatGPT.
    """
    await update.callback_query.answer()
    context.user_data["mode"] = "chat"
    # Инициализируем историю сообщений для режима обычного чата
    context.user_data["chat_history"] = [
        {"role": "system", "content": "Ты — дружелюбный и полезный ассистент."}
    ]
    back_button = InlineKeyboardMarkup([[InlineKeyboardButton("↩️ Назад в меню", callback_data="back_to_menu")]])
    msg = (
        "🤖 *Режим обычного чата активирован.* Вы можете общаться с ботом (ChatGPT), задавать вопросы, бот будет отвечать как ChatGPT.\n"
        "_Для возврата в меню нажмите кнопку «Назад в меню» ниже._"
    )
    sent_msg = await update.callback_query.message.reply_text(msg, reply_markup=back_button, parse_mode="Markdown")
    context.user_data["mode_msg_id"] = sent_msg.message_id

async def back_to_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    Обработчик кнопки «Назад в меню». Выход из текущего режима/диалога и показ главного меню.
    """
    await update.callback_query.answer()
    # Очищаем данные пользователя (сбрасываем режим/состояние)
    context.user_data.clear()
    # Убираем клавиатуру у последнего сообщения (если оно было с кнопкой)
    try:
        await update.callback_query.edit_message_reply_markup(reply_markup=None)
    except Exception as e:
        logger.debug(f"Failed to edit message on back: {e}")
    # Отправляем снова главное меню
    keyboard = [
        [InlineKeyboardButton("🎥 Сгенерировать видео по тексту", callback_data="gen_text")],
        [InlineKeyboardButton("🖼️ Сгенерировать видео по фото", callback_data="gen_photo")],
        [InlineKeyboardButton("💭 Промпт-мастер (ChatGPT)", callback_data="mode_prompt")],
        [InlineKeyboardButton("💬 Обычный чат (ChatGPT)", callback_data="mode_chat")],
        [InlineKeyboardButton("❓ FAQ", callback_data="faq"), InlineKeyboardButton("📈 Канал с промптами", url="https://t.me/your_channel")]
    ]
    await update.callback_query.message.reply_text("🏠 *Главное меню:*", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    return ConversationHandler.END

async def cancel_conversation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    Обработчик команды /cancel для отмены диалога и возврата в меню.
    """
    context.user_data.clear()
    await update.message.reply_text("❌ Отменено. Возвращаемся в главное меню...")
    # Показываем главное меню
    await start_command(update, context)
    return ConversationHandler.END

async def prompt_text_step(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    (Не вызывается напрямую) Заглушка для шага ввода текста в режиме генерации по тексту.
    """
    return T_PROMPT

async def prompt_photo_step(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    (Не вызывается напрямую) Заглушка для шага отправки фото в режиме генерации по фото.
    """
    return P_PHOTO

async def gen_text_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    Точка входа в диалог генерации видео по тексту. Запрашивает у пользователя текстовый промпт.
    """
    await update.callback_query.answer()
    await update.callback_query.message.reply_text(
        "✏️ *Пришлите описание видео.*\n_Напишите текст-промпт — описания сцены, персонажей, окружения и действий._\n\nℹ️ Когда закончите, вы можете отправить команду /cancel для отмены.",
        parse_mode="Markdown"
    )
    return T_PROMPT

async def receive_text_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    Обрабатывает введённый пользователем текстовый промпт для генерации видео.
    """
    user_text = update.message.text.strip()
    if not user_text:
        await update.message.reply_text("⚠️ Описание не должно быть пустым. Введите текст ещё раз.")
        return T_PROMPT
    # Сохраняем промпт
    context.user_data["prompt_text"] = user_text
    # Предлагаем выбрать соотношение сторон видео
    ratio_keyboard = [
        [InlineKeyboardButton("16:9 (горизонтальное)", callback_data="ratio_16_9")],
        [InlineKeyboardButton("9:16 (вертикальное)", callback_data="ratio_9_16")],
        [InlineKeyboardButton("↩️ Назад в меню", callback_data="back_to_menu")]
    ]
    await update.message.reply_text("🖼️ *Выберите формат видео* (соотношение сторон):", reply_markup=InlineKeyboardMarkup(ratio_keyboard), parse_mode="Markdown")
    return T_RATIO

async def gen_photo_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    Точка входа в диалог генерации видео по фото. Запрашивает у пользователя изображение.
    """
    await update.callback_query.answer()
    await update.callback_query.message.reply_text(
        "🖼️ *Пришлите изображение* для генерации видео.\nВы можете также добавить подпись к фото с описанием сцены (необязательно).\n\nℹ️ Отправьте команду /cancel для отмены.",
        parse_mode="Markdown"
    )
    return P_PHOTO

async def receive_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    Обрабатывает полученное изображение от пользователя (в режиме генерации по фото).
    Если у фото есть подпись, сохраняет её как текстовый промпт.
    """
    photo_file_id = None
    # Telegram передаёт фото с разными размерами, берём последнее (наибольшее)
    if update.message.photo:
        photo_file_id = update.message.photo[-1].file_id
    elif update.message.document and update.message.document.mime_type.startswith("image"):
        # Если изображение прислано как документ (без сжатия)
        photo_file_id = update.message.document.file_id
    else:
        await update.message.reply_text("⚠️ Это не изображение. Пожалуйста, отправьте фото.")
        return P_PHOTO
    # Сохраняем file_id фотографии
    context.user_data["photo_file_id"] = photo_file_id
    # Если у изображения была подпись (caption), используем её как текстовый промпт
    prompt_caption = update.message.caption.strip() if update.message.caption else ""
    if prompt_caption:
        context.user_data["prompt_text"] = prompt_caption
        # Получено и фото, и описание – переходим сразу к выбору формата
        ratio_keyboard = [
            [InlineKeyboardButton("16:9 (горизонтальное)", callback_data="ratio_16_9")],
            [InlineKeyboardButton("9:16 (вертикальное)", callback_data="ratio_9_16")],
            [InlineKeyboardButton("↩️ Назад в меню", callback_data="back_to_menu")]
        ]
        await update.message.reply_text("📄 Описание получено.\n🖼️ *Выберите формат видео*:", reply_markup=InlineKeyboardMarkup(ratio_keyboard), parse_mode="Markdown")
        return P_RATIO
    else:
        # Если подписи нет, запрашиваем текстовое описание (промпт)
        await update.message.reply_text(
            "✏️ Теперь отправьте текстовое описание видео (промпт) или введите /skip, чтобы пропустить текстовое описание.",
            parse_mode="Markdown"
        )
        return P_PROMPT

async def receive_photo_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    Получает текстовый промпт от пользователя после изображения (если пользователь не добавил подпись к фото).
    """
    user_text = update.message.text.strip()
    # Обработка команды /skip (пропустить описание)
    if user_text.lower() in ("/skip", "skip", "/пропустить", "пропустить"):
        context.user_data["prompt_text"] = ""
    else:
        if not user_text:
            await update.message.reply_text("⚠️ Вы отправили пустое сообщение. Введите описание или /skip для пропуска.")
            return P_PROMPT
        context.user_data["prompt_text"] = user_text
    # Переходим к выбору соотношения сторон
    ratio_keyboard = [
        [InlineKeyboardButton("16:9 (горизонтальное)", callback_data="ratio_16_9")],
        [InlineKeyboardButton("9:16 (вертикальное)", callback_data="ratio_9_16")],
        [InlineKeyboardButton("↩️ Назад в меню", callback_data="back_to_menu")]
    ]
    await update.message.reply_text("🖼️ *Выберите формат видео*:", reply_markup=InlineKeyboardMarkup(ratio_keyboard), parse_mode="Markdown")
    return P_RATIO

async def select_ratio(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    Обрабатывает выбор соотношения сторон (16:9 или 9:16) и переходит к шагу подтверждения.
    """
    query = update.callback_query
    await query.answer()
    data = query.data
    # Определяем выбранное соотношение сторон
    aspect_ratio = "16:9" if "16_9" in data else "9:16"
    context.user_data["aspect_ratio"] = aspect_ratio
    # Клавиатура подтверждения запуска генерации
    confirm_keyboard = [
        [InlineKeyboardButton("🚀 Запустить генерацию", callback_data="confirm_start")],
        [InlineKeyboardButton("↩️ Назад в меню", callback_data="back_to_menu")]
    ]
    # Формируем текст для подтверждения (обзор запроса)
    prompt_summary = context.user_data.get("prompt_text", "")
    photo_attached = "photo_file_id" in context.user_data
    summary_text = "✅ *Проверьте детали перед запуском:* \n"
    if prompt_summary:
        summary_text += f"• *Описание:* {prompt_summary}\n"
    if photo_attached:
        summary_text += "• Режим: видео по изображению\n"
    else:
        summary_text += "• Режим: видео по тексту\n"
    summary_text += f"• Формат видео: {aspect_ratio}\n\nНажмите «🚀 Запустить генерацию», чтобы начать."
    await query.edit_message_text(summary_text, reply_markup=InlineKeyboardMarkup(confirm_keyboard), parse_mode="Markdown")
    # Возвращаем соответствующее состояние подтверждения
    return T_CONFIRM if not photo_attached else P_CONFIRM

async def start_generation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    Финальный шаг: отправляет запрос на генерацию видео через KIE API, отслеживает статус и высылает результат пользователю.
    """
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat_id
    # Сообщаем пользователю, что задача принята в работу
    await query.edit_message_text("🚀 Отправляю задачу в Veo3...")
    # Готовим данные запроса для KIE API
    prompt_text = context.user_data.get("prompt_text", "")
    aspect_ratio = context.user_data.get("aspect_ratio", "16:9")
    payload = {
        "prompt": prompt_text,
        "model": "veo3",
        "aspectRatio": aspect_ratio,
        "enableFallback": True
    }
    if "photo_file_id" in context.user_data:
        # Если приложено изображение, получаем URL файла через Telegram API
        file_id = context.user_data["photo_file_id"]
        bot = context.bot
        file = await bot.get_file(file_id)
        file_url = file.file_path  # прямая ссылка на файл
        # (KIE требует публично доступный URL; используем прямой URL Telegram файла)
        payload["imageUrls"] = [file_url]
    # Отправляем задачу на генерацию в API KIE
    def post_request(url, headers, json):
        try:
            return requests.post(url, headers=headers, json=json, timeout=30)
        except Exception as err:
            return err
    headers = {
        "Authorization": f"Bearer {KIE_API_KEY}",
        "Content-Type": "application/json"
    }
    logger.info(f"Отправка запроса генерации на {KIE_GENERATE_URL}")
    try:
        response = await asyncio.get_running_loop().run_in_executor(None, post_request, KIE_GENERATE_URL, headers, payload)
    except Exception as e:
        logger.error(f"Ошибка отправки запроса к KIE: {e}")
        await context.bot.send_message(chat_id, "❌ Произошла ошибка при отправке запроса на генерацию. Попробуйте позже.")
        return ConversationHandler.END
    if isinstance(response, Exception):
        logger.error(f"Ошибка при отправке запроса к KIE: {response}")
        await context.bot.send_message(chat_id, "❌ Произошла ошибка при запуске генерации. Попробуйте позже.")
        return ConversationHandler.END
    try:
        result = response.json()
    except Exception as e:
        logger.error(f"Некорректный JSON-ответ от KIE: {e}")
        await context.bot.send_message(chat_id, "❌ Некорректный ответ от сервиса генерации.")
        return ConversationHandler.END
    if result.get("code") != 200 or "data" not in result or "taskId" not in result["data"]:
        # Запрос на генерацию завершился ошибкой
        error_msg = result.get("msg") or "Не удалось создать задачу."
        await context.bot.send_message(chat_id, f"❌ Ошибка запуска генерации: {error_msg}")
        return ConversationHandler.END
    task_id = result["data"]["taskId"]
    logger.info(f"Video generation task created: {task_id}")
    # Уведомляем пользователя о создании задачи и начинаем отслеживать статус
    await context.bot.send_message(chat_id, f"📄 Задача создана. ID: `{task_id}`", parse_mode="Markdown")
    await context.bot.send_message(chat_id, "⚙️ Генерация идёт... ⏳")
    status_headers = {"Authorization": f"Bearer {KIE_API_KEY}"}
    success = False
    video_url = None
    error_reason = None
    elapsed = 0
    check_interval = 5
    next_update = 60
    while True:
        # Ждём перед очередной проверкой статуса
        await asyncio.sleep(check_interval)
        elapsed += check_interval
        # Проверка таймаута (на случай долгой генерации)
        if elapsed >= 600:
            error_reason = "Превышено время ожидания генерации."
            break
        # Запрашиваем статус задачи
        status_params = {"taskId": task_id}
        def get_status(url, headers, params):
            try:
                return requests.get(url, headers=headers, params=params, timeout=10)
            except Exception as err:
                return err
        status_response = await asyncio.get_running_loop().run_in_executor(None, get_status, KIE_STATUS_URL, status_headers, status_params)
        if isinstance(status_response, Exception):
            logger.warning(f"Ошибка запроса статуса: {status_response}")
            # При временной ошибке продолжаем пытаться
            continue
        try:
            status_data = status_response.json()
        except Exception as e:
            logger.warning(f"Некорректный ответ при проверке статуса, пропуск: {e}")
            continue
        if status_data.get("code") != 200 or "data" not in status_data:
            # Непредвиденный формат ответа, пробуем снова
            logger.debug(f"Статус-ответ: {status_data}")
            continue
        data = status_data["data"]
        success_flag = data.get("successFlag")
        if success_flag == 1:
            # Успех – видео сгенерировано
            success = True
            try:
                result_urls = data["response"]["resultUrls"]
                if result_urls:
                    video_url = result_urls[0]
            except KeyError:
                video_url = None
            break
        elif success_flag in (2, 3):
            # Задача завершилась неудачей
            error_reason = data.get("errorMessage") or "Задача не выполнена (ошибка генерации)."
            break
        else:
            # successFlag == 0 (ещё генерируется)
            if elapsed >= next_update:
                await context.bot.send_message(chat_id, f"⚙️ Генерация продолжается... {elapsed} сек")
                next_update += 60
            continue
    # Генерация завершена: либо успех, либо ошибка
    if success and video_url:
        # Пытаемся отправить видео как файл по прямому URL
        try:
            await context.bot.send_video(chat_id, video=video_url, caption="🎬 Видео готово!")
        except Exception as e:
            logger.error(f"Не удалось отправить видео по URL: {e}. Скачиваем и отправляем файл...")
            # Если отправка по URL не удалась, пробуем скачать и отправить вручную
            def download_video(url):
                return requests.get(url, timeout=120)
            vid_resp = await asyncio.get_running_loop().run_in_executor(None, download_video, video_url)
            if vid_resp and vid_resp.status_code == 200:
                await context.bot.send_video(chat_id, video=vid_resp.content, filename="veo_video.mp4", caption="🎬 Видео готово!")
            else:
                await context.bot.send_message(chat_id, "✅ Генерация завершена, но не удалось получить видео.")
        # После отправки видео предлагаем вернуться в меню
        await context.bot.send_message(chat_id, "↩️ Вернуться в меню: /start")
    else:
        # Генерация не удалась (или истекло время)
        await context.bot.send_message(chat_id, f"❌ Генерация не удалась. Причина: {error_reason or 'неизвестная ошибка'}")
        await context.bot.send_message(chat_id, "ℹ️ Вы можете изменить запрос и попробовать снова. Введите /start для возвращения в меню.")
    return ConversationHandler.END

async def handle_user_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Обрабатывает обычные текстовые сообщения пользователя, когда активен режим чата (Prompt-master или Обычный чат).
    """
    mode = context.user_data.get("mode")
    if mode not in ("prompt", "chat"):
        # Если пользователь прислал текст вне какого-либо режима, предлагаем воспользоваться /start
        await update.message.reply_text("ℹ️ Пожалуйста, воспользуйтесь командой /start, чтобы вернуться в меню.")
        return
    user_text = update.message.text.strip()
    if not user_text:
        return  # пустое сообщение игнорируем
    # Определяем историю диалога в зависимости от режима
    if mode == "prompt":
        history = context.user_data.get("prompt_history", [])
    else:  # mode == "chat"
        history = context.user_data.get("chat_history", [])
    # Добавляем сообщение пользователя в историю
    history.append({"role": "user", "content": user_text})
    # Готовим запрос к OpenAI Chat Completion API
    api_url = "https://api.openai.com/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "Content-Type": "application/json"
    }
    data = {
        "model": "gpt-3.5-turbo",
        "messages": history
    }
    # Выполняем запрос к OpenAI API в отдельном потоке (чтобы не блокировать обработку бота)
    def ask_openai(url, headers, payload):
        try:
            return requests.post(url, headers=headers, json=payload, timeout=20)
        except Exception as err:
            return err
    response = await asyncio.get_running_loop().run_in_executor(None, ask_openai, api_url, headers, data)
    if isinstance(response, Exception):
        logger.error(f"Ошибка при запросе к OpenAI: {response}")
        await update.message.reply_text("⚠️ Не удалось получить ответ от ChatGPT. Попробуйте позже.")
        # Удаляем последний вопрос пользователя из истории (чтобы не повторялся при следующей попытке)
        history.pop()
        return
    try:
        result = response.json()
        assistant_msg = result["choices"][0]["message"]["content"].strip()
    except Exception as e:
        logger.error(f"Ошибка разбора ответа OpenAI: {e}")
        await update.message.reply_text("⚠️ Ошибка при обработке ответа от ChatGPT.")
        history.pop()
        return
    # Добавляем ответ ассистента в историю и отправляем его пользователю
    history.append({"role": "assistant", "content": assistant_msg})
    await update.message.reply_text(assistant_msg)

def main() -> None:
    logger.info(f"KIE endpoint: {KIE_GENERATE_URL}")
    # Создаем приложение (бота) и регистрируем обработчики
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    # Команды /start и /help
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("help", start_command))
    # Диалоги генерации видео
    conv_text = ConversationHandler(
        entry_points=[CallbackQueryHandler(gen_text_entry, pattern="^gen_text$")],
        states={
            T_PROMPT: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_text_prompt),
                       CallbackQueryHandler(back_to_menu, pattern="^back_to_menu$")],
            T_RATIO: [CallbackQueryHandler(select_ratio, pattern="^ratio_"),
                      CallbackQueryHandler(back_to_menu, pattern="^back_to_menu$")],
            T_CONFIRM: [CallbackQueryHandler(start_generation, pattern="^confirm_start$"),
                        CallbackQueryHandler(back_to_menu, pattern="^back_to_menu$")]
        },
        fallbacks=[CommandHandler("cancel", cancel_conversation)],
        allow_reentry=False
    )
    conv_photo = ConversationHandler(
        entry_points=[CallbackQueryHandler(gen_photo_entry, pattern="^gen_photo$")],
        states={
            P_PHOTO: [MessageHandler(~filters.COMMAND, receive_photo),
                      CallbackQueryHandler(back_to_menu, pattern="^back_to_menu$")],
            P_PROMPT: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_photo_prompt),
                       CommandHandler("skip", receive_photo_prompt),
                       CallbackQueryHandler(back_to_menu, pattern="^back_to_menu$")],
            P_RATIO: [CallbackQueryHandler(select_ratio, pattern="^ratio_"),
                      CallbackQueryHandler(back_to_menu, pattern="^back_to_menu$")],
            P_CONFIRM: [CallbackQueryHandler(start_generation, pattern="^confirm_start$"),
                        CallbackQueryHandler(back_to_menu, pattern="^back_to_menu$")]
        },
        fallbacks=[CommandHandler("cancel", cancel_conversation)],
        allow_reentry=False
    )
    app.add_handler(conv_text)
    app.add_handler(conv_photo)
    # Обработчики для перехода в режимы ChatGPT и FAQ
    app.add_handler(CallbackQueryHandler(enter_prompt_mode, pattern="^mode_prompt$"))
    app.add_handler(CallbackQueryHandler(enter_chat_mode, pattern="^mode_chat$"))
    app.add_handler(CallbackQueryHandler(faq_callback, pattern="^faq$"))
    # Обработчик кнопки «Назад в меню» вне диалогов (например, в режимах чата)
    app.add_handler(CallbackQueryHandler(back_to_menu, pattern="^back_to_menu$"))
    # Обработчик обычных сообщений пользователя (когда активен режим чата с GPT)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_user_message))

    # Запуск бота (долгопрочное подключение с опросом обновлений)
    app.run_polling()

if __name__ == "__main__":
    main()

# Initialize and run the bot (not shown here, ensure to add handlers and call app.run_polling())

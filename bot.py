import logging, os
from io import BytesIO
from dotenv import load_dotenv

import openai
import requests
from telegram import ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Load environment variables
load_dotenv()
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
KIE_API_KEY   = os.getenv('KIE_API_KEY')
KIE_BASE_URL  = os.getenv('KIE_BASE_URL')
KIE_GEN_PATH  = os.getenv('KIE_GEN_PATH')
KIE_STATUS_PATH = os.getenv('KIE_STATUS_PATH', '/api/v1/veo/record-info')
# Optional environment variables
KIE_MODEL       = os.getenv('KIE_MODEL', 'veo3_fast')
KIE_ASPECT_RATIO = os.getenv('KIE_ASPECT_RATIO', '16:9')
OPENAI_API_KEY  = os.getenv('OPENAI_API_KEY')

openai.api_key = OPENAI_API_KEY

async def refine_prompt_with_openai(user_prompt: str) -> str:
    """Refine or translate the user's prompt into a detailed English prompt using OpenAI."""
    try:
        system_msg = {
            "role": "system",
            "content": (
                "You are an AI assistant skilled at creating detailed video generation prompts. "
                "Transform the user's idea into a single creative English prompt with vivid details."
            )
        }
        user_msg = {"role": "user", "content": user_prompt}
        response = openai.ChatCompletion.create(
            model="gpt-3.5-turbo",
            messages=[system_msg, user_msg],
            temperature=0.7,
            max_tokens=100
        )
        refined_prompt = response.choices[0].message.content.strip()
        logger.info(f"Refined prompt: {refined_prompt}")
        return refined_prompt
    except Exception as e:
        logger.error(f"OpenAI API error: {e}")
        # If there's an error, fall back to the original user prompt
        return user_prompt

async def generate_video_via_kie(prompt: str, image_url: str = None) -> str:
    """Send a generation request to the KIE API and poll for the resulting video URL."""
    try:
        headers = {"Authorization": f"Bearer {KIE_API_KEY}"}
        payload = {
            "prompt": prompt,
            "model": KIE_MODEL,
            "aspectRatio": KIE_ASPECT_RATIO
        }
        if image_url:
            payload["imageUrls"] = [image_url]
        # Submit generation task
        resp = requests.post(KIE_BASE_URL + KIE_GEN_PATH, headers=headers, json=payload)
        if resp.status_code != 200:
            logger.error(f"KIE API request failed with HTTP {resp.status_code}")
            return ""
        data = resp.json()
        if data.get("code") != 200 or "taskId" not in data.get("data", {}):
            logger.error(f"KIE API error response: {data.get('msg')}")
            return ""
        task_id = data["data"]["taskId"]
        logger.info(f"Generation task submitted (task_id={task_id})")
        # Poll for completion
        video_url = ""
        error_msg = ""
        import asyncio
        for attempt in range(60):  # up to ~3 minutes
            await asyncio.sleep(3)
            status = requests.get(f"{KIE_BASE_URL}{KIE_STATUS_PATH}?taskId={task_id}", headers=headers)
            if status.status_code != 200:
                error_msg = f"Status check HTTP {status.status_code}"
                logger.error(f"KIE status check failed: HTTP {status.status_code}")
                break
            status_data = status.json()
            if status_data.get("code") != 200:
                error_msg = status_data.get("msg", "Unknown error")
                logger.error(f"KIE status response error: {error_msg}")
                break
            info = status_data.get("data", {})
            flag = info.get("successFlag")
            if flag == 0:
                # Still generating
                continue
            if flag == 1:
                # Success – retrieve video URL
                resp_info = info.get("response", {})
                if "resultUrls" in resp_info and resp_info["resultUrls"]:
                    video_url = resp_info["resultUrls"][0]
                elif "resultUrl" in resp_info:
                    video_url = resp_info["resultUrl"]
                else:
                    logger.error("KIE responded success without video URL.")
                break
            # If successFlag is 2 or 3, generation failed
            error_msg = info.get("errorMessage") or info.get("errorCode") or status_data.get("msg", "")
            logger.error(f"KIE generation failed: {error_msg}")
            break
        else:
            # loop didn't break, meaning timeout
            error_msg = "Video generation timed out."
            logger.error(error_msg)
        return video_url
    except Exception as e:
        logger.exception(f"Exception in KIE generation: {e}")
        return ""

# Handler: /start command
async def start_handler(update, context):
    # Reset user state and present the main menu
    context.user_data.clear()
    keyboard = [["🎥 Генерация по тексту", "🎥 Генерация по фото"]]
    await update.message.reply_text(
        "Привет! Я помогу сгенерировать короткое видео с помощью нейросети Veo3.\n"
        "Выберите, что будем использовать для генерации видео:",
        reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    )

# Handler: /exit command
async def exit_handler(update, context):
    if context.user_data.get("active_task"):
        # If a generation is in progress, mark it to cancel
        context.user_data["cancel"] = True
        context.user_data["active_task"] = False
        context.user_data["pending"] = None
        await update.message.reply_text("Генерация отменена.")
    else:
        # No generation in progress, just reset state
        context.user_data.clear()
        await update.message.reply_text("Действие отменено.", reply_markup=ReplyKeyboardRemove())

# Handler: when user selects "Generate by text" from menu
async def choose_text_mode(update, context):
    context.user_data["pending"] = "text"
    await update.message.reply_text("Отправьте текстовое описание желаемого видео.")

# Handler: when user selects "Generate by photo" from menu
async def choose_photo_mode(update, context):
    context.user_data["pending"] = "photo"
    await update.message.reply_text(
        "Пришлите изображение для генерации видео.\n"
        "Вы можете добавить описание в подпись к фото."
    )

# Handler: receiving a text message (prompt)
async def text_message_handler(update, context):
    user_text = update.message.text
    # Ignore if it's actually a command or menu selection
    if user_text.startswith('/'):
        return
    if context.user_data.get("pending") != "text":
        # If not in text generation mode
        await update.message.reply_text("Пожалуйста, выберите режим \"Генерация по тексту\" на клавиатуре.")
        return
    if context.user_data.get("active_task"):
        await update.message.reply_text("⏳ Пожалуйста, дождитесь окончания предыдущей генерации.")
        return
    # Mark generation as active
    context.user_data["active_task"] = True
    context.user_data["cancel"] = False
    try:
        await update.message.reply_text("🔄 Генерирую видео, это может занять немного времени...")
        # Refine the user's prompt with OpenAI
        refined_prompt = await refine_prompt_with_openai(user_text)
        # Request video generation from KIE
        video_url = await generate_video_via_kie(refined_prompt)
        # Check if user canceled during wait
        if context.user_data.get("cancel"):
            return  # skip sending result
        if not video_url:
            await update.message.reply_text("❌ Не удалось сгенерировать видео по данному запросу.")
        else:
            try:
                # Try to send the video by URL
                await context.bot.send_video(chat_id=update.effective_chat.id, video=video_url, caption="🎥 Ваше видео готово!")
            except Exception as e:
                logger.warning(f"Sending video by URL failed: {e}. Falling back to file upload.")
                # Download the video and send it as a file
                video_data = None
                try:
                    file_resp = requests.get(video_url, timeout=30)
                    if file_resp.status_code == 200:
                        video_data = file_resp.content
                except Exception as ex:
                    logger.error(f"Error downloading video: {ex}")
                if video_data:
                    video_file = BytesIO(video_data)
                    video_file.name = "video.mp4"
                    await context.bot.send_video(chat_id=update.effective_chat.id, video=video_file, caption="🎥 Ваше видео готово!")
                else:
                    await update.message.reply_text("⚠️ Не удалось получить сгенерированное видео.")
    finally:
        # Reset state for next use
        context.user_data["active_task"] = False
        context.user_data["pending"] = None

# Handler: receiving a photo message
async def photo_message_handler(update, context):
    if context.user_data.get("pending") != "photo":
        await update.message.reply_text("Пожалуйста, выберите режим \"Генерация по фото\" на клавиатуре прежде, чем отправлять изображение.")
        return
    if context.user_data.get("active_task"):
        await update.message.reply_text("⏳ Пожалуйста, дождитесь окончания предыдущей генерации.")
        return
    # Get the file_id of the highest-resolution photo
    photo_file_id = update.message.photo[-1].file_id
    # Obtain file path from Telegram
    file_obj = await context.bot.get_file(photo_file_id)
    image_url = f"https://api.telegram.org/file/bot{TELEGRAM_TOKEN}/{file_obj.file_path}"
    # Mark generation as active
    context.user_data["active_task"] = True
    context.user_data["cancel"] = False
    try:
        await update.message.reply_text("🔄 Генерирую видео по изображению, это может занять немного времени...")
        # Use caption as user prompt if available
        user_caption = (update.message.caption or "").strip()
        refined_prompt = await refine_prompt_with_openai(user_caption) if user_caption else ""
        # Request video generation from KIE with image
        video_url = await generate_video_via_kie(refined_prompt, image_url=image_url)
        if context.user_data.get("cancel"):
            return
        if not video_url:
            await update.message.reply_text("❌ Не удалось сгенерировать видео по присланному изображению.")
        else:
            try:
                await context.bot.send_video(chat_id=update.effective_chat.id, video=video_url, caption="🎥 Ваше видео готово!")
            except Exception as e:
                logger.warning(f"Sending video by URL failed: {e}. Falling back to file upload.")
                video_data = None
                try:
                    file_resp = requests.get(video_url, timeout=30)
                    if file_resp.status_code == 200:
                        video_data = file_resp.content
                except Exception as ex:
                    logger.error(f"Error downloading video: {ex}")
                if video_data:
                    video_file = BytesIO(video_data)
                    video_file.name = "video.mp4"
                    await context.bot.send_video(chat_id=update.effective_chat.id, video=video_file, caption="🎥 Ваше видео готово!")
                else:
                    await update.message.reply_text("⚠️ Не удалось получить сгенерированное видео.")
    finally:
        context.user_data["active_task"] = False
        context.user_data["pending"] = None

# Set up the Telegram application and handlers
application = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
application.add_handler(CommandHandler("start", start_handler))
application.add_handler(CommandHandler("exit", exit_handler))
application.add_handler(MessageHandler(filters.Regex('^🎥 Генерация по тексту$'), choose_text_mode))
application.add_handler(MessageHandler(filters.Regex('^🎥 Генерация по фото$'), choose_photo_mode))
application.add_handler(MessageHandler(filters.PHOTO, photo_message_handler))
application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_message_handler))

if __name__ == "__main__":
    application.run_polling()

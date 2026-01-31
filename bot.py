# bot.py
import os
import logging
import traceback
from typing import Optional

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# GenAI (google-genai)
from google import genai

# -------------- config & logging --------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("shop-ai-bot")

BOT_TOKEN = os.getenv("BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN not set")
if not GEMINI_API_KEY:
    raise RuntimeError("GEMINI_API_KEY not set")

# Create client
client = genai.Client(api_key=GEMINI_API_KEY)

# Candidate models (try in order)
MODEL_CANDIDATES = [
    "models/gemini-2.5-flash-image",
    "models/gemini-2.5-flash",
    "models/gemini-2.0-flash",
    "models/gemini-1.0-pro-vision-latest",
]

MAX_IMAGE_BYTES = 6 * 1024 * 1024  # 6 MB conservative

# -------------- helpers --------------
def normalize_response_text(resp) -> str:
    """Extract readable text from various response shapes."""
    if resp is None:
        return ""
    # common attribute
    text = getattr(resp, "text", None)
    if text:
        return text.strip()
    # some responses wrap in candidates/outputs
    candidates = getattr(resp, "candidates", None)
    if candidates and isinstance(candidates, (list, tuple)) and len(candidates) > 0:
        first = candidates[0]
        t = getattr(first, "content", None) or getattr(first, "text", None)
        if t:
            return t.strip()
    outputs = getattr(resp, "outputs", None)
    if outputs and isinstance(outputs, (list, tuple)):
        parts = []
        for o in outputs:
            p = getattr(o, "text", None) or getattr(o, "content", None)
            if p:
                parts.append(p)
        if parts:
            return "\n".join(parts).strip()
    # fallback to stringification
    return str(resp).strip()

# -------------- commands --------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("✅ البوت يعمل\n📸 أرسل صورة منتج وسأحاول التعرف عليه")

# -------------- photo handler --------------
async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔍 أفحص الصورة... الرجاء الانتظار ثانيتين")
    try:
        photos = update.message.photo
        if not photos:
            await update.message.reply_text("❌ لم أجد صورة في الرسالة.")
            return

        # pick candidate from largest down; try to keep under MAX_IMAGE_BYTES
        selected_file = None
        image_bytes: Optional[bytes] = None
        tried_sizes = []

        for idx in range(len(photos) - 1, -1, -1):
            p = photos[idx]
            f = await p.get_file()  # telegram.File
            try:
                b = await f.download_as_bytearray()
                size = len(b)
                tried_sizes.append((idx, size))
                logger.info("photo idx=%d size=%d bytes", idx, size)
                if size <= MAX_IMAGE_BYTES:
                    selected_file = f
                    image_bytes = b
                    break
            except Exception as e:
                # download might fail in some contexts; keep file object for URL fallback
                tried_sizes.append((idx, None))
                selected_file = f

        if selected_file is None:
            # fallback to smallest
            fsmall = await photos[0].get_file()
            selected_file = fsmall
            image_bytes = await fsmall.download_as_bytearray()
            tried_sizes.append((0, len(image_bytes)))

        # Build Telegram public file URL (contains bot token in path)
        file_path = getattr(selected_file, "file_path", None)
        telegram_file_url = None
        if file_path:
            telegram_file_url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_path}"
            logger.info("Built telegram file URL (truncated): %s", telegram_file_url[:120])

        prompt = (
            "Identify the product visible in this image. "
            "Reply with a short product name (2-6 words) or a concise description. "
            "If you cannot identify a specific product, reply 'Unknown'."
        )

        # Try candidate models and multiple input styles (URL first, then bytes)
        for model_name in MODEL_CANDIDATES:
            logger.info("Trying model=%s (URL then bytes)", model_name)

            # 1) URL-based attempt (preferred for vision models)
            if telegram_file_url:
                try:
                    logger.info("Attempting generate_text with images=[URL] on model=%s", model_name)
                    resp = client.models.generate_text(
                        model=model_name,
                        prompt=prompt,
                        images=[telegram_file_url],
                    )
                    text = normalize_response_text(resp)
                    if text:
                        await update.message.reply_text(f"🛒 المنتج المحتمل:\n{text}")
                        return
                    logger.info("model %s returned empty text for URL attempt", model_name)
                except Exception as e:
                    logger.warning("URL-based call failed for model=%s : %s", model_name, repr(e))

            # 2) bytes-based attempt
            if image_bytes:
                try:
                    logger.info("Attempting generate_text with images=[bytes] on model=%s", model_name)
                    resp2 = client.models.generate_text(
                        model=model_name,
                        prompt=prompt,
                        images=[bytes(image_bytes)],
                    )
                    text2 = normalize_response_text(resp2)
                    if text2:
                        await update.message.reply_text(f"🛒 المنتج المحتمل:\n{text2}")
                        return
                    logger.info("model %s returned empty text for bytes attempt", model_name)
                except Exception as e:
                    logger.warning("Bytes-based call failed for model=%s : %s", model_name, repr(e))

        # if none succeeded
        await update.message.reply_text(
            "❌ لم أتمكن من استخراج اسم المنتج من الصورة. حاول صورة أو وصف أو أعد المحاولة لاحقًا."
        )
        logger.warning("All attempts failed. tried_sizes=%s telegram_file_url=%s", tried_sizes, bool(telegram_file_url))

    except Exception as exc:
        tb = traceback.format_exc()
        logger.error("Unhandled exception in handle_photo: %s\n%s", repr(exc), tb)
        await update.message.reply_text("❌ حدث خطأ أثناء تحليل الصورة")
        # print to stdout/stderr for Railway logs
        print("Gemini detailed error:", repr(exc))
        print(tb)

# -------------- main --------------
def main():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    logger.info("Bot starting (polling)...")
    app.run_polling(close_loop=False)

if __name__ == "__main__":
    main()

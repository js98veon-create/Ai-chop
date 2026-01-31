import os
import logging
import traceback

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)
import google.generativeai as genai

# ---------- CONFIG & LOGGING ----------
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

if not BOT_TOKEN:
    logger.error("BOT_TOKEN not set in environment variables")
    raise RuntimeError("BOT_TOKEN not set")

if not GEMINI_API_KEY:
    logger.error("GEMINI_API_KEY not set in environment variables")
    raise RuntimeError("GEMINI_API_KEY not set")

genai.configure(api_key=GEMINI_API_KEY)

# Use a stable vision model; change here if Google updates model names
MODEL_NAME = "models/gemini-1.0-pro-vision-latest"
logger.info("Using Gemini model: %s", MODEL_NAME)

try:
    model = genai.GenerativeModel(model_name=MODEL_NAME)
except Exception as e:
    logger.exception("Failed to construct GenerativeModel; check model name and library compatibility.")
    raise

MAX_IMAGE_BYTES = 6 * 1024 * 1024  # 6 MB

# ---------- HELPERS ----------
def extract_text_from_response(resp) -> str:
    if resp is None:
        return ""
    text = getattr(resp, "text", None)
    if text:
        return text.strip()
    candidates = getattr(resp, "candidates", None)
    if candidates and isinstance(candidates, (list, tuple)) and len(candidates) > 0:
        first = candidates[0]
        t = getattr(first, "content", None) or getattr(first, "text", None)
        if t:
            return t.strip()
    outputs = getattr(resp, "outputs", None)
    if outputs and isinstance(outputs, (list, tuple)) and len(outputs) > 0:
        pieces = []
        for o in outputs:
            p = getattr(o, "text", None) or getattr(o, "content", None)
            if p:
                pieces.append(p)
        if pieces:
            return "\n".join(pieces).strip()
    # fallback to string
    return str(resp).strip()

# ---------- COMMANDS ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("✅ البوت يعمل بنجاح\n📸 أرسل صورة منتج وسأحاول التعرف عليه")

# ---------- HANDLER ----------
async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔍 أفحص الصورة...")
    try:
        photos = update.message.photo
        if not photos:
            await update.message.reply_text("❌ لم أجد صورة في الرسالة.")
            return

        # choose candidate photo (try largest but we'll fall back)
        selected_file = None
        tried_sizes = []
        for idx in range(len(photos)-1, -1, -1):
            photo = photos[idx]
            file = await photo.get_file()
            # file.file_path may be available
            try:
                # download size check
                b = await file.download_as_bytearray()
                size = len(b)
                tried_sizes.append((idx, size))
                if size <= MAX_IMAGE_BYTES:
                    selected_file = file
                    image_bytes = b
                    logger.info("Selected photo index %d size %d", idx, size)
                    break
                else:
                    logger.info("Photo index %d too large (%d bytes)", idx, size)
            except Exception as e:
                # if download fails, still keep file object for URL attempt
                logger.warning("Could not download variant idx %d: %s", idx, repr(e))
                tried_sizes.append((idx, None))
                selected_file = file  # keep at least a candidate for URL

        if selected_file is None:
            # extremely unlikely, but handle
            await update.message.reply_text("❌ حدث خطأ في معالجة الصورة محليًا. حاول إرسالها مرة أخرى.")
            logger.error("No selected_file after iterating photos. tried: %s", tried_sizes)
            return

        # Build Telegram file URL: https://api.telegram.org/file/bot<token>/<file_path>
        file_path = getattr(selected_file, "file_path", None)
        telegram_file_url = None
        if file_path:
            telegram_file_url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_path}"
            logger.info("Built telegram_file_url: %s ...", telegram_file_url[:80])

        prompt = (
            "Describe the product visible in this image in a short phrase (product name). "
            "If you cannot identify a specific product, reply 'Unknown'."
        )

        # ATTEMPT SEQUENCE:
        # 1) Try send image by URL if available (most robust for some Gemini versions)
        if telegram_file_url:
            try:
                logger.info("Attempting model.generate_text with image URL")
                # Many genai variants accept images as list of URLs or bytes
                try:
                    resp = model.generate_text(prompt=prompt, images=[telegram_file_url])
                except TypeError:
                    # fallback positional
                    resp = model.generate_text(prompt, [telegram_file_url])
                product_name = extract_text_from_response(resp)
                if product_name:
                    await update.message.reply_text(f"🛒 المنتج المحتمل:\n{product_name}")
                    return
                logger.info("URL-based generate_text returned empty; continuing to other attempts")
            except Exception as e:
                logger.exception("URL-based generate_text failed: %s", repr(e))

            # try generate_content with uri object (some libs accept dict with uri)
            try:
                logger.info("Attempting model.generate_content with uri dict")
                resp = model.generate_content([prompt, {"uri": telegram_file_url}])
                product_name = extract_text_from_response(resp)
                if product_name:
                    await update.message.reply_text(f"🛒 المنتج المحتمل:\n{product_name}")
                    return
                logger.info("generate_content with uri returned empty")
            except Exception as e:
                logger.exception("generate_content with uri failed: %s", repr(e))

        # 2) If URL approaches didn't work, try sending raw bytes (if not already downloaded or if small)
        try:
            logger.info("Attempting byte-based generate_text/generate_content as fallback")
            # ensure we have image_bytes (may have been set earlier)
            image_bytes = locals().get("image_bytes", None)
            if not image_bytes:
                # download now (smallest variant)
                file2 = await photos[0].get_file()
                image_bytes = await file2.download_as_bytearray()
                logger.info("Downloaded smallest variant bytes size %d", len(image_bytes))

            # try generate_content with data dict
            try:
                resp = model.generate_content([prompt, {"mime_type": "image/jpeg", "data": bytes(image_bytes)}])
                product_name = extract_text_from_response(resp)
                if product_name:
                    await update.message.reply_text(f"🛒 المنتج المحتمل:\n{product_name}")
                    return
                logger.info("byte-based generate_content returned empty")
            except Exception as e:
                logger.exception("byte-based generate_content failed: %s", repr(e))

            # try generate_text with images as bytes
            try:
                logger.info("Attempting generate_text with bytes")
                try:
                    resp2 = model.generate_text(prompt=prompt, images=[bytes(image_bytes)])
                except TypeError:
                    resp2 = model.generate_text(prompt, [bytes(image_bytes)])
                product_name = extract_text_from_response(resp2)
                if product_name:
                    await update.message.reply_text(f"🛒 المنتج المحتمل:\n{product_name}")
                    return
                logger.info("byte-based generate_text returned empty")
            except Exception as e:
                logger.exception("byte-based generate_text failed: %s", repr(e))

        except Exception as final_bytes_exc:
            logger.exception("Failed in byte-based fallback: %s", repr(final_bytes_exc))

        # If we reach here, all attempts failed
        await update.message.reply_text("❌ لم أتمكن من استخراج اسم المنتج من الصورة. حاول صورة أو وصف أو أعد المحاولة لاحقًا.")
        logger.warning("All Gemini attempts failed. tried_sizes=%s telegram_file_url=%s", tried_sizes, bool(telegram_file_url))

    except Exception as exc:
        tb = traceback.format_exc()
        logger.error("Unhandled exception in handle_photo: %s\n%s", repr(exc), tb)
        await update.message.reply_text("❌ حدث خطأ أثناء تحليل الصورة")
        print("Gemini detailed error:", repr(exc))
        print(tb)

# ---------- MAIN ----------
def main():
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))

    logger.info("Bot starting (polling)...")
    app.run_polling(close_loop=False)

if __name__ == "__main__":
    main()

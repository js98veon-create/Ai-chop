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
import google.generativeai as genai

# ---------------- config & logging ----------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN not set")
if not GEMINI_API_KEY:
    raise RuntimeError("GEMINI_API_KEY not set")

# configure the library
genai.configure(api_key=GEMINI_API_KEY)

# client (use client-based calls which match docs examples)
client = genai.Client()

# Try several candidate models (order matters: most likely first)
MODEL_CANDIDATES = [
    "models/gemini-1.0-pro-vision-latest",   # good for vision tasks (try first)
    "models/gemini-1.0-pro-text-vision",     # text-vision variants
    "models/gemini-3-pro",                   # newer multimodal model (may be available)
    "gemini-1.5-flash",                      # older naming (keeps for fallback)
]

# conservative max image size (bytes)
MAX_IMAGE_BYTES = 6 * 1024 * 1024  # 6MB

# ---------------- helpers ----------------
def extract_text_from_response(resp) -> str:
    """
    Normalize various response shapes from google.generativeai.
    """
    if resp is None:
        return ""
    # newer responses often have .text
    text = getattr(resp, "text", None)
    if text:
        return text.strip()
    # older shapes: .candidates or .outputs
    candidates = getattr(resp, "candidates", None)
    if candidates and isinstance(candidates, (list, tuple)) and len(candidates) > 0:
        first = candidates[0]
        t = getattr(first, "content", None) or getattr(first, "text", None)
        if t:
            return t.strip()
    outputs = getattr(resp, "outputs", None)
    if outputs and isinstance(outputs, (list, tuple)):
        pieces = []
        for o in outputs:
            p = getattr(o, "text", None) or getattr(o, "content", None)
            if p:
                pieces.append(p)
        if pieces:
            return "\n".join(pieces).strip()
    # fallback
    try:
        s = str(resp)
        return s.strip()
    except Exception:
        return ""

# ---------------- commands ----------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("✅ البوت يعمل\n📸 أرسل صورة منتج وسأحاول التعرف عليه")

# ---------------- core handler ----------------
async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔍 أفحص الصورة... الرجاء الانتظار قليلاً")
    try:
        photos = update.message.photo
        if not photos:
            await update.message.reply_text("❌ لم أجد صورة في الرسالة.")
            return

        # pick candidate: try largest -> smaller, but check size
        selected_file = None
        image_bytes: Optional[bytes] = None
        tried = []

        for idx in range(len(photos)-1, -1, -1):
            p = photos[idx]
            f = await p.get_file()
            try:
                b = await f.download_as_bytearray()
                size = len(b)
                tried.append((idx, size))
                if size <= MAX_IMAGE_BYTES:
                    selected_file = f
                    image_bytes = b
                    logger.info("Selected photo index %d size %d", idx, size)
                    break
                else:
                    logger.info("Photo idx %d too large (%d bytes), trying smaller", idx, size)
            except Exception as e:
                # keep file object for URL attempt
                tried.append((idx, None))
                selected_file = f

        if selected_file is None:
            # fallback: use the smallest
            fsmall = await photos[0].get_file()
            selected_file = fsmall
            image_bytes = await fsmall.download_as_bytearray()
            tried.append((0, len(image_bytes)))

        # build Telegram file URL if possible
        file_path = getattr(selected_file, "file_path", None)
        telegram_file_url = None
        if file_path:
            telegram_file_url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_path}"
            logger.info("Telegram file URL built (truncated): %s", telegram_file_url[:80])

        prompt = (
            "Identify the product visible in this image. Reply with a short product name only. "
            "If you cannot identify a product, reply 'Unknown'."
        )

        # Try each model and a few input styles
        for model_name in MODEL_CANDIDATES:
            logger.info("Trying model %s", model_name)
            # 1) Try URL-based generate_content/generate_text
            if telegram_file_url:
                try:
                    # try generate_content (some libs / versions support images via uri dict)
                    try:
                        resp = client.models.generate_content(model=model_name, contents=[prompt, {"uri": telegram_file_url}])
                        text = extract_text_from_response(resp)
                        if text:
                            await update.message.reply_text(f"🛒 المنتج المحتمل:\n{text}")
                            return
                        logger.info("generate_content (uri) returned empty for %s", model_name)
                    except Exception as e:
                        logger.info("generate_content(uri) failed for %s: %s", model_name, repr(e))

                    # try generate_text signature with images param
                    try:
                        resp2 = client.models.generate_text(model=model_name, prompt=prompt, images=[telegram_file_url])
                        text2 = extract_text_from_response(resp2)
                        if text2:
                            await update.message.reply_text(f"🛒 المنتج المحتمل:\n{text2}")
                            return
                        logger.info("generate_text (url) returned empty for %s", model_name)
                    except Exception as e:
                        logger.info("generate_text(url) failed for %s: %s", model_name, repr(e))

                except Exception as outer_e:
                    logger.exception("URL-based attempts failed for %s: %s", model_name, repr(outer_e))

            # 2) Try bytes-based with generate_content
            if image_bytes:
                try:
                    try:
                        resp3 = client.models.generate_content(model=model_name, contents=[prompt, {"mime_type": "image/jpeg", "data": bytes(image_bytes)}])
                        t3 = extract_text_from_response(resp3)
                        if t3:
                            await update.message.reply_text(f"🛒 المنتج المحتمل:\n{t3}")
                            return
                        logger.info("generate_content (bytes) returned empty for %s", model_name)
                    except Exception as e:
                        logger.info("generate_content(bytes) failed for %s: %s", model_name, repr(e))

                    try:
                        resp4 = client.models.generate_text(model=model_name, prompt=prompt, images=[bytes(image_bytes)])
                        t4 = extract_text_from_response(resp4)
                        if t4:
                            await update.message.reply_text(f"🛒 المنتج المحتمل:\n{t4}")
                            return
                        logger.info("generate_text (bytes) returned empty for %s", model_name)
                    except Exception as e:
                        logger.info("generate_text(bytes) failed for %s: %s", model_name, repr(e))

                except Exception as bytes_outer:
                    logger.exception("Bytes-based attempts failed for %s: %s", model_name, repr(bytes_outer))

        # if we reach here: all attempts exhausted
        await update.message.reply_text("❌ لم أتمكن من استخراج اسم المنتج من الصورة. حاول صورة أو وصف أو أعد المحاولة لاحقًا.")
        logger.warning("All models/attempts failed. Tried: %s telegram_file_url=%s", tried, bool(telegram_file_url))

    except Exception as exc:
        tb = traceback.format_exc()
        logger.error("Unhandled exception: %s\n%s", repr(exc), tb)
        await update.message.reply_text("❌ حدث خطأ أثناء تحليل الصورة")
        print("Gemini detailed error:", repr(exc))
        print(tb)

# ---------------- main ----------------
def main():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    logger.info("Bot starting (polling)...")
    app.run_polling(close_loop=False)

if __name__ == "__main__":
    main()

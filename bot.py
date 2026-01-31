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

# Max image bytes we allow to send to Gemini (conservative default)
MAX_IMAGE_BYTES = 6 * 1024 * 1024  # 6 MB

# ---------- HELPERS ----------
def extract_text_from_response(resp) -> str:
    """
    Try to pull meaningful text from a response object returned by google.generativeai.
    Different API versions may return text in different attributes.
    """
    if resp is None:
        return ""
    # Common attribute used earlier
    text = getattr(resp, "text", None)
    if text:
        return text.strip()
    # Some responses may have 'candidates' or 'outputs'
    candidates = getattr(resp, "candidates", None)
    if candidates and isinstance(candidates, (list, tuple)) and len(candidates) > 0:
        first = candidates[0]
        t = getattr(first, "content", None) or getattr(first, "text", None)
        if t:
            return t.strip()
    outputs = getattr(resp, "outputs", None)
    if outputs and isinstance(outputs, (list, tuple)) and len(outputs) > 0:
        # attempt to join textual parts
        pieces = []
        for o in outputs:
            p = getattr(o, "text", None) or getattr(o, "content", None)
            if p:
                pieces.append(p)
        if pieces:
            return "\n".join(pieces).strip()
    # Fallback to repr
    return str(resp).strip()

# ---------- COMMANDS ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("✅ البوت يعمل بنجاح\n📸 أرسل صورة منتج وسأحاول التعرف عليه")

# ---------- HANDLER ----------
async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Inform user immediately
    await update.message.reply_text("🔍 أفحص الصورة...")

    try:
        # Telegram sends multiple sizes; choose largest then fallback to smaller if size is too big
        photos = update.message.photo
        if not photos:
            await update.message.reply_text("❌ لم أجد صورة في الرسالة.")
            return

        # strategy: try the highest resolution first, but if it's too large, try smaller ones
        tried = []
        image_bytes = None
        for i in range(len(photos)-1, -1, -1):
            photo = photos[i]
            file = await photo.get_file()
            image_bytes = await file.download_as_bytearray()
            tried.append((i, len(image_bytes)))
            if len(image_bytes) <= MAX_IMAGE_BYTES:
                logger.info("Selected photo index %d with size %d bytes", i, len(image_bytes))
                break
            else:
                # try next smaller resolution
                logger.info("Photo index %d size %d > %d, trying smaller version", i, len(image_bytes), MAX_IMAGE_BYTES)
                image_bytes = None

        if image_bytes is None:
            # None of the variants were below threshold
            # Use the smallest one and ask user to send smaller if Gemini rejects it
            smallest_photo = photos[0]
            file = await smallest_photo.get_file()
            image_bytes = await file.download_as_bytearray()
            await update.message.reply_text("⚠️ الصورة كبيرة جدًا؛ حاول إرسال صورة بجودة أقل أو مقصوصة.")
            logger.info("All photo variants were large. Tried sizes: %s", tried)

        # Build prompt
        prompt = (
            "Identify the product in the image. Reply with a short product name only. "
            "If it's unclear, reply 'Unknown'."
        )

        # Attempt 1: try generate_content (older examples)
        try:
            logger.info("Calling model.generate_content (attempt 1)")
            response = model.generate_content(
                [
                    prompt,
                    {
                        "mime_type": "image/jpeg",
                        "data": bytes(image_bytes),
                    },
                ]
            )
            product_name = extract_text_from_response(response)
            if product_name:
                await update.message.reply_text(f"🛒 المنتج المحتمل:\n{product_name}")
                return
            # if empty, continue to fallback
            logger.info("generate_content returned empty text; falling back")
        except Exception as e:
            # log and continue to fallback
            logger.exception("generate_content failed: %s", repr(e))

        # Attempt 2: try generate_text (some versions expect this)
        try:
            logger.info("Calling model.generate_text (attempt 2)")
            # some genai versions accept generate_text(prompt=..., images=[bytes,...])
            # we'll try that signature first
            try:
                response2 = model.generate_text(prompt=prompt, images=[bytes(image_bytes)])
            except TypeError:
                # fallback to positional args variant if signature differs
                response2 = model.generate_text(prompt, [bytes(image_bytes)])
            product_name = extract_text_from_response(response2)
            if product_name:
                await update.message.reply_text(f"🛒 المنتج المحتمل:\n{product_name}")
                return
            logger.info("generate_text returned empty text")
        except Exception as e2:
            logger.exception("generate_text failed: %s", repr(e2))

        # If we reach here, all attempts failed to yield a product name
        await update.message.reply_text("❌ لم أتمكن من استخراج اسم المنتج من الصورة. حاول صورة أو وصف أو أعد المحاولة لاحقًا.")
        logger.warning("All Gemini attempts failed. Tried sizes: %s", tried)

    except Exception as exc:
        # final catch-all: log detailed error for debugging in Railway logs
        tb = traceback.format_exc()
        logger.error("Unhandled exception in handle_photo: %s\n%s", repr(exc), tb)
        await update.message.reply_text("❌ حدث خطأ أثناء تحليل الصورة")
        # also print to stdout (Railway captures stdout/stderr)
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

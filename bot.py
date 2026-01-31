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

from google import genai

# ---------------- config & logging ----------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN not set")
if not GEMINI_API_KEY:
    raise RuntimeError("GEMINI_API_KEY not set")

# ---------------- GenAI Client ----------------
client = genai.Client(api_key=GEMINI_API_KEY)

# قائمة الموديلات المرشحة
MODEL_CANDIDATES = [
    "gemini-1.5",
    "gemini-1.5-vision",
    "gemini-1.0-pro-vision-latest"
]

MAX_IMAGE_BYTES = 6 * 1024 * 1024  # 6MB

# ---------------- helpers ----------------
def extract_text_from_response(resp) -> str:
    """Normalize response from google-genai"""
    if resp is None:
        return ""
    text = getattr(resp, "text", None)
    if text:
        return text.strip()
    candidates = getattr(resp, "candidates", None)
    if candidates:
        for c in candidates:
            t = getattr(c, "content", None) or getattr(c, "text", None)
            if t:
                return t.strip()
    return str(resp).strip()

# ---------------- commands ----------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "✅ البوت يعمل\n📸 أرسل صورة منتج وسأحاول التعرف عليه"
    )

# ---------------- core handler ----------------
async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔍 أفحص الصورة... الرجاء الانتظار قليلاً")
    try:
        photos = update.message.photo
        if not photos:
            await update.message.reply_text("❌ لم أجد صورة في الرسالة.")
            return

        selected_file = None
        image_bytes: Optional[bytes] = None
        tried = []

        # اختر أفضل صورة تناسب الحجم
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
                    break
            except Exception:
                tried.append((idx, None))
                selected_file = f

        if selected_file is None:
            # fallback
            fsmall = await photos[0].get_file()
            selected_file = fsmall
            image_bytes = await fsmall.download_as_bytearray()
            tried.append((0, len(image_bytes)))

        # رابط Telegram للملف
        file_path = getattr(selected_file, "file_path", None)
        telegram_file_url = None
        if file_path:
            telegram_file_url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_path}"
            logger.info("Telegram file URL: %s", telegram_file_url[:80])

        prompt = (
            "Identify the product visible in this image. Reply with a short product name only. "
            "If unknown, reply 'Unknown'."
        )

        # جرب كل موديل بالترتيب
        for model_name in MODEL_CANDIDATES:
            logger.info("Trying model %s", model_name)
            # 1) URL-based generate_text
            if telegram_file_url:
                try:
                    resp = client.models.generate_text(
                        model=model_name,
                        prompt=prompt,
                        images=[telegram_file_url]
                    )
                    text = extract_text_from_response(resp)
                    if text:
                        await update.message.reply_text(f"🛒 المنتج المحتمل:\n{text}")
                        return
                except Exception as e:
                    logger.info("URL-based generate_text failed for %s: %s", model_name, repr(e))

            # 2) Bytes-based generate_text
            if image_bytes:
                try:
                    resp2 = client.models.generate_text(
                        model=model_name,
                        prompt=prompt,
                        images=[bytes(image_bytes)]
                    )
                    text2 = extract_text_from_response(resp2)
                    if text2:
                        await update.message.reply_text(f"🛒 المنتج المحتمل:\n{text2}")
                        return
                except Exception as e:
                    logger.info("Bytes-based generate_text failed for %s: %s", model_name, repr(e))

        # فشل كل الموديلات
        await update.message.reply_text(
            "❌ لم أتمكن من استخراج اسم المنتج من الصورة. حاول صورة أو وصف أو أعد المحاولة لاحقًا."
        )
        logger.warning(
            "All Gemini attempts failed. tried=%s telegram_file_url=%s", tried, bool(telegram_file_url)
        )

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

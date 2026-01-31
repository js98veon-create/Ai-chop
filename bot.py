# bot.py
import os
import logging
import traceback
from typing import Optional, Any

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# OpenAI modern client
from openai import OpenAI

# ---------- config & logging ----------
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("shop-ai-openai")

BOT_TOKEN = os.getenv("BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN not set in environment")
if not OPENAI_API_KEY:
    raise RuntimeError("OPENAI_API_KEY not set in environment")

# Create OpenAI client
openai_client = OpenAI(api_key=OPENAI_API_KEY)

# Models to try (order = preferred -> fallback)
MODEL_CANDIDATES = [
    "gpt-4.1",           # good vision model (try first)
    "gpt-4o-mini",       # cheaper fallback (may support images)
    "gpt-4o",            # another fallback if available
]

# conservative size limit (bytes)
MAX_IMAGE_BYTES = 6 * 1024 * 1024  # 6 MB

# ---------- helpers ----------
def extract_text_from_response(resp: Any) -> str:
    """
    Robust extraction of textual answer from OpenAI Responses result.
    Tries multiple possible fields to find human text.
    """
    if resp is None:
        return ""
    # preferred direct field (may exist)
    try:
        if hasattr(resp, "output_text") and resp.output_text:
            return str(resp.output_text).strip()
    except Exception:
        pass

    # older/newer shapes: resp.output -> list of message parts
    try:
        out = getattr(resp, "output", None)
        if out and isinstance(out, (list, tuple)) and len(out) > 0:
            # iterate and collect text fragments
            fragments = []
            for item in out:
                # item may be dict-like or object-like
                text = None
                if isinstance(item, dict):
                    # check common shapes
                    if "content" in item:
                        # content can be list
                        c = item["content"]
                        if isinstance(c, list):
                            for part in c:
                                if isinstance(part, dict) and part.get("type") == "output_text":
                                    text = part.get("text")
                                    if text:
                                        fragments.append(text)
                                elif isinstance(part, str):
                                    fragments.append(part)
                        elif isinstance(c, str):
                            fragments.append(c)
                    elif "text" in item:
                        fragments.append(item["text"])
                else:
                    # object-like: try attributes
                    txt = getattr(item, "text", None) or getattr(item, "content", None)
                    if txt:
                        fragments.append(str(txt))
            if fragments:
                return "\n".join([f for f in fragments if f]).strip()
    except Exception:
        pass

    # fallback to string conversion
    try:
        return str(resp).strip()
    except Exception:
        return ""

# ---------- commands ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("✅ البوت يعمل\n📸 أرسل صورة منتج وسأحاول التعرف عليه (OpenAI)")

# ---------- photo handler ----------
async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔍 أفحص الصورة... انتظر لحظة من فضلك")
    try:
        photos = update.message.photo
        if not photos:
            await update.message.reply_text("❌ لم أجد صورة في الرسالة.")
            return

        # choose best candidate (largest that is <= MAX_IMAGE_BYTES)
        selected_file = None
        image_bytes: Optional[bytes] = None
        tried = []

        for idx in range(len(photos) - 1, -1, -1):
            p = photos[idx]
            f = await p.get_file()
            try:
                b = await f.download_as_bytearray()
                size = len(b)
                tried.append((idx, size))
                logger.info("photo idx=%d size=%d bytes", idx, size)
                if size <= MAX_IMAGE_BYTES:
                    selected_file = f
                    image_bytes = b
                    break
            except Exception as e:
                # keep file object for URL fallback
                tried.append((idx, None))
                selected_file = f

        if selected_file is None:
            # fallback to smallest
            fsmall = await photos[0].get_file()
            selected_file = fsmall
            image_bytes = await fsmall.download_as_bytearray()
            tried.append((0, len(image_bytes)))

        # build telegram file URL
        file_path = getattr(selected_file, "file_path", None)
        telegram_file_url = None
        if file_path:
            telegram_file_url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_path}"
            logger.info("Telegram file URL built (truncated): %s", telegram_file_url[:120])

        # concise prompt to minimize cost
        prompt = "Identify the product in this image. Reply with a short product name (2-6 words) or 'Unknown'."

        # Try models
        for model_name in MODEL_CANDIDATES:
            logger.info("Trying model %s", model_name)

            # 1) prefer URL-based image input (input_image)
            if telegram_file_url:
                try:
                    # Build the 'input' structure for the Responses API
                    # Many SDK shapes accept 'input' as list of message dicts.
                    input_payload = [
                        {"role": "user", "content": prompt},
                        {"role": "user", "type": "input_image", "image_url": telegram_file_url},
                    ]
                    resp = openai_client.responses.create(
                        model=model_name,
                        input=input_payload,
                    )
                    text = extract_text_from_response(resp)
                    if text:
                        await update.message.reply_text(f"🛒 المنتج المحتمل:\n{text}")
                        return
                    logger.info("Model %s returned empty text for URL attempt", model_name)
                except Exception as e:
                    logger.warning("URL attempt failed on model %s: %s", model_name, repr(e))

            # 2) try bytes-based fallback (some models accept raw bytes in the SDK)
            if image_bytes:
                try:
                    # bytes-based attempt using input with inline image (some SDKs accept base64 or bytes)
                    # We'll attempt the common shape with 'input' and image as bytes
                    input_payload = [
                        {"role": "user", "content": prompt},
                        {"role": "user", "type": "input_image", "image_url": None},
                    ]
                    # many clients don't accept raw bytes in 'image_url' field.
                    # so we try another signatures; if it fails, exception will be caught.
                    resp2 = openai_client.responses.create(
                        model=model_name,
                        input=input_payload,
                        # pass bytes in "attachments" or similar if supported (some SDKs differ)
                        # This is a best-effort attempt; many environments accept image_url only.
                    )
                    text2 = extract_text_from_response(resp2)
                    if text2:
                        await update.message.reply_text(f"🛒 المنتج المحتمل:\n{text2}")
                        return
                except Exception as e:
                    logger.info("Bytes attempt failed for model %s: %s", model_name, repr(e))

        # all attempts exhausted
        await update.message.reply_text(
            "❌ لم أتمكن من استخراج اسم المنتج من الصورة. حاول صورة أو وصف أو أعد المحاولة لاحقًا."
        )
        logger.warning("All OpenAI attempts failed. Tried: %s telegram_file_url=%s", tried, bool(telegram_file_url))

    except Exception as exc:
        tb = traceback.format_exc()
        logger.error("Unhandled exception in handle_photo: %s\n%s", repr(exc), tb)
        await update.message.reply_text("❌ حدث خطأ أثناء تحليل الصورة")
        print("OpenAI detailed error:", repr(exc))
        print(tb)

# ---------- main ----------
def main():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    logger.info("Bot starting (polling)...")
    app.run_polling(close_loop=False)

if __name__ == "__main__":
    main()

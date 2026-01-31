# bot.py
import os
import logging
import traceback
import base64
from typing import Optional, Any

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

from openai import OpenAI
import requests  # used only for optional fallback upload (if needed)

# ---------- logging ----------
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("shop-ai-base64")

# ---------- env ----------
BOT_TOKEN = os.getenv("BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN not set")
if not OPENAI_API_KEY:
    raise RuntimeError("OPENAI_API_KEY not set")

# ---------- OpenAI client ----------
openai_client = OpenAI(api_key=OPENAI_API_KEY)

# ---------- models to try (preferred first) ----------
MODEL_CANDIDATES = ["gpt-4.1", "gpt-4o-mini"]

# ---------- helpers ----------
def extract_text_from_response(resp: Any) -> str:
    """Extract readable text from different possible response shapes."""
    if resp is None:
        return ""
    # direct output_text
    try:
        if hasattr(resp, "output_text") and resp.output_text:
            return str(resp.output_text).strip()
    except Exception:
        pass
    # try resp.output list
    try:
        out = getattr(resp, "output", None)
        if out and isinstance(out, (list, tuple)):
            parts = []
            for item in out:
                if isinstance(item, dict):
                    content = item.get("content")
                    if isinstance(content, list):
                        for c in content:
                            if isinstance(c, dict) and c.get("type") == "output_text":
                                parts.append(c.get("text", ""))
                            elif isinstance(c, str):
                                parts.append(c)
                    elif isinstance(content, str):
                        parts.append(content)
                    elif "text" in item:
                        parts.append(item["text"])
                else:
                    txt = getattr(item, "text", None) or getattr(item, "content", None)
                    if txt:
                        parts.append(str(txt))
            if parts:
                return "\n".join([p for p in parts if p]).strip()
    except Exception:
        pass
    try:
        return str(resp).strip()
    except Exception:
        return ""

# Optional fallback: upload bytes to 0x0.st (may fail in some networks)
def upload_to_0x0(image_bytes: bytes) -> Optional[str]:
    try:
        files = {"file": ("image.jpg", image_bytes, "image/jpeg")}
        r = requests.post("https://0x0.st", files=files, timeout=30)
        if r.status_code == 200 and r.text.strip().startswith("http"):
            return r.text.strip()
    except Exception as e:
        logger.info("upload_to_0x0 failed: %s", repr(e))
    return None

# ---------- Telegram handlers ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("✅ البوت جاهز — أرسل صورة منتج وسأحاول التعرف عليه")

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔍 أفحص الصورة... انتظر لحظة من فضلك")
    try:
        photos = update.message.photo
        if not photos:
            await update.message.reply_text("❌ لم أجد صورة في الرسالة.")
            return

        # اختر أعلى دقة (آخر عنصر)، حمّل الـ bytes
        photo = photos[-1]
        tfile = await photo.get_file()
        image_bytes = await tfile.download_as_bytearray()
        image_bytes = bytes(image_bytes)  # ensure bytes type

        # Encode to base64 (small memory overhead)
        try:
            image_b64 = base64.b64encode(image_bytes).decode("utf-8")
        except Exception as e:
            logger.exception("Base64 encode failed: %s", repr(e))
            image_b64 = None

        prompt = "Identify the product in this image. Reply with a short product name (2-6 words) or 'Unknown'."

        # Attempt 1: send as input_image with image_base64 (preferred)
        if image_b64:
            for model_name in MODEL_CANDIDATES:
                try:
                    logger.info("Trying model %s with image_base64", model_name)
                    input_payload = [
                        {"role": "user", "content": prompt},
                        {"role": "user", "type": "input_image", "image_base64": image_b64},
                    ]
                    resp = openai_client.responses.create(model=model_name, input=input_payload)
                    text = extract_text_from_response(resp)
                    if text:
                        await update.message.reply_text(f"🛒 المنتج المحتمل:\n{text}")
                        return
                    logger.info("model %s returned empty for base64 attempt", model_name)
                except Exception as e:
                    # record and continue to next model/fallback
                    logger.exception("Base64 attempt failed for %s: %s", model_name, repr(e))

        # Attempt 2: fallback — try using Telegram file URL textually in prompt
        try:
            # Try to get file_path; sometimes Telegram returns it
            file_path = getattr(tfile, "file_path", None)
            if file_path:
                telegram_file_url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_path}"
            else:
                telegram_file_url = None
        except Exception:
            telegram_file_url = None

        if telegram_file_url:
            # try sending prompt with URL in text (some models accept and fetch it)
            for model_name in MODEL_CANDIDATES:
                try:
                    logger.info("Trying model %s with telegram_file_url text prompt", model_name)
                    text_prompt = f"{prompt}\nImage URL: {telegram_file_url}"
                    resp2 = openai_client.responses.create(model=model_name, input=[{"role":"user","content":text_prompt}])
                    text2 = extract_text_from_response(resp2)
                    if text2:
                        await update.message.reply_text(f"🛒 المنتج المحتمل:\n{text2}")
                        return
                except Exception as e:
                    logger.exception("URL-in-text attempt failed for %s: %s", model_name, repr(e))

        # Attempt 3: upload to 0x0.st then try URL (best-effort)
        try:
            uploaded_url = upload_to_0x0(image_bytes)
            if uploaded_url:
                for model_name in MODEL_CANDIDATES:
                    try:
                        logger.info("Trying model %s with uploaded URL %s", model_name, uploaded_url)
                        text_prompt = f"{prompt}\nImage URL: {uploaded_url}"
                        resp3 = openai_client.responses.create(model=model_name, input=[{"role":"user","content":text_prompt}])
                        text3 = extract_text_from_response(resp3)
                        if text3:
                            await update.message.reply_text(f"🛒 المنتج المحتمل:\n{text3}")
                            return
                    except Exception as e:
                        logger.exception("Uploaded-URL attempt failed for %s: %s", model_name, repr(e))
        except Exception as e:
            logger.info("upload_to_0x0 raised: %s", repr(e))

        # If we reach here — all attempts failed
        await update.message.reply_text("❌ لم أتمكن من استخراج اسم المنتج من الصورة. حاول صورة أو وصف أو أعد المحاولة لاحقًا.")
        logger.warning("All OpenAI attempts failed. Check logs for details.")

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

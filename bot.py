# bot.py
import os
import logging
import traceback
import asyncio
from typing import Optional, Any

import requests

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

from openai import OpenAI

# ---------- config & logging ----------
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("shop-ai-uploader")

BOT_TOKEN = os.getenv("BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN not set")
if not OPENAI_API_KEY:
    raise RuntimeError("OPENAI_API_KEY not set")

# OpenAI client
openai_client = OpenAI(api_key=OPENAI_API_KEY)

# prefer these models (order: preferred -> fallback)
MODEL_CANDIDATES = [
    "gpt-4.1",       # good vision-capable model (if available on your account)
    "gpt-4o-mini",   # cheaper fallback (may support images)
    "gpt-4o",        # another fallback
]

MAX_IMAGE_BYTES = 8 * 1024 * 1024  # 8 MB (we'll try to pick a smaller variant if possible)

# ---------- helpers: upload to anonymous hosts ----------
def upload_to_0x0(image_bytes: bytes, filename: str = "image.jpg", timeout: int = 30) -> Optional[str]:
    """
    Upload bytes to https://0x0.st (anonymous). Returns URL string or None.
    """
    try:
        files = {"file": (filename, image_bytes, "image/jpeg")}
        r = requests.post("https://0x0.st", files=files, timeout=timeout)
        if r.status_code == 200:
            url = r.text.strip()
            if url.startswith("http"):
                logger.info("Uploaded to 0x0.st -> %s", url)
                return url
        logger.warning("0x0.st upload failed status=%s text=%s", r.status_code, r.text[:200])
    except Exception as e:
        logger.exception("Exception uploading to 0x0.st: %s", repr(e))
    return None

def upload_to_transfersh(image_bytes: bytes, filename: str = "image.jpg", timeout: int = 30) -> Optional[str]:
    """
    Upload bytes to https://transfer.sh as fallback. Returns URL or None.
    """
    try:
        files = {"file": (filename, image_bytes, "image/jpeg")}
        r = requests.post("https://transfer.sh/", files=files, timeout=timeout)
        if r.status_code in (200, 201):
            url = r.text.strip()
            if url.startswith("http"):
                logger.info("Uploaded to transfer.sh -> %s", url)
                return url
        logger.warning("transfer.sh upload failed status=%s text=%s", r.status_code, r.text[:200])
    except Exception as e:
        logger.exception("Exception uploading to transfer.sh: %s", repr(e))
    return None

async def upload_image_public(image_bytes: bytes) -> Optional[str]:
    """
    Run blocking uploads in threadpool to avoid blocking event loop.
    Tries primary then fallback.
    """
    loop = asyncio.get_event_loop()
    url = await loop.run_in_executor(None, upload_to_0x0, image_bytes)
    if url:
        return url
    # fallback
    url2 = await loop.run_in_executor(None, upload_to_transfersh, image_bytes)
    return url2

# ---------- helpers: parse OpenAI response ----------
def extract_text_from_response(resp: Any) -> str:
    """
    Extract textual output from OpenAI Responses object.
    """
    if resp is None:
        return ""
    # try response.output_text (some SDK versions)
    try:
        if hasattr(resp, "output_text") and resp.output_text:
            return str(resp.output_text).strip()
    except Exception:
        pass

    # try resp.output (list)
    try:
        out = getattr(resp, "output", None)
        if out and isinstance(out, (list, tuple)):
            texts = []
            for item in out:
                if isinstance(item, dict):
                    # content list
                    content = item.get("content")
                    if isinstance(content, list):
                        for c in content:
                            if isinstance(c, dict) and c.get("type") == "output_text":
                                texts.append(c.get("text", ""))
                            elif isinstance(c, str):
                                texts.append(c)
                    elif isinstance(content, str):
                        texts.append(content)
                    elif "text" in item:
                        texts.append(item["text"])
                else:
                    txt = getattr(item, "text", None) or getattr(item, "content", None)
                    if txt:
                        texts.append(str(txt))
            if texts:
                return "\n".join([t for t in texts if t]).strip()
    except Exception:
        pass

    # as last resort:
    try:
        return str(resp).strip()
    except Exception:
        return ""

# ---------- telegram handlers ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("✅ البوت يعمل\n📸 أرسل صورة منتج وسأحاول التعرف عليه (سيتم رفع الصورة مؤقتًا لمعالجة OpenAI).")

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔍 أفحص الصورة... الرجاء الانتظار قليلاً")
    try:
        photos = update.message.photo
        if not photos:
            await update.message.reply_text("❌ لم أجد صورة في الرسالة.")
            return

        # pick best suitable variant (largest <= MAX_IMAGE_BYTES)
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
                logger.info("photo idx=%d size=%d", idx, size)
                if size <= MAX_IMAGE_BYTES:
                    selected_file = f
                    image_bytes = b
                    break
            except Exception as e:
                tried.append((idx, None))
                selected_file = f

        if selected_file is None:
            # fallback to smallest
            fsmall = await photos[0].get_file()
            selected_file = fsmall
            image_bytes = await fsmall.download_as_bytearray()
            tried.append((0, len(image_bytes)))

        # Upload image to public host
        upload_url = None
        if image_bytes:
            upload_url = await upload_image_public(bytes(image_bytes))
        # If upload failed and file_path exists, still try telegram url (may be private; often fails)
        if not upload_url:
            file_path = getattr(selected_file, "file_path", None)
            if file_path:
                upload_url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_path}"
                logger.info("Falling back to telegram file URL (may fail): %s", upload_url[:120])

        if not upload_url:
            await update.message.reply_text("❌ فشل رفع الصورة لمكان عام. حاول إرسال صورة أصغر أو أعد المحاولة لاحقًا.")
            logger.error("No upload URL obtained. Tried=%s", tried)
            return

        # Build prompt and call OpenAI Responses API
        prompt = "Identify the product in this image. Reply with a short product name (2-6 words) or 'Unknown'."

        # Try models sequentially
        for model_name in MODEL_CANDIDATES:
            try:
                logger.info("Calling OpenAI model=%s with image_url=%s", model_name, upload_url[:120])
                input_payload = [
                    {"role": "user", "content": prompt},
                    {"role": "user", "type": "input_image", "image_url": upload_url},
                ]
                resp = openai_client.responses.create(
                    model=model_name,
                    input=input_payload,
                )
                text = extract_text_from_response(resp)
                if text:
                    await update.message.reply_text(f"🛒 المنتج المحتمل:\n{text}")
                    return
                logger.info("Model %s returned empty text; trying next model", model_name)
            except Exception as e:
                logger.exception("OpenAI call failed for model %s: %s", model_name, repr(e))

        # If we reach here, all failed
        await update.message.reply_text("❌ لم أتمكن من استخراج اسم المنتج من الصورة. حاول صورة أو وصف أو أعد المحاولة لاحقًا.")
        logger.warning("All OpenAI attempts failed. Tried models: %s upload_url=%s tried=%s", MODEL_CANDIDATES, upload_url, tried)

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

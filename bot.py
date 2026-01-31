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

# ---------- إعدادات logging ----------
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("shop-ai-bot")

# ---------- متغيرات البيئة ----------
BOT_TOKEN = os.getenv("BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN not set")
if not OPENAI_API_KEY:
    raise RuntimeError("OPENAI_API_KEY not set")

# ---------- إنشاء عميل OpenAI ----------
openai_client = OpenAI(api_key=OPENAI_API_KEY)

# ---------- النماذج التي سنجربها ----------
MODEL_CANDIDATES = [
    "gpt-4.1",       # النموذج الأساسي للصور
    "gpt-4o-mini",   # أرخص كنسخة احتياطية
]

MAX_IMAGE_BYTES = 8 * 1024 * 1024  # أقصى حجم للصورة (8 ميجا)

# ---------- رفع الصور ----------
def upload_to_0x0(image_bytes: bytes, filename: str = "image.jpg") -> Optional[str]:
    """رفع الصورة إلى 0x0.st"""
    try:
        files = {"file": (filename, image_bytes, "image/jpeg")}
        r = requests.post("https://0x0.st", files=files, timeout=30)
        if r.status_code == 200 and r.text.startswith("http"):
            logger.info("Uploaded to 0x0.st: %s", r.text.strip())
            return r.text.strip()
    except Exception as e:
        logger.exception("0x0.st upload failed: %s", repr(e))
    return None

def upload_to_transfersh(image_bytes: bytes, filename: str = "image.jpg") -> Optional[str]:
    """رفع الصورة إلى transfer.sh كنسخة احتياطية"""
    try:
        files = {"file": (filename, image_bytes, "image/jpeg")}
        r = requests.post("https://transfer.sh/", files=files, timeout=30)
        if r.status_code in (200, 201) and r.text.startswith("http"):
            logger.info("Uploaded to transfer.sh: %s", r.text.strip())
            return r.text.strip()
    except Exception as e:
        logger.exception("transfer.sh upload failed: %s", repr(e))
    return None

async def upload_image_public(image_bytes: bytes) -> Optional[str]:
    """رفع الصورة في threadpool لتجنب حجب event loop"""
    loop = asyncio.get_event_loop()
    url = await loop.run_in_executor(None, upload_to_0x0, image_bytes)
    if url:
        return url
    return await loop.run_in_executor(None, upload_to_transfersh, image_bytes)

# ---------- استخراج النص من استجابة OpenAI ----------
def extract_text_from_response(resp: Any) -> str:
    """يحاول استخراج نص المنتج من الاستجابة"""
    if resp is None:
        return ""
    try:
        if hasattr(resp, "output_text") and resp.output_text:
            return str(resp.output_text).strip()
    except Exception:
        pass
    try:
        out = getattr(resp, "output", None)
        if out and isinstance(out, (list, tuple)):
            texts = []
            for item in out:
                if isinstance(item, dict):
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
    try:
        return str(resp).strip()
    except Exception:
        return ""

# ---------- أوامر Telegram ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "✅ البوت يعمل!\n📸 أرسل صورة منتج وسأحاول التعرف عليه تلقائيًا."
    )

# ---------- معالجة الصور ----------
async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔍 أفحص الصورة... الرجاء الانتظار قليلاً")
    try:
        photos = update.message.photo
        if not photos:
            await update.message.reply_text("❌ لم أجد صورة في الرسالة.")
            return

        # اختيار أفضل صورة بحجم مناسب
        selected_file = None
        image_bytes: Optional[bytes] = None

        for idx in range(len(photos)-1, -1, -1):
            p = photos[idx]
            f = await p.get_file()
            b = await f.download_as_bytearray()
            if len(b) <= MAX_IMAGE_BYTES:
                selected_file = f
                image_bytes = b
                break
        if not image_bytes:
            fsmall = await photos[0].get_file()
            image_bytes = await fsmall.download_as_bytearray()

        # رفع الصورة لمكان عام
        upload_url = await upload_image_public(image_bytes)
        if not upload_url:
            await update.message.reply_text(
                "❌ فشل رفع الصورة لمكان عام. حاول صورة أصغر أو أعد المحاولة لاحقًا."
            )
            return

        # إعداد prompt
        prompt = "Identify the product in this image. Reply with a short product name (2-6 words) or 'Unknown'."

        # تجربة النماذج
        for model_name in MODEL_CANDIDATES:
            try:
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
            except Exception as e:
                logger.exception("OpenAI call failed for model %s: %s", model_name, repr(e))

        # كل النماذج فشلت
        await update.message.reply_text(
            "❌ لم أتمكن من استخراج اسم المنتج من الصورة. حاول صورة أو وصف أو أعد المحاولة لاحقًا."
        )

    except Exception as exc:
        tb = traceback.format_exc()
        logger.error("Unhandled exception in handle_photo: %s\n%s", repr(exc), tb)
        await update.message.reply_text("❌ حدث خطأ أثناء تحليل الصورة")

# ---------- main ----------
def main():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    logger.info("Bot starting (polling)...")
    app.run_polling(close_loop=False)

if __name__ == "__main__":
    main()

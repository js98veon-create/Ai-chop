# bot.py
import os
import logging
import traceback
import asyncio
from io import BytesIO
from typing import Optional, Any

import requests
from PIL import Image
import cloudinary
import cloudinary.uploader
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters
from openai import OpenAI

# ---------- Logging ----------
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("shop-ai-cloudinary-bot")

# ---------- Environment Variables ----------
BOT_TOKEN = os.getenv("BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
CLOUD_NAME = os.getenv("CLOUDINARY_CLOUD_NAME")
CLOUD_KEY = os.getenv("CLOUDINARY_API_KEY")
CLOUD_SECRET = os.getenv("CLOUDINARY_API_SECRET")

for var in [BOT_TOKEN, OPENAI_API_KEY, CLOUD_NAME, CLOUD_KEY, CLOUD_SECRET]:
    if not var:
        raise RuntimeError(f"{var} is not set")

# ---------- OpenAI Client ----------
openai_client = OpenAI(api_key=OPENAI_API_KEY)

MODEL_CANDIDATES = ["gpt-4.1", "gpt-4o-mini"]
MAX_IMAGE_BYTES = 8 * 1024 * 1024  # 8 MB

# ---------- Cloudinary config ----------
cloudinary.config(
    cloud_name=CLOUD_NAME,
    api_key=CLOUD_KEY,
    api_secret=CLOUD_SECRET
)

# ---------- Compress image ----------
def compress_image(image_bytes: bytes, max_size=1024*1024) -> bytes:
    im = Image.open(BytesIO(image_bytes))
    buffer = BytesIO()
    quality = 95
    while True:
        buffer.seek(0)
        buffer.truncate()
        im.save(buffer, format="JPEG", quality=quality)
        data = buffer.getvalue()
        if len(data) <= max_size or quality <= 20:
            return data
        quality -= 5

# ---------- Upload to Cloudinary ----------
async def upload_to_cloudinary(image_bytes: bytes, filename: str = "image.jpg") -> Optional[str]:
    loop = asyncio.get_event_loop()
    try:
        def upload():
            res = cloudinary.uploader.upload(
                BytesIO(image_bytes),
                public_id=filename.split(".")[0],
                resource_type="image"
            )
            return res.get("secure_url")
        url = await loop.run_in_executor(None, upload)
        logger.info("Uploaded to Cloudinary: %s", url)
        return url
    except Exception as e:
        logger.exception("Cloudinary upload failed: %s", repr(e))
        return None

# ---------- Extract text ----------
def extract_text_from_response(resp: Any) -> str:
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

# ---------- Telegram Handlers ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "✅ البوت جاهز!\n📸 أرسل صورة منتج وسأحاول التعرف عليه."
    )

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔍 أفحص الصورة... الرجاء الانتظار قليلاً")
    try:
        photos = update.message.photo
        if not photos:
            await update.message.reply_text("❌ لم أجد صورة في الرسالة.")
            return

        # اختر أفضل صورة
        selected_file = None
        image_bytes: Optional[bytes] = None
        for idx in range(len(photos)-1, -1, -1):
            p = photos[idx]
            f = await p.get_file()
            b = await f.download_as_bytearray()
            selected_file = f
            image_bytes = b
            break

        if not image_bytes:
            await update.message.reply_text("❌ تعذر تحميل الصورة.")
            return

        # ضغط الصورة
        image_bytes = compress_image(bytes(image_bytes), max_size=1024*1024)

        # رفع الصورة إلى Cloudinary
        upload_url = await upload_to_cloudinary(image_bytes)
        if not upload_url:
            await update.message.reply_text(
                "❌ فشل رفع الصورة لمكان عام. حاول صورة أخرى."
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

        await update.message.reply_text(
            "❌ لم أتمكن من استخراج اسم المنتج من الصورة. حاول صورة أو وصف أو أعد المحاولة لاحقًا."
        )

    except Exception as exc:
        tb = traceback.format_exc()
        logger.error("Unhandled exception: %s\n%s", repr(exc), tb)
        await update.message.reply_text("❌ حدث خطأ أثناء تحليل الصورة")

# ---------- Main ----------
def main():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    logger.info("Bot starting (polling)...")
    app.run_polling(close_loop=False)

if __name__ == "__main__":
    main()

# bot.py
import os
import logging
import base64
from io import BytesIO

from PIL import Image
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from openai import OpenAI

# ---------- logging ----------
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("shop-ai-final")

# ---------- Environment ----------
BOT_TOKEN = os.getenv("BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

if not BOT_TOKEN or not OPENAI_API_KEY:
    raise RuntimeError("BOT_TOKEN and OPENAI_API_KEY must be set in environment")

# ---------- OpenAI Client ----------
openai_client = OpenAI(api_key=OPENAI_API_KEY)

# ---------- Helper: Compress Image ----------
def compress_image(image_bytes: bytes, max_size=500*1024) -> bytes:
    """Compress image to under max_size using Pillow."""
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

# ---------- Helper: Extract text ----------
def extract_text(resp) -> str:
    if hasattr(resp, "output_text") and resp.output_text:
        return str(resp.output_text).strip()
    try:
        out = getattr(resp, "output", None)
        if out:
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
            if parts:
                return "\n".join([p for p in parts if p]).strip()
    except Exception:
        pass
    return str(resp)

# ---------- Telegram Handlers ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "✅ البوت جاهز! أرسل صورة منتج وسأعطيك اسمه مباشرة."
    )

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔍 أفحص الصورة... انتظر لحظة من فضلك")
    try:
        photos = update.message.photo
        if not photos:
            await update.message.reply_text("❌ لم أجد صورة في الرسالة.")
            return

        # أفضل صورة (آخر عنصر)
        photo = photos[-1]
        tfile = await photo.get_file()
        image_bytes = await tfile.download_as_bytearray()
        image_bytes = compress_image(bytes(image_bytes), max_size=500*1024)
        image_b64 = base64.b64encode(image_bytes).decode("utf-8")

        prompt = "Identify the product in this image. Reply with a short product name (2-6 words) or 'Unknown'."

        # نرسل الصورة مباشرة كـ Base64 إلى GPT-4.1
        resp = openai_client.responses.create(
            model="gpt-4.1",
            input=[
                {"role": "user", "content": prompt},
                {"role": "user", "type": "input_image", "image_base64": image_b64}
            ]
        )
        text = extract_text(resp)
        if text:
            await update.message.reply_text(f"🛒 المنتج المحتمل:\n{text}")
        else:
            await update.message.reply_text(
                "❌ لم أتمكن من استخراج اسم المنتج. حاول صورة أو وصف آخر."
            )

    except Exception as e:
        logger.exception("Error processing photo: %s", e)
        await update.message.reply_text("❌ حدث خطأ أثناء معالجة الصورة")

# ---------- Main ----------
def main():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    logger.info("Bot starting (polling)...")
    app.run_polling(close_loop=False)

if __name__ == "__main__":
    main()

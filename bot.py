import os
import io
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)
import google.generativeai as genai

# ===== Gemini setup =====
genai.configure(api_key=os.getenv("GEMINI_API_KEY"))
model = genai.GenerativeModel("gemini-1.5-flash")

# ===== /start =====
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "✅ البوت يعمل\n📸 أرسل صورة منتج وسأحاول التعرف عليه"
    )

# ===== photo handler =====
async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔍 أفحص الصورة...")

    photo = update.message.photo[-1]
    file = await photo.get_file()
    image_bytes = await file.download_as_bytearray()

    prompt = (
        "Identify the product in the image. "
        "Reply with a short product name only."
    )

    response = model.generate_content(
        [
            prompt,
            {
                "mime_type": "image/jpeg",
                "data": bytes(image_bytes),
            },
        ]
    )

    product_name = response.text.strip()
    await update.message.reply_text(f"🛒 المنتج المحتمل:\n{product_name}")

# ===== main =====
def main():
    token = os.getenv("BOT_TOKEN")
    if not token:
        raise RuntimeError("BOT_TOKEN not set")

    app = Application.builder().token(token).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))

    app.run_polling(close_loop=False)

if __name__ == "__main__":
    main()

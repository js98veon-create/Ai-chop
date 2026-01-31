import os
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
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
if not GEMINI_API_KEY:
    raise RuntimeError("GEMINI_API_KEY not set")

genai.configure(api_key=GEMINI_API_KEY)

# Vision model (stable)
model = genai.GenerativeModel(
    model_name="models/gemini-1.0-pro-vision-latest"
)

# ===== /start command =====
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "✅ البوت يعمل بنجاح\n📸 أرسل صورة منتج وسأحاول التعرف عليه"
    )

# ===== photo handler =====
async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔍 أفحص الصورة...")

    # get highest resolution photo
    photo = update.message.photo[-1]
    file = await photo.get_file()
    image_bytes = await file.download_as_bytearray()

    prompt = (
        "Identify the product in the image. "
        "Reply with a short, clear product name only."
    )

    try:
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
        if not product_name:
            product_name = "لم أستطع التعرف على المنتج بدقة"

        await update.message.reply_text(
            f"🛒 المنتج المحتمل:\n{product_name}"
        )

    except Exception as e:
        await update.message.reply_text("❌ حدث خطأ أثناء تحليل الصورة")
        print("Gemini error:", e)

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

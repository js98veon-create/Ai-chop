import os
import logging
import sqlite3
import base64
import aiohttp
import asyncio

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# ================== ENV ==================
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

if not TELEGRAM_TOKEN:
    raise RuntimeError("TELEGRAM_TOKEN is missing")

# ================== LOGGING ==================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("AffiliateBot")

# ================== DATABASE ==================
DB_PATH = "users.db"

def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            language TEXT NOT NULL
        )
        """)
        conn.execute("PRAGMA journal_mode=WAL")

def set_language(user_id: int, lang: str):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO users (user_id, language) VALUES (?, ?)",
            (user_id, lang),
        )

def get_language(user_id: int) -> str:
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.execute(
            "SELECT language FROM users WHERE user_id = ?",
            (user_id,),
        )
        row = cur.fetchone()
        return row[0] if row else "en"

# ================== MARKDOWN ==================
def escape_markdown_v2(text: str) -> str:
    if not text:
        return text
    escape_chars = r"_\*[]()~`>#+-=|{}.!"
    return "".join("\\" + c if c in escape_chars else c for c in text)

# ================== AMAZON ==================
def amazon_link(name: str) -> str:
    query = name.replace(" ", "+")
    return f"https://www.amazon.com/s?k={query}&tag=chop07c-20"

# ================== GEMINI ==================
GEMINI_URL = (
    "https://generativelanguage.googleapis.com/"
    "v1beta/models/gemini-1.5-flash:generateContent"
)

async def identify_product(image_bytes: bytes) -> str | None:
    if not GEMINI_API_KEY:
        logger.error("GEMINI_API_KEY missing")
        return None

    image_b64 = base64.b64encode(image_bytes).decode()

    payload = {
        "contents": [{
            "parts": [
                {"text": "Identify this product and give me ONLY the short commercial name"},
                {"inline_data": {
                    "mime_type": "image/jpeg",
                    "data": image_b64
                }}
            ]
        }]
    }

    headers = {
        "Content-Type": "application/json",
        "x-goog-api-key": GEMINI_API_KEY
    }

    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as session:
            async with session.post(GEMINI_URL, json=payload, headers=headers) as resp:
                if resp.status != 200:
                    logger.error("Gemini error status %s", resp.status)
                    return None

                data = await resp.json()
                text = data["candidates"][0]["content"]["parts"][0]["text"]
                return text.strip()
    except Exception:
        logger.exception("Gemini request failed")
        return None

# ================== UI TEXT ==================
TEXTS = {
    "en": {
        "welcome": "👋 *Welcome!*\n\nSend me a product photo or name.",
        "buy": "🛒 Buy Now",
        "error": "❌ Sorry, I couldn't recognize the product."
    },
    "ar": {
        "welcome": "👋 *مرحباً بك!*\n\nأرسل صورة المنتج أو اسمه.",
        "buy": "🛒 اشتري الآن",
        "error": "❌ لم أتمكن من التعرف على المنتج."
    }
}

# ================== HANDLERS ==================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return

    keyboard = [
        [InlineKeyboardButton("العربية 🇸🇦", callback_data="lang_ar")],
        [InlineKeyboardButton("English 🇺🇸", callback_data="lang_en")],
    ]
    await update.message.reply_text(
        "Choose your language / اختر اللغة",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def language_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    lang = "ar" if query.data == "lang_ar" else "en"
    set_language(query.from_user.id, lang)

    text = escape_markdown_v2(TEXTS[lang]["welcome"])
    await query.edit_message_text(text, parse_mode=ParseMode.MARKDOWN_V2)

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return

    user_id = update.message.from_user.id
    lang = get_language(user_id)
    name = update.message.text.strip()

    link = amazon_link(name)

    text = f"*{escape_markdown_v2(name)}*"
    keyboard = [[InlineKeyboardButton(TEXTS[lang]["buy"], url=link)]]

    await update.message.reply_text(
        text,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode=ParseMode.MARKDOWN_V2
    )

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return

    user_id = update.message.from_user.id
    lang = get_language(user_id)

    photo = update.message.photo[-1]
    file = await photo.get_file()
    image_bytes = await file.download_as_bytearray()

    name = await identify_product(image_bytes)

    if not name:
        await update.message.reply_text(TEXTS[lang]["error"])
        return

    link = amazon_link(name)

    text = f"*{escape_markdown_v2(name)}*"
    keyboard = [[InlineKeyboardButton(TEXTS[lang]["buy"], url=link)]]

    await update.message.reply_text(
        text,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode=ParseMode.MARKDOWN_V2
    )

# ================== MAIN ==================
async def main():
    init_db()

    app = Application.builder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(MessageHandler(filters.StatusUpdate.ALL, lambda *_: None))
    app.add_handler(MessageHandler(filters.ALL, lambda *_: None))
    app.add_handler(MessageHandler(filters.CallbackQueryHandler, language_callback))

    logger.info("Bot started successfully")

    await app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    asyncio.run(main())

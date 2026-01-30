"""
Improved Telegram Gemini Affiliate Bot
- Async Gemini calls using aiohttp (non-blocking)
- Robust logging (console + rotating file)
- Retries with exponential backoff for Gemini requests
- Concurrency limiting for Gemini calls
- /start language selection persisted in SQLite
- /lang command and /health endpoint
- Uses only two environment variables: TELEGRAM_TOKEN, GEMINI_API_KEY
- Designed to run on Railway with drop_pending_updates=True
"""

import os
import logging
from logging.handlers import RotatingFileHandler
import sqlite3
import base64
import urllib.parse
import asyncio
from typing import Optional

import aiohttp
from aiohttp import ClientTimeout
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.constants import ParseMode
from telegram.ext import (
    ApplicationBuilder,
    ContextTypes,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
)

# --------------------------- Configuration & Logging ---------------------------
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

if TELEGRAM_TOKEN is None:
    raise RuntimeError("TELEGRAM_TOKEN environment variable is required")

# Logging: console + rotating file (Railway captures stdout; file is helpful for local debugging)
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
formatter = logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")

console_handler = logging.StreamHandler()
console_handler.setFormatter(formatter)
logger.addHandler(console_handler)

# Rotating file handler (keep small logs)
file_handler = RotatingFileHandler("bot.log", maxBytes=1_000_000, backupCount=3)
file_handler.setFormatter(formatter)
logger.addHandler(file_handler)

# --------------------------- SQLite (language persistence) ---------------------------
DB_PATH = "users.db"

def init_db():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            lang TEXT NOT NULL
        )
        """
    )
    conn.commit()
    conn.close()


def set_user_lang(user_id: int, lang: str):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("REPLACE INTO users (user_id, lang) VALUES (?, ?)", (user_id, lang))
    conn.commit()
    conn.close()


def get_user_lang(user_id: int) -> str:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT lang FROM users WHERE user_id = ?", (user_id,))
    row = cur.fetchone()
    conn.close()
    return row[0] if row else "en"

# --------------------------- Utilities ---------------------------

def escape_markdown_v2(text: str) -> str:
    if not text:
        return text
    escape_chars = r"_\*[]()~`>#+-=|{}.!"
    return "".join(("\" + ch) if ch in escape_chars else ch for ch in text)


def make_amazon_link(name: str) -> str:
    q = urllib.parse.quote_plus(name)
    return f"https://www.amazon.com/s?k={q}&tag=chop07c-20"

# --------------------------- Gemini (async, aiohttp) ---------------------------
GEMINI_ENDPOINT_TEMPLATE = (
    "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
)
GEMINI_MODEL = "gemini-1.5-flash"

# limit concurrent Gemini calls to avoid bursts
GEMINI_SEMAPHORE = asyncio.Semaphore(3)

async def call_gemini_with_image(session: aiohttp.ClientSession, image_bytes: bytes, prompt_text: str) -> Optional[str]:
    if not GEMINI_API_KEY:
        logger.warning("GEMINI_API_KEY not set; skipping Gemini call")
        return None

    url = GEMINI_ENDPOINT_TEMPLATE.format(model=GEMINI_MODEL)
    b64 = base64.b64encode(image_bytes).decode("utf-8")

    payload = {
        "contents": [
            {
                "parts": [
                    {"inline_data": {"mime_type": "image/jpeg", "data": b64}},
                    {"text": prompt_text},
                ]
            }
        ]
    }

    headers = {"Content-Type": "application/json", "x-goog-api-key": GEMINI_API_KEY}

    backoff = 1
    max_attempts = 3
    async with GEMINI_SEMAPHORE:
        for attempt in range(1, max_attempts + 1):
            try:
                timeout = ClientTimeout(total=30)
                async with session.post(url, json=payload, headers=headers, timeout=timeout) as resp:
                    text_status = f"status={resp.status}"
                    logger.info("Gemini request attempt %s %s", attempt, text_status)
                    if resp.status != 200:
                        body = await resp.text()
                        logger.error("Gemini API error: %s -- %s", resp.status, body[:200])
                        # retry on 5xx
                        if 500 <= resp.status < 600 and attempt < max_attempts:
                            await asyncio.sleep(backoff)
                            backoff *= 2
                            continue
                        return None
                    data = await resp.json()
                    text = extract_text_from_gemini_response(data)
                    return text
            except asyncio.TimeoutError:
                logger.warning("Gemini request timed out on attempt %s", attempt)
                if attempt < max_attempts:
                    await asyncio.sleep(backoff)
                    backoff *= 2
                    continue
                return None
            except Exception as e:
                logger.exception("Exception while calling Gemini: %s", e)
                if attempt < max_attempts:
                    await asyncio.sleep(backoff)
                    backoff *= 2
                    continue
                return None


def extract_text_from_gemini_response(data: dict) -> Optional[str]:
    try:
        # try known patterns
        cands = data.get("candidates") or data.get("outputs") or data.get("outputs_candidates")
        if cands and isinstance(cands, list) and len(cands) > 0:
            first = cands[0]
            if isinstance(first, dict):
                cont = first.get("content")
                if cont and isinstance(cont, list):
                    for part in cont:
                        if isinstance(part, dict) and "text" in part:
                            return part["text"].strip()
                        if isinstance(part, str):
                            return part.strip()
                if "text" in first and isinstance(first["text"], str):
                    return first["text"].strip()
        if "output" in data and isinstance(data["output"], dict):
            out = data["output"]
            if "text" in out:
                return out["text"].strip()
        for k in ("response", "result", "text"):
            if k in data and isinstance(data[k], str):
                return data[k].strip()
        # fallback deep search
        def find_first_str(obj):
            if isinstance(obj, str):
                return obj
            if isinstance(obj, dict):
                for v in obj.values():
                    res = find_first_str(v)
                    if res:
                        return res
            if isinstance(obj, list):
                for item in obj:
                    res = find_first_str(item)
                    if res:
                        return res
            return None
        found = find_first_str(data)
        return found.strip() if isinstance(found, str) else None
    except Exception as e:
        logger.exception("Error extracting text from Gemini response: %s", e)
        return None

# --------------------------- Telegram Handlers ---------------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    keyboard = [
        [
            InlineKeyboardButton("العربية 🇸🇦", callback_data="lang:ar"),
            InlineKeyboardButton("English 🇺🇸", callback_data="lang:en"),
        ]
    ]
    lang = get_user_lang(user.id)
    if lang == "ar":
        text = "مرحباً! أرسل صورة منتج أو اكتب اسمه وسأعيد لك رابط أمازون مع زر 'اشتري الآن 🛒'." 
    else:
        text = "Hello! Send a product photo or type a product name and I'll return an Amazon link with a 'Buy Now 🛒' button."
    await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard))


async def lang_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data or ""
    user = query.from_user
    if data.startswith("lang:"):
        chosen = data.split(":", 1)[1]
        set_user_lang(user.id, chosen)
        if chosen == "ar":
            await query.edit_message_text("تم اختيار العربية 🇸🇦. الآن أرسل صورة أو اسم المنتج.")
        else:
            await query.edit_message_text("English 🇺🇸 selected. Now send a product photo or the product name.")


async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    text = (update.message.text or "").strip()
    if not text:
        return
    try:
        link = make_amazon_link(text)
        lang = get_user_lang(user.id)
        bold_name = escape_markdown_v2(text)
        if lang == "ar":
            caption = f"*{bold_name}*
إليك الرابط على أمازون:"
            button_text = "اشتري الآن 🛒"
        else:
            caption = f"*{bold_name}*
Here's the Amazon link:"
            button_text = "Buy Now 🛒"
        keyboard = InlineKeyboardMarkup.from_button(InlineKeyboardButton(button_text, url=link))
        await update.message.reply_text(caption, reply_markup=keyboard, parse_mode=ParseMode.MARKDOWN_V2)
    except Exception as e:
        logger.exception("Error in text_handler: %s", e)
        await update.message.reply_text("Sorry, an error occurred. Please try again later.")


async def photo_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    photos = update.message.photo
    if not photos:
        await update.message.reply_text("No photo found in the message.")
        return

    photo = photos[-1]
    lang = get_user_lang(user.id)

    try:
        file = await context.bot.get_file(photo.file_id)
        # check file size if available (may be None)
        if hasattr(photo, 'file_size') and photo.file_size and photo.file_size > 10_000_000:
            # warn about large size; we still proceed but it's likely slow
            if lang == "ar":
                await update.message.reply_text("الصورة كبيرة جداً؛ قد تستغرق العملية وقتاً أو تفشل. حاول تقليل دقة الصورة.")
            else:
                await update.message.reply_text("Image is very large; processing may be slow or fail. Try reducing image resolution.")

        image_bytes = await file.download_as_bytearray()
        prompt = "Identify this product and give me ONLY the short commercial name"

        session: aiohttp.ClientSession = context.application.bot_data.get('http_session')
        if session is None:
            # fallback to simple failure message
            logger.error("HTTP session not initialized")
            await update.message.reply_text("Internal error: HTTP client not available.")
            return

        product_name = await call_gemini_with_image(session, bytes(image_bytes), prompt)

        if product_name:
            product_name = product_name.strip().splitlines()[0]
            link = make_amazon_link(product_name)
            bold_name = escape_markdown_v2(product_name)
            if lang == "ar":
                caption = f"*{bold_name}*
إليك الرابط على أمازون:"
                button_text = "اشتري الآن 🛒"
            else:
                caption = f"*{bold_name}*
Here's the Amazon link:"
                button_text = "Buy Now 🛒"
            keyboard = InlineKeyboardMarkup.from_button(InlineKeyboardButton(button_text, url=link))
            await update.message.reply_text(caption, reply_markup=keyboard, parse_mode=ParseMode.MARKDOWN_V2)
        else:
            if lang == "ar":
                await update.message.reply_text("عذراً لم أستطع التعرف على المنتج. أرسل اسم المنتج نصياً لأقوم بإنشاء الرابط.")
            else:
                await update.message.reply_text("Sorry, I couldn't identify the product. Send the product name as text and I'll create the link.")
    except Exception as e:
        logger.exception("Error in photo_handler: %s", e)
        if lang == "ar":
            await update.message.reply_text("حدث خطأ أثناء معالجة الصورة. تأكد من وضوح الصورة أو توافر مفتاح Gemini.")
        else:
            await update.message.reply_text("An error occurred while processing the image. Make sure the image is clear or check your Gemini API key.")


async def help_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lang = get_user_lang(update.effective_user.id)
    if lang == "ar":
        await update.message.reply_text("أرسل صورة أو اكتب اسم المنتج وسيتم إنشاء رابط أمازون تلقائياً.")
    else:
        await update.message.reply_text("Send a photo or type a product name and I'll return an Amazon search link.")


async def lang_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # allow users to change language anytime
    keyboard = [
        [
            InlineKeyboardButton("العربية 🇸🇦", callback_data="lang:ar"),
            InlineKeyboardButton("English 🇺🇸", callback_data="lang:en"),
        ]
    ]
    await update.message.reply_text("Choose your language:", reply_markup=InlineKeyboardMarkup(keyboard))


async def health_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("OK")


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.exception("Unhandled exception: %s", context.error)

# --------------------------- App Start ---------------------------

async def on_startup(app):
    # create shared aiohttp session and save to bot_data
    timeout = ClientTimeout(total=60)
    session = aiohttp.ClientSession(timeout=timeout)
    app.bot_data['http_session'] = session
    logger.info("HTTP session created on startup")

async def on_shutdown(app):
    session: aiohttp.ClientSession = app.bot_data.get('http_session')
    if session:
        await session.close()
        logger.info("HTTP session closed on shutdown")


def main():
    init_db()

    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_handler))
    app.add_handler(CommandHandler("lang", lang_command))
    app.add_handler(CommandHandler("health", health_handler))
    app.add_handler(CallbackQueryHandler(lang_callback, pattern=r"^lang:"))
    app.add_handler(MessageHandler(filters.PHOTO, photo_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))

    app.add_error_handler(error_handler)

    # register lifecycle
    app.on_startup.append(on_startup)
    app.on_shutdown.append(on_shutdown)

    logger.info("Starting bot with drop_pending_updates=True")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()


import os
import re
import asyncio
from datetime import datetime
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import (
    ApplicationBuilder, ContextTypes,
    CommandHandler, MessageHandler, filters
)
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.tl.types import (
    MessageMediaDocument, MessageMediaPhoto, DocumentAttributeFilename
)
from motor.motor_asyncio import AsyncIOMotorClient
import humanize
from flask import Flask
import threading

# Load environment variables
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
API_ID = int(os.getenv("API_ID"))
API_HASH = os.getenv("API_HASH")
ADMINS = [int(id.strip()) for id in os.getenv("ADMINS").split(",")]
DATABASE_URL = os.getenv("DATABASE_URL")

# MongoDB setup
mongo_client = AsyncIOMotorClient(DATABASE_URL)
db = mongo_client.telegram_bot
sessions = db.sessions

# Global states
USER_STATES = {}
ACTIVE_CLIENTS = {}
PROGRESS_MESSAGES = {}

# Helper: Extract chat ID and message ID from link
def parse_tg_link(link):
    pattern = r"https://t\.me/c/(\d+)/(\d+)"
    match = re.match(pattern, link)
    if match:
        return int(match.group(1)), int(match.group(2))
    return None, None

# Improved progress callback with speed
async def progress_callback(current, total, bot, chat_id, start_time):
    progress = current / total * 100
    elapsed = datetime.now() - start_time
    speed = humanize.naturalsize(current / elapsed.total_seconds()) + "/s" if elapsed.total_seconds() > 0 else ""
    
    message = (
        f"📥 Downloading...\n"
        f"Progress: {progress:.1f}%\n"
        f"Size: {humanize.naturalsize(total)}\n"
        f"Speed: {speed}"
    )
    
    if chat_id in PROGRESS_MESSAGES and PROGRESS_MESSAGES[chat_id] is not None:
        try:
            await bot.edit_message_text(
                chat_id=chat_id,
                message_id=PROGRESS_MESSAGES[chat_id],
                text=message
            )
        except:
            pass
    else:
        msg = await bot.send_message(chat_id=chat_id, text=message)
        PROGRESS_MESSAGES[chat_id] = msg.message_id

# Get filename safely
def get_filename(media):
    if isinstance(media, MessageMediaDocument):
        for attr in media.document.attributes:
            if isinstance(attr, DocumentAttributeFilename):
                return attr.file_name
    elif isinstance(media, MessageMediaPhoto):
        return f"photo_{datetime.now().timestamp()}.jpg"
    return f"file_{datetime.now().timestamp()}"

# Bot Commands
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMINS:
        return
    await update.message.reply_text("🔑 Welcome! Use /login to start.")

async def login(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in ADMINS:
        return

    USER_STATES[user_id] = "AWAITING_PHONE"
    await update.message.reply_text("📱 Please send your phone number (e.g., +1234567890):")

async def logout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id in ACTIVE_CLIENTS:
        await ACTIVE_CLIENTS[user_id].disconnect()
        del ACTIVE_CLIENTS[user_id]
    await sessions.delete_one({"user_id": user_id})
    await update.message.reply_text("🔒 Session terminated.")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in ADMINS:
        return

    state = USER_STATES.get(user_id)

    # Login flow
    if state == "AWAITING_PHONE":
        phone = update.message.text
        client = TelegramClient(StringSession(), API_ID, API_HASH)
        await client.connect()
        sent_code = await client.send_code_request(phone)
        USER_STATES[user_id] = {
            "phone": phone,
            "sent_code": sent_code,
            "client": client
        }
        await update.message.reply_text("🔒 Enter the Telegram OTP you received:")

    elif isinstance(state, dict) and "sent_code" in state:
        otp = update.message.text
        client = state["client"]
        try:
            await client.sign_in(state["phone"], code=otp)
            session_string = client.session.save()

            await sessions.update_one(
                {"user_id": user_id},
                {"$set": {"session": session_string}},
                upsert=True
            )

            ACTIVE_CLIENTS[user_id] = client
            await update.message.reply_text("✅ Login successful!")
            USER_STATES[user_id] = None
        except Exception as e:
            await update.message.reply_text(f"❌ Error: {str(e)}")

    # Message link handling
    elif update.message.text.startswith("https://t.me/c/"):
        links = update.message.text.strip().split("\n")
        if len(links) != 2:
            await update.message.reply_text("❌ Send exactly 2 message links (start and end).")
            return

        chat_id, start_id = parse_tg_link(links[0])
        _, end_id = parse_tg_link(links[1])
        client = ACTIVE_CLIENTS.get(user_id)

        if not client:
            await update.message.reply_text("❌ Not logged in! Use /login first.")
            return

        messages = []
        async for msg in client.iter_messages(chat_id, min_id=start_id, max_id=end_id):
            if msg.media:
                messages.append(msg)

        for msg in reversed(messages):
            filename = get_filename(msg.media)
            start_time = datetime.now()
            PROGRESS_MESSAGES[user_id] = None

            try:
                file_path = await client.download_media(
                    msg.media,
                    progress_callback=lambda c, t: asyncio.create_task(
                        progress_callback(c, t, context.bot, user_id, start_time)
                    )
                )

                if PROGRESS_MESSAGES.get(user_id):
                    await context.bot.delete_message(
                        chat_id=user_id,
                        message_id=PROGRESS_MESSAGES[user_id]
                    )
                    del PROGRESS_MESSAGES[user_id]

                await update.message.reply_text(f"✅ Saved: {filename}")
                await context.bot.send_document(
                    chat_id=user_id,
                    document=file_path,
                    filename=filename
                )

            except Exception as e:
                await update.message.reply_text(f"❌ Failed to download {filename}: {str(e)}")

# Restore sessions from MongoDB
async def restore_sessions():
    async for doc in sessions.find():
        try:
            user_id = doc["user_id"]
            session_string = doc["session"]
            client = TelegramClient(StringSession(session_string), API_ID, API_HASH)
            await client.connect()

            if await client.is_user_authorized():
                ACTIVE_CLIENTS[user_id] = client
        except Exception as e:
            print(f"Failed to restore session for {user_id}: {str(e)}")

# Flask app for keeping the service alive
app = Flask(__name__)
@app.route("/")
def home():
    return "Bot is running!"

def run_flask():
    app.run(host="0.0.0.0", port=8080)

def main():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    application = ApplicationBuilder().token(BOT_TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("login", login))
    application.add_handler(CommandHandler("logout", logout))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    loop.run_until_complete(restore_sessions())

    threading.Thread(target=run_flask).start()
    application.run_polling()

if __name__ == "__main__":
    main()

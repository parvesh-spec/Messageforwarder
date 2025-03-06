import sys
import logging
import json
import time
from threading import Thread
import psycopg2
from psycopg2.extras import DictCursor
import os
import asyncio
from telethon import TelegramClient, events
from telethon.errors import SessionPasswordNeededError, AuthKeyUnregisteredError
from telethon.sessions import StringSession

# Add stream handler to output logs to console
handler = logging.StreamHandler(sys.stdout)
handler.setLevel(logging.DEBUG)
formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
handler.setFormatter(formatter)
logger = logging.getLogger(__name__)
logger.addHandler(handler)
logger.setLevel(logging.DEBUG)

# Message ID mapping dictionary
MESSAGE_IDS = {}  # Will store source_msg_id: destination_msg_id mapping

# Telegram API credentials
API_ID = int(os.getenv('API_ID', '27202142'))
API_HASH = os.getenv('API_HASH', 'db4dd0d95dc68d46b77518bf997ed165')

# Define source and destination channels
SOURCE_CHANNEL = None
DESTINATION_CHANNEL = None

# Text replacement dictionary - now per user
TEXT_REPLACEMENTS = {}
CURRENT_USER_ID = None

def get_db():
    conn = psycopg2.connect(
        os.getenv('DATABASE_URL'),
        application_name='telegram_bot_main'
    )
    conn.autocommit = True  # Prevent transaction locks
    return conn

def load_channel_config():
    global SOURCE_CHANNEL, DESTINATION_CHANNEL
    try:
        conn = get_db()
        try:
            with conn.cursor(cursor_factory=DictCursor) as cur:
                cur.execute("SELECT source_channel, destination_channel FROM channel_config ORDER BY updated_at DESC LIMIT 1")
                row = cur.fetchone()

                if row:
                    SOURCE_CHANNEL = row['source_channel']
                    DESTINATION_CHANNEL = row['destination_channel']
                    logger.info(f"📱 Loaded channel config: Source={SOURCE_CHANNEL}, Dest={DESTINATION_CHANNEL}")
                else:
                    logger.warning("❌ No channel configuration found in database")
        finally:
            conn.close()
    except Exception as e:
        logger.error(f"❌ Error loading channel config: {str(e)}")

def get_user_id_by_phone(phone):
    try:
        conn = get_db()
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM users WHERE phone = %s", (phone,))
            result = cur.fetchone()
            if result:
                return result[0]
    except Exception as e:
        logger.error(f"❌ Error getting user ID for phone {phone}: {str(e)}")
    return None

def load_user_replacements(user_id):
    global TEXT_REPLACEMENTS, CURRENT_USER_ID
    try:
        CURRENT_USER_ID = user_id
        conn = get_db()
        try:
            with conn.cursor(cursor_factory=DictCursor) as cur:
                cur.execute("""
                    SELECT original_text, replacement_text 
                    FROM text_replacements 
                    WHERE user_id = %s
                    ORDER BY LENGTH(original_text) DESC
                """, (user_id,))
                TEXT_REPLACEMENTS = {row['original_text']: row['replacement_text'] for row in cur.fetchall()}

                logger.info(f"🔄 Loading replacements for user {user_id}")
                logger.info(f"📚 Found {len(TEXT_REPLACEMENTS)} replacements")
                for original, replacement in TEXT_REPLACEMENTS.items():
                    logger.info(f"📝 Loaded: '{original}' → '{replacement}'")
        finally:
            conn.close()
    except Exception as e:
        logger.error(f"❌ Error loading replacements: {str(e)}")
        TEXT_REPLACEMENTS = {}

def apply_text_replacements(text):
    if not text or not TEXT_REPLACEMENTS:
        logger.info(f"❌ No replacements possible - Text: '{text}', Replacements: {bool(TEXT_REPLACEMENTS)}")
        return text

    logger.info(f"🔄 Processing text: '{text}'")
    logger.info(f"📚 Using {len(TEXT_REPLACEMENTS)} replacements")

    result = text
    for original, replacement in sorted(TEXT_REPLACEMENTS.items(), key=lambda x: len(x[0]), reverse=True):
        if original in result:
            old_text = result
            result = result.replace(original, replacement)
            logger.info(f"✅ Replaced '{original}' with '{replacement}'")
            logger.info(f"📝 Changed: '{old_text}' → '{result}'")

    return result

async def main():
    try:
        # Start the client with better connection handling
        logger.info("🔄 Starting Telegram client...")

        # Initialize client with more robust settings
        client = TelegramClient(
            'anon',
            API_ID,
            API_HASH,
            device_model="Replit Bot",
            system_version="Linux",
            app_version="1.0",
            connection_retries=None  # Infinite retries
        )

        try:
            await client.connect()

            # Check authorization
            if not await client.is_user_authorized():
                logger.error("❌ Bot not authorized. Please authenticate via web interface.")
                return

            # Get session phone number
            me = await client.get_me()
            logger.info(f"✅ Logged in as {me.first_name} (ID: {me.id})")

            session_phone = None
            try:
                with open('anon.session', 'rb') as f:
                    f.seek(20)  # Skip version and DC ID
                    phone_len = int.from_bytes(f.read(1), 'little')
                    if phone_len > 0:
                        phone_bytes = f.read(phone_len)
                        session_phone = phone_bytes.decode('utf-8')
                        if not session_phone.startswith('+'):
                            session_phone = f"+{session_phone}"
                        logger.info(f"📱 Session phone: {session_phone}")
            except Exception as e:
                logger.error(f"❌ Error reading session: {str(e)}")

            if session_phone:
                user_id = get_user_id_by_phone(session_phone)
                if user_id:
                    logger.info(f"👤 Found user ID {user_id}")
                    load_user_replacements(user_id)
                else:
                    logger.warning(f"❌ No user ID for {session_phone}")

            @client.on(events.NewMessage())
            async def forward_handler(event):
                try:
                    if not SOURCE_CHANNEL or not DESTINATION_CHANNEL:
                        return

                    # Format channel IDs
                    source_id = str(SOURCE_CHANNEL)
                    if not source_id.startswith('-100'):
                        source_id = f"-100{source_id.lstrip('-')}"

                    chat_id = str(event.chat_id)
                    if not chat_id.startswith('-100'):
                        chat_id = f"-100{chat_id.lstrip('-')}"

                    if chat_id != source_id:
                        return

                    logger.info(f"📨 Got message from source channel")

                    try:
                        # Get message text and apply replacements
                        message_text = event.message.text if event.message.text else ""
                        if message_text:
                            message_text = apply_text_replacements(message_text)

                        # Send to destination
                        dest_id = str(DESTINATION_CHANNEL)
                        if not dest_id.startswith('-100'):
                            dest_id = f"-100{dest_id.lstrip('-')}"

                        dest_channel = await client.get_entity(int(dest_id))
                        sent_message = await client.send_message(
                            dest_channel,
                            message_text,
                            formatting_entities=event.message.entities
                        )

                        # Store mapping
                        MESSAGE_IDS[event.message.id] = sent_message.id
                        logger.info("✅ Message forwarded successfully")

                    except Exception as e:
                        logger.error(f"❌ Forward error: {str(e)}")

                except Exception as e:
                    logger.error(f"❌ Handler error: {str(e)}")

            @client.on(events.MessageEdited())
            async def edit_handler(event):
                try:
                    if not SOURCE_CHANNEL or not DESTINATION_CHANNEL:
                        return

                    # Check if message is from source channel
                    source_id = str(SOURCE_CHANNEL)
                    chat_id = str(event.chat_id)

                    if not source_id.startswith('-100'):
                        source_id = f"-100{source_id.lstrip('-')}"
                    if not chat_id.startswith('-100'):
                        chat_id = f"-100{chat_id.lstrip('-')}"

                    if chat_id != source_id:
                        return

                    if event.message.id not in MESSAGE_IDS:
                        return

                    # Get edited text and apply replacements
                    message_text = event.message.text if event.message.text else ""
                    if message_text:
                        message_text = apply_text_replacements(message_text)

                    # Update in destination
                    try:
                        dest_id = str(DESTINATION_CHANNEL)
                        if not dest_id.startswith('-100'):
                            dest_id = f"-100{dest_id.lstrip('-')}"

                        channel = await client.get_entity(int(dest_id))
                        await client.edit_message(
                            channel,
                            MESSAGE_IDS[event.message.id],
                            text=message_text,
                            formatting_entities=event.message.entities
                        )
                        logger.info("✅ Message edited successfully")

                    except Exception as e:
                        logger.error(f"❌ Edit error: {str(e)}")

                except Exception as e:
                    logger.error(f"❌ Handler error: {str(e)}")

            # Start config monitor
            Thread(target=config_monitor, daemon=True).start()
            logger.info("✅ Started config monitor")

            # Log initial state
            logger.info("\n🤖 Bot is running")
            logger.info(f"📱 Source channel: {SOURCE_CHANNEL}")
            logger.info(f"📱 Destination: {DESTINATION_CHANNEL}")

            # Run the client
            await client.run_until_disconnected()

        except Exception as e:
            logger.error(f"❌ Client error: {str(e)}")
            if client and client.connected:
                await client.disconnect()
            raise

    except Exception as e:
        logger.error(f"❌ Critical error: {str(e)}")
        raise

def config_monitor():
    while True:
        try:
            load_channel_config()
            if CURRENT_USER_ID:
                load_user_replacements(CURRENT_USER_ID)
            time.sleep(30)
        except Exception as e:
            logger.error(f"❌ Monitor error: {str(e)}")
            time.sleep(1)

if __name__ == "__main__":
    # Clean up old session if exists
    if os.path.exists('anon.session-journal'):
        try:
            os.remove('anon.session-journal')
            logger.info("✅ Cleaned old session journal")
        except Exception as e:
            logger.error(f"❌ Cleanup error: {str(e)}")

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("\n👋 Bot stopped by user")
    except Exception as e:
        logger.error(f"❌ Startup error: {str(e)}")
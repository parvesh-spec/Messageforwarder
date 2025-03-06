import sys
import logging
import json
import time
from threading import Thread
import psycopg2
from psycopg2.extras import DictCursor
import os

# Add stream handler to output logs to console
handler = logging.StreamHandler(sys.stdout)
handler.setLevel(logging.DEBUG)
formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
handler.setFormatter(formatter)
logger = logging.getLogger(__name__)
logger.addHandler(handler)
logger.setLevel(logging.DEBUG)

# Rest of the imports
from telethon import TelegramClient, events
from telethon.errors import SessionPasswordNeededError
import asyncio

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
                # Create channels table if it doesn't exist
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS channel_config (
                        id SERIAL PRIMARY KEY,
                        source_channel TEXT NOT NULL,
                        destination_channel TEXT NOT NULL,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                """)

                # Get the latest channel configuration
                cur.execute("SELECT source_channel, destination_channel FROM channel_config ORDER BY updated_at DESC LIMIT 1")
                row = cur.fetchone()

                if row:
                    SOURCE_CHANNEL = row['source_channel']
                    DESTINATION_CHANNEL = row['destination_channel']
                    logger.info(f"Loaded channel configuration from DB - Source: {SOURCE_CHANNEL}, Destination: {DESTINATION_CHANNEL}")
                else:
                    logger.warning("No channel configuration found in database")
        finally:
            conn.close()
    except Exception as e:
        logger.error(f"Error loading channel configuration: {str(e)}")

def get_user_id_by_phone(phone):
    try:
        conn = get_db()
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM users WHERE phone = %s", (phone,))
            result = cur.fetchone()
            if result:
                return result[0]
    except Exception as e:
        logger.error(f"Error getting user ID for phone {phone}: {str(e)}")
    return None

def load_user_replacements(user_id):
    global TEXT_REPLACEMENTS, CURRENT_USER_ID
    try:
        CURRENT_USER_ID = user_id
        conn = get_db()
        try:
            with conn.cursor(cursor_factory=DictCursor) as cur:
                # Get replacements sorted by length
                cur.execute("""
                    SELECT original_text, replacement_text 
                    FROM text_replacements 
                    WHERE user_id = %s
                    ORDER BY LENGTH(original_text) DESC
                """, (user_id,))
                TEXT_REPLACEMENTS = {row['original_text']: row['replacement_text'] for row in cur.fetchall()}

                logger.info(f"🔄 Reloaded text replacements for user {user_id}")
                logger.info(f"📝 Found {len(TEXT_REPLACEMENTS)} replacements")
                for original, replacement in TEXT_REPLACEMENTS.items():
                    logger.info(f"📌 Loaded: '{original}' → '{replacement}'")
        finally:
            conn.close()
    except Exception as e:
        logger.error(f"❌ Error loading text replacements: {str(e)}")
        TEXT_REPLACEMENTS = {}

def apply_text_replacements(text):
    if not text or not TEXT_REPLACEMENTS:
        return text

    logger.info(f"📝 Applying replacements to text: '{text}'")
    logger.info(f"🔍 Using {len(TEXT_REPLACEMENTS)} replacements")

    result = text
    for original, replacement in sorted(TEXT_REPLACEMENTS.items(), key=lambda x: len(x[0]), reverse=True):
        if original in result:
            old_text = result
            result = result.replace(original, replacement)
            logger.info(f"✅ Replaced '{original}' with '{replacement}'")
            logger.info(f"📝 Text changed: '{old_text}' → '{result}'")

    return result

def config_monitor():
    while True:
        try:
            load_channel_config()
            if CURRENT_USER_ID:
                load_user_replacements(CURRENT_USER_ID)
            time.sleep(30)  # Check every 30 seconds
        except Exception as e:
            logger.error(f"Error in config monitor: {str(e)}")
            time.sleep(1)

async def main():
    try:
        # Start config monitoring in background
        Thread(target=config_monitor, daemon=True).start()
        logger.info("Started configuration monitor")

        # Start the client
        logger.info("Starting Telegram client...")
        client = TelegramClient('anon', API_ID, API_HASH)
        await client.start()

        # Check if already authorized
        if not await client.is_user_authorized():
            logger.error("Bot is not authorized. Please run the web interface first to authenticate.")
            return

        # Get information about yourself
        me = await client.get_me()
        logger.info(f"Successfully logged in as {me.first_name} (ID: {me.id})")

        # Get user_id from the phone number in the session file
        session_phone = None
        try:
            with open('anon.session', 'rb') as f:
                # Skip the first 20 bytes which contain version and DC ID
                f.seek(20)
                # Read the phone number length (1 byte)
                phone_len = int.from_bytes(f.read(1), 'little')
                # Read the phone number
                if phone_len > 0:
                    phone_bytes = f.read(phone_len)
                    session_phone = phone_bytes.decode('utf-8')
                    if not session_phone.startswith('+'):
                        session_phone = f"+{session_phone}"
        except Exception as e:
            logger.error(f"Error reading session file: {str(e)}")

        if session_phone:
            user_id = get_user_id_by_phone(session_phone)
            if user_id:
                logger.info(f"Found user ID {user_id} for phone {session_phone}")
                load_user_replacements(user_id)
            else:
                logger.warning(f"No user ID found for phone {session_phone}")

        @client.on(events.NewMessage())
        async def forward_handler(event):
            try:
                # Skip if channels not configured
                if not SOURCE_CHANNEL or not DESTINATION_CHANNEL:
                    logger.warning("Channels not configured yet")
                    return

                # Format source channel ID for comparison
                source_id = str(SOURCE_CHANNEL)
                if not source_id.startswith('-100'):
                    source_id = f"-100{source_id.lstrip('-')}"

                # Format event chat ID for comparison
                chat_id = str(event.chat_id)
                if not chat_id.startswith('-100'):
                    chat_id = f"-100{chat_id.lstrip('-')}"

                # Check if message is from source channel
                if chat_id != source_id:
                    return

                logger.info(f"📨 Processing message from source channel {source_id}")

                try:
                    # Format destination channel ID
                    dest_id = str(DESTINATION_CHANNEL)
                    if not dest_id.startswith('-100'):
                        dest_id = f"-100{dest_id.lstrip('-')}"

                    # Get destination channel entity
                    dest_channel = await client.get_entity(int(dest_id))
                    logger.info(f"📍 Found destination channel: {getattr(dest_channel, 'title', 'Unknown')}")

                    # Create a new message
                    message_text = event.message.text if event.message.text else ""
                    logger.debug(f"📄 Original message: {message_text}")

                    # Apply text replacements
                    if message_text:
                        message_text = apply_text_replacements(message_text)

                    # Send message
                    try:
                        logger.info("📤 Sending message...")
                        sent_message = await client.send_message(
                            dest_channel,
                            message_text,
                            formatting_entities=event.message.entities
                        )
                        logger.info("✅ Message sent successfully")

                        # Store message IDs mapping
                        MESSAGE_IDS[event.message.id] = sent_message.id
                        logger.debug(f"🔗 Mapped message IDs: {event.message.id} → {sent_message.id}")

                    except Exception as e:
                        logger.error(f"❌ Failed to send message: {str(e)}")
                        return

                except ValueError as e:
                    logger.error(f"❌ Failed to access destination channel: {str(e)}")
                    return

            except Exception as e:
                logger.error(f"❌ Error in forward handler: {str(e)}")
                logger.error(f"❌ Error type: {type(e).__name__}")

        @client.on(events.MessageEdited())
        async def edit_handler(event):
            try:
                # Skip if channels not configured
                if not SOURCE_CHANNEL or not DESTINATION_CHANNEL:
                    logger.warning("Channels not configured yet")
                    return

                # Format IDs for comparison
                source_id = str(SOURCE_CHANNEL)
                if not source_id.startswith('-100'):
                    source_id = f"-100{source_id.lstrip('-')}"

                chat_id = str(event.chat_id)
                if not chat_id.startswith('-100'):
                    chat_id = f"-100{chat_id.lstrip('-')}"

                # Check if message is from source channel
                if chat_id != source_id:
                    return

                if event.message.id not in MESSAGE_IDS:
                    logger.info("Original message mapping not found")
                    return

                dest_msg_id = MESSAGE_IDS[event.message.id]
                logger.info(f"Found destination message ID: {dest_msg_id}")

                # Get the edited message content
                message_text = event.message.text if event.message.text else ""

                # Apply text replacements
                if message_text:
                    message_text = apply_text_replacements(message_text)

                try:
                    # Format destination channel ID
                    dest_id = str(DESTINATION_CHANNEL)
                    if not dest_id.startswith('-100'):
                        dest_id = f"-100{dest_id.lstrip('-')}"

                    # Get destination channel entity
                    channel = await client.get_entity(int(dest_id))

                    # Edit the corresponding message
                    logger.info("Updating message in destination channel...")
                    await client.edit_message(
                        channel,
                        dest_msg_id,
                        text=message_text,
                        formatting_entities=event.message.entities
                    )
                    logger.info("Message updated successfully")

                except Exception as e:
                    logger.error(f"Error editing message: {str(e)}")
                    return

            except Exception as e:
                logger.error(f"Error in edit handler: {str(e)}")
                logger.error(f"Error type: {type(e).__name__}")

        logger.info("\nBot is running and monitoring for new messages and edits.")
        logger.info(f"Source channel: {SOURCE_CHANNEL}")
        logger.info(f"Destination channel: {DESTINATION_CHANNEL}")

        # Test text replacement functionality
        test_text = "Hello haha how are you?"
        logger.info("\nTesting text replacement functionality...")
        logger.info(f"Test text: '{test_text}'")
        logger.info(f"Current replacements: {TEXT_REPLACEMENTS}")
        test_result = apply_text_replacements(test_text)
        logger.info(f"Test result: '{test_result}'")

        await client.run_until_disconnected()

    except Exception as e:
        logger.error(f"Critical error in main function: {str(e)}")
        logger.error(f"Error type: {type(e).__name__}")
        raise

if __name__ == "__main__":
    try:
        # Clean up old configuration file if it exists
        if os.path.exists('channel_config.json'):
            try:
                os.remove('channel_config.json')
                logger.info("Removed old channel_config.json file")
            except Exception as e:
                logger.error(f"Error removing channel_config.json: {str(e)}")

        # Start the bot
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("\nBot stopped by user.")
    except Exception as e:
        logger.error(f"An unexpected error occurred: {e}")
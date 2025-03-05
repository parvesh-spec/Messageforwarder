import sys
import logging
import time
from threading import Thread
import psycopg2
from psycopg2.extras import DictCursor
import os
import tempfile
from telethon import TelegramClient, events
from telethon.errors import SessionPasswordNeededError
from telethon.sessions import StringSession
import asyncio

# Configure root logger
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler('telegram_bot.log')
    ]
)

# Create logger for this module
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

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

client = None  # Global client variable

def get_db():
    try:
        conn = psycopg2.connect(
            os.getenv('DATABASE_URL'),
            application_name='telegram_bot_main'
        )
        conn.autocommit = True
        logger.info("Database connection established successfully")
        return conn
    except Exception as e:
        logger.error(f"Database connection error: {str(e)}")
        return None

def load_channel_config():
    global SOURCE_CHANNEL, DESTINATION_CHANNEL
    try:
        if not CURRENT_USER_ID:
            logger.warning("No current user ID set")
            return

        conn = get_db()
        if not conn:
            logger.error("Could not establish database connection")
            return

        try:
            with conn.cursor(cursor_factory=DictCursor) as cur:
                cur.execute("""
                    SELECT source_channel, destination_channel 
                    FROM channel_configs 
                    WHERE user_id = %s
                """, (CURRENT_USER_ID,))
                config = cur.fetchone()

                if config:
                    SOURCE_CHANNEL = config['source_channel']
                    DESTINATION_CHANNEL = config['destination_channel']
                    logger.info(f"Loaded channel configuration - Source: {SOURCE_CHANNEL}, Destination: {DESTINATION_CHANNEL}")
                else:
                    logger.warning("No channel configuration found")
        finally:
            conn.close()
    except Exception as e:
        logger.error(f"Error loading channel configuration: {str(e)}")

def load_user_replacements(user_id):
    global TEXT_REPLACEMENTS, CURRENT_USER_ID
    try:
        CURRENT_USER_ID = user_id
        logger.info(f"Loading text replacements for user {user_id}")

        conn = get_db()
        if not conn:
            logger.error("Could not establish database connection")
            return

        try:
            with conn.cursor(cursor_factory=DictCursor) as cur:
                cur.execute("""
                    SELECT original_text, replacement_text 
                    FROM text_replacements 
                    WHERE user_id = %s
                    ORDER BY LENGTH(original_text) DESC
                """, (user_id,))

                TEXT_REPLACEMENTS = {}
                rows = cur.fetchall()
                logger.info(f"Found {len(rows)} replacements for user {user_id}")

                for row in rows:
                    TEXT_REPLACEMENTS[row['original_text']] = row['replacement_text']
                    logger.debug(f"Loaded replacement: '{row['original_text']}' -> '{row['replacement_text']}'")
        finally:
            conn.close()
    except Exception as e:
        logger.error(f"Error loading text replacements for user {user_id}: {str(e)}")
        TEXT_REPLACEMENTS = {}

async def get_initial_user():
    """Get first user ID from database to use for initial configuration"""
    try:
        conn = get_db()
        if not conn:
            logger.error("Could not establish database connection")
            return None

        try:
            with conn.cursor() as cur:
                cur.execute("SELECT id FROM users LIMIT 1")
                result = cur.fetchone()
                if result:
                    user_id = result[0]
                    logger.info(f"Found initial user ID: {user_id}")
                    return user_id
                logger.warning("No users found in database")
                return None
        finally:
            conn.close()
    except Exception as e:
        logger.error(f"Error getting initial user ID: {str(e)}")
        return None

async def get_session_string(user_id):
    """Get saved session string from database"""
    try:
        conn = get_db()
        if not conn:
            logger.error("Could not establish database connection")
            return None

        try:
            with conn.cursor(cursor_factory=DictCursor) as cur:
                cur.execute("""
                    SELECT session_string 
                    FROM user_sessions 
                    WHERE user_id = %s
                """, (user_id,))
                result = cur.fetchone()
                if result:
                    logger.info("Successfully retrieved session string")
                    return result['session_string']
                logger.warning(f"No session string found for user {user_id}")
                return None
        finally:
            conn.close()
    except Exception as e:
        logger.error(f"Error getting session string: {str(e)}")
        return None

async def start_client(session_string):
    global client
    try:
        logger.info("Initializing Telegram client")
        client = TelegramClient(StringSession(session_string), API_ID, API_HASH)
        await client.connect()
        logger.info("Client connected successfully")

        if not await client.is_user_authorized():
            logger.error("Bot is not authorized. Please authenticate through web interface.")
            return False

        me = await client.get_me()
        logger.info(f"Successfully logged in as {me.first_name} (ID: {me.id})")
        return True
    except Exception as e:
        logger.error(f"Error starting client: {str(e)}")
        return False

async def main():
    try:
        logger.info("Starting Telegram bot service")

        # Get initial user ID for configuration
        global CURRENT_USER_ID
        CURRENT_USER_ID = await get_initial_user()
        if not CURRENT_USER_ID:
            logger.warning("No users found in database")
            return

        # Start config monitoring in background
        Thread(target=lambda: asyncio.run(config_monitor()), daemon=True).start()
        logger.info("Started channel configuration monitor")

        while True:  # Keep trying to connect
            try:
                # Get session string from database
                session_string = await get_session_string(CURRENT_USER_ID)
                if not session_string:
                    logger.error("No session string found in database. Please authenticate through web interface.")
                    await asyncio.sleep(5)  # Wait before retrying
                    continue

                # Start client with session string
                if not await start_client(session_string):
                    await asyncio.sleep(5)  # Wait before retrying
                    continue

                @client.on(events.NewMessage())
                async def forward_handler(event):
                    try:
                        if not SOURCE_CHANNEL or not DESTINATION_CHANNEL:
                            return

                        source_id = str(SOURCE_CHANNEL)
                        if not source_id.startswith('-100'):
                            source_id = f"-100{source_id.lstrip('-')}"

                        chat_id = str(event.chat_id)
                        if not chat_id.startswith('-100'):
                            chat_id = f"-100{chat_id.lstrip('-')}"

                        if chat_id != source_id:
                            return

                        logger.info(f"Processing message from source channel {source_id}")

                        try:
                            dest_id = str(DESTINATION_CHANNEL)
                            if not dest_id.startswith('-100'):
                                dest_id = f"-100{dest_id.lstrip('-')}"

                            dest_channel = await client.get_entity(int(dest_id))
                            message_text = event.message.text if event.message.text else ""

                            if TEXT_REPLACEMENTS and message_text:
                                logger.debug(f"Original message: {message_text}")
                                for original, replacement in sorted(TEXT_REPLACEMENTS.items(), key=lambda x: len(x[0]), reverse=True):
                                    if original in message_text:
                                        message_text = message_text.replace(original, replacement)
                                logger.debug(f"Message after replacements: {message_text}")

                            media = None
                            if event.message.media:
                                logger.info("Downloading media from message")
                                media = await event.message.download_media()

                            try:
                                if media:
                                    logger.info("Forwarding message with media")
                                    sent_message = await client.send_file(
                                        dest_channel,
                                        media,
                                        caption=message_text,
                                        formatting_entities=event.message.entities
                                    )
                                else:
                                    logger.info("Forwarding text message")
                                    sent_message = await client.send_message(
                                        dest_channel,
                                        message_text,
                                        formatting_entities=event.message.entities
                                    )

                                MESSAGE_IDS[event.message.id] = sent_message.id
                                logger.info(f"Message forwarded successfully. ID mapping: {event.message.id} -> {sent_message.id}")

                            except Exception as e:
                                logger.error(f"Failed to send message: {str(e)}")
                                return

                        except ValueError as e:
                            logger.error(f"Failed to access destination channel: {str(e)}")
                            return

                    except Exception as e:
                        logger.error(f"Error in forward handler: {str(e)}")

                @client.on(events.MessageEdited())
                async def edit_handler(event):
                    try:
                        if not SOURCE_CHANNEL or not DESTINATION_CHANNEL:
                            return

                        source_id = str(SOURCE_CHANNEL)
                        if not source_id.startswith('-100'):
                            source_id = f"-100{source_id.lstrip('-')}"

                        chat_id = str(event.chat_id)
                        if not chat_id.startswith('-100'):
                            chat_id = f"-100{chat_id.lstrip('-')}"

                        if chat_id != source_id:
                            return

                        if event.message.id not in MESSAGE_IDS:
                            return

                        logger.info(f"Processing edited message from source channel {source_id}")

                        dest_msg_id = MESSAGE_IDS[event.message.id]
                        message_text = event.message.text if event.message.text else ""

                        if TEXT_REPLACEMENTS and message_text:
                            logger.debug(f"Original edited message: {message_text}")
                            for original, replacement in sorted(TEXT_REPLACEMENTS.items(), key=lambda x: len(x[0]), reverse=True):
                                if original in message_text:
                                    message_text = message_text.replace(original, replacement)
                            logger.debug(f"Edited message after replacements: {message_text}")

                        try:
                            dest_id = str(DESTINATION_CHANNEL)
                            if not dest_id.startswith('-100'):
                                dest_id = f"-100{dest_id.lstrip('-')}"

                            channel = await client.get_entity(int(dest_id))
                            await client.edit_message(
                                channel,
                                dest_msg_id,
                                text=message_text,
                                formatting_entities=event.message.entities
                            )
                            logger.info(f"Message edited successfully. ID: {dest_msg_id}")

                        except Exception as e:
                            logger.error(f"Error editing message: {str(e)}")
                            return

                    except Exception as e:
                        logger.error(f"Error in edit handler: {str(e)}")

                logger.info("Bot is running and monitoring for new messages and edits")
                await client.run_until_disconnected()

            except Exception as e:
                logger.error(f"Connection error: {str(e)}")
                if client and client.is_connected():
                    await client.disconnect()
                await asyncio.sleep(5)  # Wait before retrying

    except Exception as e:
        logger.error(f"Critical error in main function: {str(e)}")
        if client and client.is_connected():
            await client.disconnect()

async def config_monitor():
    while True:
        try:
            load_channel_config()
            if CURRENT_USER_ID:
                load_user_replacements(CURRENT_USER_ID)
            await asyncio.sleep(5)  # Check every 5 seconds
        except Exception as e:
            logger.error(f"Error in config monitor: {str(e)}")
            await asyncio.sleep(1)  # Wait a bit before retrying on error

if __name__ == "__main__":
    try:
        logger.info("Starting bot application")
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bot stopped by user")
    except Exception as e:
        logger.error(f"An unexpected error occurred: {e}")
    finally:
        if client and client.is_connected():
            asyncio.run(client.disconnect())
        logger.info("Bot application shutdown complete")
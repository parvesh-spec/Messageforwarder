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

                if TEXT_REPLACEMENTS:
                    logger.info(f"Loaded {len(TEXT_REPLACEMENTS)} text replacements for user {user_id}")
                    for original, replacement in TEXT_REPLACEMENTS.items():
                        logger.debug(f"Loaded replacement: '{original}' -> '{replacement}'")
                else:
                    logger.warning(f"No text replacements found for user {user_id}")
        finally:
            conn.close()
    except Exception as e:
        logger.error(f"Error loading text replacements: {str(e)}")
        TEXT_REPLACEMENTS = {}

def config_monitor():
    while True:
        try:
            load_channel_config()
            if CURRENT_USER_ID:
                load_user_replacements(CURRENT_USER_ID)
            time.sleep(30)  # Check every 30 seconds instead of 5
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

                logger.info(f"Processing message from source channel {source_id}")

                try:
                    # Format destination channel ID
                    dest_id = str(DESTINATION_CHANNEL)
                    if not dest_id.startswith('-100'):
                        dest_id = f"-100{dest_id.lstrip('-')}"

                    # Get destination channel entity
                    dest_channel = await client.get_entity(int(dest_id))
                    logger.info(f"Found destination channel: {getattr(dest_channel, 'title', 'Unknown')}")

                    # Create a new message
                    message_text = event.message.text if event.message.text else ""
                    logger.debug(f"Original message text: {message_text}")

                    # Apply text replacements if any
                    if TEXT_REPLACEMENTS and message_text:
                        logger.debug("Starting text replacement process...")
                        for original, replacement in sorted(TEXT_REPLACEMENTS.items(), key=lambda x: len(x[0]), reverse=True):
                            if original in message_text:
                                old_text = message_text
                                message_text = message_text.replace(original, replacement)
                                logger.info(f"Replaced '{original}' with '{replacement}'")
                                logger.debug(f"Text changed from '{old_text}' to '{message_text}'")

                    # Handle media
                    media = None
                    if event.message.media:
                        logger.info("Downloading media...")
                        try:
                            media = await event.message.download_media()
                            logger.info(f"Media downloaded: {media}")
                        except Exception as e:
                            logger.error(f"Failed to download media: {str(e)}")
                            return

                    # Send message
                    try:
                        if media:
                            logger.info("Sending message with media...")
                            sent_message = await client.send_file(
                                dest_channel,
                                media,
                                caption=message_text,
                                formatting_entities=event.message.entities
                            )
                            os.remove(media)  # Clean up
                            logger.info("Message with media sent successfully")
                        else:
                            logger.info("Sending text message...")
                            sent_message = await client.send_message(
                                dest_channel,
                                message_text,
                                formatting_entities=event.message.entities
                            )
                            logger.info("Text message sent successfully")

                        # Store message IDs mapping
                        MESSAGE_IDS[event.message.id] = sent_message.id
                        logger.debug(f"Stored message ID mapping: {event.message.id} → {sent_message.id}")

                    except Exception as e:
                        logger.error(f"Failed to send message: {str(e)}")
                        if media and os.path.exists(media):
                            os.remove(media)
                        return

                except ValueError as e:
                    logger.error(f"Failed to access destination channel: {str(e)}")
                    return

            except Exception as e:
                logger.error(f"Error in forward handler: {str(e)}")
                logger.error(f"Error type: {type(e).__name__}")

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

                # Apply text replacements if any
                if TEXT_REPLACEMENTS and message_text:
                    logger.debug("Applying text replacements to edited message...")
                    for original, replacement in sorted(TEXT_REPLACEMENTS.items(), key=lambda x: len(x[0]), reverse=True):
                        if original in message_text:
                            message_text = message_text.replace(original, replacement)
                            logger.info(f"Replaced '{original}' with '{replacement}' in edited message")

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
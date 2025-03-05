import sys
import logging
import time
from threading import Thread
import psycopg2
from psycopg2.extras import DictCursor
import os
from telethon import TelegramClient, events
from telethon.sessions import StringSession
import asyncio

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Database connection with better error handling
def get_db():
    try:
        conn = psycopg2.connect(
            os.getenv('DATABASE_URL'),
            application_name='telegram_bot_main',
            connect_timeout=10
        )
        conn.autocommit = True
        return conn
    except Exception as e:
        logger.error(f"Database connection error: {str(e)}")
        return None

# Message ID mapping dictionary
MESSAGE_IDS = {}

# Telegram API credentials
API_ID = int(os.getenv('API_ID', '27202142'))
API_HASH = os.getenv('API_HASH', 'db4dd0d95dc68d46b77518bf997ed165')

# Channel configuration
SOURCE_CHANNEL = None
DESTINATION_CHANNEL = None

async def get_user_session():
    """Get session string from database for first user"""
    try:
        db = get_db()
        if not db:
            logger.error("Could not connect to database")
            return None

        with db.cursor(cursor_factory=DictCursor) as cur:
            # Get first active user session
            cur.execute("""
                SELECT us.session_string 
                FROM user_sessions us 
                JOIN users u ON us.user_id = u.id 
                LIMIT 1
            """)
            result = cur.fetchone()
            return result['session_string'] if result else None
    except Exception as e:
        logger.error(f"Error getting user session: {str(e)}")
        return None

def load_channel_config():
    global SOURCE_CHANNEL, DESTINATION_CHANNEL
    try:
        if not CURRENT_USER_ID:
            logger.warning("No current user ID set")
            return

        conn = get_db()
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
            if conn:
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

                # Clear existing replacements first
                TEXT_REPLACEMENTS = {}
                rows = cur.fetchall()
                logger.info(f"Found {len(rows)} replacements for user {user_id}")

                for row in rows:
                    TEXT_REPLACEMENTS[row['original_text']] = row['replacement_text']
                    logger.debug(f"Loaded replacement: '{row['original_text']}' -> '{row['replacement_text']}'")

                # Verify data was loaded
                if not TEXT_REPLACEMENTS:
                    logger.warning(f"No text replacements found for user {user_id}")
                else:
                    logger.info(f"Successfully loaded {len(TEXT_REPLACEMENTS)} replacements")
        finally:
            if conn:
                conn.close()
    except Exception as e:
        logger.error(f"Error loading text replacements for user {user_id}: {str(e)}")
        logger.error(f"Error type: {type(e).__name__}")
        TEXT_REPLACEMENTS = {}

def config_monitor():
    while True:
        try:
            load_channel_config()
            if CURRENT_USER_ID:
                load_user_replacements(CURRENT_USER_ID)
            time.sleep(5)  # Check every 5 seconds
        except Exception as e:
            logger.error(f"Error in config monitor: {str(e)}")
            time.sleep(1)  # Wait a bit before retrying on error

# Text replacement dictionary - per user
TEXT_REPLACEMENTS = {}
CURRENT_USER_ID = None

async def main():
    try:
        # Get session string
        session_string = await get_user_session()
        if not session_string:
            logger.error("No session string found. Please authenticate through web interface first.")
            return

        # Initialize client with session
        client = TelegramClient(StringSession(session_string), API_ID, API_HASH)

        try:
            await client.connect()

            if not await client.is_user_authorized():
                logger.error("Bot is not authorized. Please authenticate through web interface.")
                return

            me = await client.get_me()
            logger.info(f"Successfully logged in as {me.first_name} (ID: {me.id})")
            
            # Start config monitoring in background
            Thread(target=config_monitor, daemon=True).start()
            logger.info("Started channel configuration monitor")

            @client.on(events.NewMessage())
            async def forward_handler(event):
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

                    logger.info(f"Processing message from source channel {source_id}")

                    try:
                        # Format destination channel ID
                        dest_id = str(DESTINATION_CHANNEL)
                        if not dest_id.startswith('-100'):
                            dest_id = f"-100{dest_id.lstrip('-')}"

                        # Get destination channel entity
                        dest_channel = await client.get_entity(int(dest_id))

                        # Create a new message
                        message_text = event.message.text if event.message.text else ""

                        # Apply text replacements if any
                        if TEXT_REPLACEMENTS and message_text:
                            logger.debug("Starting text replacement process...")
                            logger.debug(f"Current TEXT_REPLACEMENTS dictionary: {TEXT_REPLACEMENTS}")
                            logger.debug(f"Original text before replacements: {message_text}")

                            # Sort replacements by length (longest first) to handle overlapping patterns
                            for original, replacement in sorted(TEXT_REPLACEMENTS.items(), key=lambda x: len(x[0]), reverse=True):
                                logger.debug(f"Checking replacement: '{original}' -> '{replacement}'")
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
            logger.error(f"Failed to start client: {str(e)}")
            if client and client.is_connected():
                await client.disconnect()

    except Exception as e:
        logger.error(f"Critical error in main function: {str(e)}")
        logger.error(f"Error type: {type(e).__name__}")
        raise

if __name__ == "__main__":
    max_retries = 3
    retry_count = 0

    while retry_count < max_retries:
        try:
            asyncio.run(main())
            break
        except KeyboardInterrupt:
            logger.info("\nBot stopped by user.")
            break
        except Exception as e:
            retry_count += 1
            logger.error(f"An error occurred (attempt {retry_count}/{max_retries}): {e}")
            if retry_count < max_retries:
                time.sleep(5)  # Wait before retrying
            else:
                logger.error("Max retries reached, exiting.")
                sys.exit(1)
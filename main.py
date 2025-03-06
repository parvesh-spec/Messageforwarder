import os
import logging
import json
import time
from threading import Thread, Lock
import psycopg2
from psycopg2.extras import DictCursor
import asyncio
from telethon import TelegramClient, events
from telethon.errors import SessionPasswordNeededError, AuthKeyUnregisteredError
from telethon.sessions import StringSession

# Set up logging
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Global variables
MESSAGE_IDS = {}  # Will store source_msg_id: destination_msg_id mapping
TEXT_REPLACEMENTS = {}
CURRENT_USER_ID = None
SOURCE_CHANNEL = None
DESTINATION_CHANNEL = None
client = None

# Telegram API credentials
API_ID = int(os.getenv('API_ID', '27202142'))
API_HASH = os.getenv('API_HASH', 'db4dd0d95dc68d46b77518bf997ed165')

def get_db():
    try:
        conn = psycopg2.connect(
            os.getenv('DATABASE_URL'),
            application_name='telegram_bot_main'
        )
        conn.autocommit = True
        return conn
    except Exception as e:
        logger.error(f"❌ Database connection error: {str(e)}")
        return None

def load_channel_config():
    """Load channel configuration from database"""
    global SOURCE_CHANNEL, DESTINATION_CHANNEL
    try:
        conn = get_db()
        if not conn:
            return False

        with conn.cursor(cursor_factory=DictCursor) as cur:
            cur.execute("""
                SELECT source_channel, destination_channel 
                FROM channel_config 
                ORDER BY updated_at DESC 
                LIMIT 1
            """)
            result = cur.fetchone()

            if result:
                SOURCE_CHANNEL = result['source_channel']
                DESTINATION_CHANNEL = result['destination_channel']
                logger.info(f"✅ Loaded channels - Source: {SOURCE_CHANNEL}, Dest: {DESTINATION_CHANNEL}")
                return True
            else:
                logger.warning("❌ No channel configuration found")
                return False

    except Exception as e:
        logger.error(f"❌ Error loading channels: {str(e)}")
        return False
    finally:
        if conn:
            conn.close()

def load_user_replacements(user_id):
    """Load text replacements for user"""
    global TEXT_REPLACEMENTS, CURRENT_USER_ID
    try:
        conn = get_db()
        if not conn:
            return False

        with conn.cursor(cursor_factory=DictCursor) as cur:
            cur.execute("""
                SELECT original_text, replacement_text 
                FROM text_replacements 
                WHERE user_id = %s
                ORDER BY LENGTH(original_text) DESC
            """, (user_id,))
            TEXT_REPLACEMENTS = {row['original_text']: row['replacement_text'] for row in cur.fetchall()}
            CURRENT_USER_ID = user_id

            logger.info(f"👤 Loaded replacements for user {user_id}")
            logger.info(f"📚 Found {len(TEXT_REPLACEMENTS)} replacements")
            for original, replacement in TEXT_REPLACEMENTS.items():
                logger.info(f"📝 Loaded: '{original}' → '{replacement}'")
            return True

    except Exception as e:
        logger.error(f"❌ Error loading replacements: {str(e)}")
        TEXT_REPLACEMENTS = {}
        CURRENT_USER_ID = None
        return False
    finally:
        if conn:
            conn.close()

def apply_text_replacements(text):
    """Apply text replacements to message"""
    if not text or not TEXT_REPLACEMENTS:
        return text

    result = text
    for original, replacement in TEXT_REPLACEMENTS.items():
        if original in result:
            result = result.replace(original, replacement)
            logger.info(f"✅ Replaced '{original}' with '{replacement}'")
    return result

async def setup_client():
    """Initialize Telegram client"""
    global client

    try:
        # Initialize client
        logger.info("🔄 Starting Telegram client...")
        client = TelegramClient(
            'anon',
            API_ID,
            API_HASH,
            device_model="Replit Web",
            system_version="Linux",
            app_version="1.0"
        )

        # Connect to Telegram
        if not client.is_connected():
            await client.connect()
            logger.info("✅ Connected to Telegram")

        if not await client.is_user_authorized():
            logger.error("❌ User not authorized")
            return False

        me = await client.get_me()
        logger.info(f"✅ Client active as: {me.first_name} (ID: {me.id})")

        return True

    except Exception as e:
        logger.error(f"❌ Setup error: {str(e)}")
        return False

async def setup_handlers():
    """Set up message handlers for Telegram client"""
    global client

    try:
        # Clear existing handlers
        if client.list_event_handlers():
            for handler in client.list_event_handlers():
                client.remove_event_handler(handler)
            logger.info("🔄 Cleared existing handlers")

        # Add message handler
        @client.on(events.NewMessage())
        async def handle_new_message(event):
            try:
                # Log message details
                logger.info("\n📨 New message received")
                logger.info(f"- Chat ID: {event.chat_id}")
                logger.info(f"- Message ID: {event.message.id}")
                logger.info(f"- Message: {event.message.text}")

                if not SOURCE_CHANNEL or not DESTINATION_CHANNEL:
                    logger.warning("❌ Channels not configured")
                    return

                # Format chat IDs
                chat_id = str(event.chat_id)
                source_id = str(SOURCE_CHANNEL)
                if not chat_id.startswith('-100'):
                    chat_id = f"-100{chat_id.lstrip('-')}"
                if not source_id.startswith('-100'):
                    source_id = f"-100{source_id.lstrip('-')}"

                # Verify source channel
                if chat_id != source_id:
                    return

                logger.info("✅ Message is from source channel")

                # Process message
                message_text = event.message.text if event.message.text else ""
                if message_text and TEXT_REPLACEMENTS:
                    message_text = apply_text_replacements(message_text)

                # Format destination channel ID
                dest_id = str(DESTINATION_CHANNEL)
                if not dest_id.startswith('-100'):
                    dest_id = f"-100{dest_id.lstrip('-')}"

                # Send to destination
                try:
                    dest_channel = await client.get_entity(int(dest_id))
                    sent_message = await client.send_message(
                        dest_channel,
                        message_text,
                        formatting_entities=event.message.entities
                    )
                    MESSAGE_IDS[event.message.id] = sent_message.id
                    logger.info("✅ Message forwarded successfully")

                except Exception as e:
                    logger.error(f"❌ Forward error: {str(e)}")

            except Exception as e:
                logger.error(f"❌ Handler error: {str(e)}")

        # Add edit handler
        @client.on(events.MessageEdited())
        async def handle_edit(event):
            try:
                if event.chat_id != int(SOURCE_CHANNEL):
                    return

                dest_msg_id = MESSAGE_IDS.get(event.message.id)
                if not dest_msg_id:
                    return

                message_text = event.message.text
                if message_text and TEXT_REPLACEMENTS:
                    message_text = apply_text_replacements(message_text)

                dest_channel = await client.get_entity(int(DESTINATION_CHANNEL))
                await client.edit_message(
                    dest_channel,
                    dest_msg_id,
                    message_text,
                    formatting_entities=event.message.entities
                )
                logger.info("✅ Message edit synced successfully")

            except Exception as e:
                logger.error(f"❌ Edit sync error: {str(e)}")

        # Add debug handler
        @client.on(events.Raw)
        async def debug_raw_events(event):
            logger.debug(f"🔍 Raw event: {type(event).__name__}")

        return True

    except Exception as e:
        logger.error(f"❌ Handler setup error: {str(e)}")
        return False

async def main():
    """Main function to run the bot"""
    global client

    try:
        # Setup client
        if not await setup_client():
            logger.error("❌ Failed to setup client")
            return False

        # Load channel config
        if not load_channel_config():
            logger.error("❌ Failed to load channel configuration")
            return False

        # Setup handlers
        if not await setup_handlers():
            logger.error("❌ Failed to setup handlers")
            return False

        # Log system state
        logger.info("\n🤖 System is ready")
        logger.info(f"📱 Source channel: {SOURCE_CHANNEL}")
        logger.info(f"📱 Destination channel: {DESTINATION_CHANNEL}")
        logger.info(f"👤 Current user: {CURRENT_USER_ID}")
        logger.info(f"📚 Active replacements: {len(TEXT_REPLACEMENTS)}")

        # Run client
        await client.run_until_disconnected()
        return True

    except Exception as e:
        logger.error(f"❌ Critical error: {str(e)}")
        if client and client.is_connected():
            await client.disconnect()
        return False

if __name__ == "__main__":
    try:
        # Clean up old session
        if os.path.exists('anon.session-journal'):
            try:
                os.remove('anon.session-journal')
                logger.info("✅ Cleaned old session journal")
            except Exception as e:
                logger.error(f"❌ Cleanup error: {str(e)}")

        # Set up event loop
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            # Run main function
            loop.run_until_complete(main())
        except KeyboardInterrupt:
            logger.info("\n👋 System stopped by user")
        except Exception as e:
            logger.error(f"❌ Startup error: {str(e)}")
        finally:
            # Cleanup
            try:
                if client and client.is_connected():
                    loop.run_until_complete(client.disconnect())
            except:
                pass
            loop.close()
    except Exception as e:
        logger.error(f"❌ Fatal error: {str(e)}")
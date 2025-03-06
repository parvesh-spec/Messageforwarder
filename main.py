import os
import logging
import json
import time
from threading import Thread
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
MESSAGE_IDS = {}
TEXT_REPLACEMENTS = {}
CURRENT_USER_ID = None
SOURCE_CHANNEL = None
DESTINATION_CHANNEL = None
client = None
is_running = False

# Telegram API credentials
API_ID = int(os.getenv('API_ID', '27202142'))
API_HASH = os.getenv('API_HASH', 'db4dd0d95dc68d46b77518bf997ed165')

def get_db():
    conn = psycopg2.connect(
        os.getenv('DATABASE_URL'),
        application_name='telegram_bot_main'
    )
    conn.autocommit = True
    return conn

def load_channel_config():
    global SOURCE_CHANNEL, DESTINATION_CHANNEL
    try:
        conn = get_db()
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
                logger.info(f"📱 Loaded channels - Source: {SOURCE_CHANNEL}, Dest: {DESTINATION_CHANNEL}")
                return True
            else:
                logger.warning("❌ No channel configuration found")
                return False
    except Exception as e:
        logger.error(f"❌ Error loading channels: {str(e)}")
        return False
    finally:
        conn.close()

def load_user_replacements(user_id):
    global TEXT_REPLACEMENTS, CURRENT_USER_ID
    try:
        conn = get_db()
        with conn.cursor(cursor_factory=DictCursor) as cur:
            cur.execute("""
                SELECT original_text, replacement_text 
                FROM text_replacements 
                WHERE user_id = %s
                ORDER BY LENGTH(original_text) DESC
            """, (user_id,))

            TEXT_REPLACEMENTS = {row['original_text']: row['replacement_text'] for row in cur.fetchall()}
            CURRENT_USER_ID = user_id

            logger.info(f"👤 Loading replacements for user {user_id}")
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
        conn.close()

def apply_text_replacements(text):
    if not text:
        return text

    if not TEXT_REPLACEMENTS:
        logger.info("❌ No replacements configured")
        return text

    result = text
    for original, replacement in TEXT_REPLACEMENTS.items():
        if original in result:
            result = result.replace(original, replacement)
            logger.info(f"✅ Replaced '{original}' with '{replacement}'")
    return result

async def ensure_client_connected():
    """Ensure client is connected, reconnect if needed"""
    global client

    if not client:
        return False

    try:
        if not client.is_connected():
            await client.connect()
            if not await client.is_user_authorized():
                logger.error("❌ Client not authorized")
                return False
            logger.info("✅ Reconnected to Telegram")
        return True
    except Exception as e:
        logger.error(f"❌ Connection error: {str(e)}")
        return False

async def setup_client():
    """Initialize and connect Telegram client"""
    global client, is_running

    try:
        if client and client.is_connected():
            logger.info("✅ Using existing client")
            return True

        logger.info("🔄 Starting Telegram client...")
        client = TelegramClient(
            'anon',
            API_ID,
            API_HASH,
            device_model="Replit Web",
            system_version="Linux",
            app_version="1.0",
            retry_delay=1,
            connection_retries=3
        )

        await client.connect()
        if not await client.is_user_authorized():
            logger.error("❌ Client not authorized")
            return False

        me = await client.get_me()
        logger.info(f"✅ Client active as: {me.first_name} (ID: {me.id})")
        is_running = True
        return True

    except Exception as e:
        logger.error(f"❌ Setup error: {str(e)}")
        is_running = False
        return False

async def forward_message(event, source_id, dest_id):
    """Forward message with retries"""
    max_retries = 3
    retry_count = 0

    while retry_count < max_retries:
        try:
            if not await ensure_client_connected():
                logger.error("❌ Client disconnected")
                return False

            # Get message text
            message_text = event.message.text if event.message.text else ""
            logger.info(f"📥 Processing message: {message_text}")

            # Apply replacements
            if message_text and TEXT_REPLACEMENTS:
                old_text = message_text
                message_text = apply_text_replacements(message_text)
                logger.info(f"📝 Text replaced: {old_text} → {message_text}")

            # Get destination channel
            dest_channel = await client.get_entity(int(dest_id))
            logger.info(f"📤 Forwarding to: {getattr(dest_channel, 'title', 'Unknown')}")

            # Send message
            sent_message = await client.send_message(
                dest_channel,
                message_text,
                formatting_entities=event.message.entities
            )

            MESSAGE_IDS[event.message.id] = sent_message.id
            logger.info("✅ Message forwarded successfully")
            return True

        except Exception as e:
            retry_count += 1
            logger.error(f"❌ Forward error (attempt {retry_count}/{max_retries}): {str(e)}")
            await asyncio.sleep(1)

    return False

async def setup_handlers():
    """Set up message handlers"""
    global client

    try:
        # Clear existing handlers
        for handler in client.list_event_handlers():
            client.remove_event_handler(handler)
        logger.info("🔄 Cleared existing handlers")

        # Add message handler
        @client.on(events.NewMessage(pattern=''))
        async def handle_new_message(event):
            try:
                if not is_running:
                    return

                logger.info("\n📨 New message received")
                logger.info(f"- Chat ID: {event.chat_id}")
                logger.info(f"- Message: {event.message.text}")

                if not SOURCE_CHANNEL or not DESTINATION_CHANNEL:
                    return

                # Format chat IDs
                chat_id = str(event.chat_id)
                source_id = str(SOURCE_CHANNEL)

                if not chat_id.startswith('-100'):
                    chat_id = f"-100{chat_id.lstrip('-')}"
                if not source_id.startswith('-100'):
                    source_id = f"-100{source_id.lstrip('-')}"

                if chat_id != source_id:
                    return

                logger.info("✅ Message is from source channel")
                await forward_message(event, source_id, DESTINATION_CHANNEL)

            except Exception as e:
                logger.error(f"❌ Handler error: {str(e)}")

        # Add debug handler
        @client.on(events.Raw)
        async def debug_raw_events(event):
            if is_running:
                logger.info(f"🔍 Raw event: {type(event).__name__}")

        return True

    except Exception as e:
        logger.error(f"❌ Handler setup error: {str(e)}")
        return False

async def main():
    """Main entry point"""
    global client, is_running

    try:
        # Setup client
        if not await setup_client():
            return

        # Load config
        if not load_channel_config():
            logger.error("❌ Failed to load channel configuration")
            return

        # Setup handlers
        if not await setup_handlers():
            logger.error("❌ Failed to setup handlers")
            return

        # Monitor configuration
        def config_monitor():
            while is_running:
                try:
                    if client and client.is_connected():
                        if load_channel_config():
                            logger.info("✅ Channel config refreshed")
                        if CURRENT_USER_ID and load_user_replacements(CURRENT_USER_ID):
                            logger.info("✅ Replacements refreshed")
                    time.sleep(30)
                except Exception as e:
                    logger.error(f"❌ Monitor error: {str(e)}")
                    time.sleep(1)

        Thread(target=config_monitor, daemon=True).start()
        logger.info("✅ Started config monitor")

        # Log state
        logger.info("\n🤖 System is ready")
        logger.info(f"📱 Source channel: {SOURCE_CHANNEL}")
        logger.info(f"📱 Destination: {DESTINATION_CHANNEL}")
        logger.info(f"👤 Current user: {CURRENT_USER_ID}")
        logger.info(f"📚 Active replacements: {len(TEXT_REPLACEMENTS)}")

        await client.run_until_disconnected()

    except Exception as e:
        logger.error(f"❌ Critical error: {str(e)}")
        import traceback
        logger.error(f"❌ Traceback:\n{traceback.format_exc()}")
    finally:
        is_running = False
        if client and client.is_connected():
            await client.disconnect()

if __name__ == "__main__":
    try:
        # Clean up old session
        if os.path.exists('anon.session-journal'):
            try:
                os.remove('anon.session-journal')
                logger.info("✅ Cleaned old session journal")
            except Exception as e:
                logger.error(f"❌ Cleanup error: {str(e)}")

        # Run main function
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("\n👋 System stopped by user")
    finally:
        is_running = False
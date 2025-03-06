import os
import logging
import json
import time
from threading import Thread, Event
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
monitor_thread = None
stop_monitor = Event()

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

            logger.info(f"📚 Loaded {len(TEXT_REPLACEMENTS)} replacements")
            return True
    except Exception as e:
        logger.error(f"❌ Error loading replacements: {str(e)}")
        TEXT_REPLACEMENTS = {}
        CURRENT_USER_ID = None
        return False
    finally:
        conn.close()

def apply_text_replacements(text):
    if not text or not TEXT_REPLACEMENTS:
        return text

    result = text
    for original, replacement in TEXT_REPLACEMENTS.items():
        if original in result:
            result = result.replace(original, replacement)
            logger.info(f"✅ Replaced '{original}' → '{replacement}'")
    return result

async def setup_client():
    global client

    try:
        # Initialize client with retry settings
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

def start_monitor():
    global monitor_thread, stop_monitor

    stop_monitor.clear()

    def monitor_func():
        while not stop_monitor.is_set():
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

    monitor_thread = Thread(target=monitor_func, daemon=True)
    monitor_thread.start()
    logger.info("✅ Started config monitor")

def stop_monitor_thread():
    global monitor_thread, stop_monitor

    if monitor_thread and monitor_thread.is_alive():
        stop_monitor.set()
        monitor_thread.join(timeout=5)
        logger.info("✅ Stopped config monitor")

async def setup_handlers():
    global client

    if not client:
        logger.error("❌ Client not initialized")
        return False

    try:
        # Clear existing handlers
        for handler in client.list_event_handlers():
            client.remove_event_handler(handler)
        logger.info("🔄 Cleared existing handlers")

        # Main message handler
        @client.on(events.NewMessage(pattern=''))
        async def handle_new_message(event):
            try:
                logger.info("\n📨 New message received")
                logger.info(f"- From chat: {event.chat_id}")
                logger.info(f"- Text: {event.message.text}")

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

                logger.info(f"🔍 Message from {chat_id}, Source: {source_id}")

                if chat_id != source_id:
                    logger.info("👉 Not from source channel")
                    return

                logger.info("✅ Message is from source channel")

                # Process message
                message_text = event.message.text if event.message.text else ""
                if message_text and TEXT_REPLACEMENTS:
                    old_text = message_text
                    message_text = apply_text_replacements(message_text)
                    logger.info(f"📝 Text replaced: '{old_text}' → '{message_text}'")

                # Format destination ID
                dest_id = str(DESTINATION_CHANNEL)
                if not dest_id.startswith('-100'):
                    dest_id = f"-100{dest_id.lstrip('-')}"

                # Send to destination
                dest_channel = await client.get_entity(int(dest_id))
                logger.info(f"📤 Forwarding to: {getattr(dest_channel, 'title', 'Unknown')}")

                sent_message = await client.send_message(
                    dest_channel,
                    message_text,
                    formatting_entities=event.message.entities
                )

                MESSAGE_IDS[event.message.id] = sent_message.id
                logger.info("✅ Message forwarded successfully")

            except Exception as e:
                logger.error(f"❌ Handler error: {str(e)}")
                import traceback
                logger.error(f"❌ Traceback:\n{traceback.format_exc()}")

        # Add debug handler
        @client.on(events.Raw)
        async def debug_raw_events(event):
            logger.info(f"🔍 Raw event: {type(event).__name__}")

        # Verify handlers
        handlers = client.list_event_handlers()
        logger.info(f"\n✅ Total handlers: {len(handlers)}")
        for handler in handlers:
            logger.info(f"📌 Handler: {handler}")

        return True

    except Exception as e:
        logger.error(f"❌ Handler setup error: {str(e)}")
        return False

async def cleanup_client():
    global client

    try:
        if client:
            # Stop event handlers
            for handler in client.list_event_handlers():
                client.remove_event_handler(handler)

            # Disconnect client
            if client.is_connected():
                await client.disconnect()
                logger.info("✅ Client disconnected")

            client = None
    except Exception as e:
        logger.error(f"❌ Cleanup error: {str(e)}")

async def main():
    try:
        # Setup client
        if not await setup_client():
            logger.error("❌ Failed to setup client")
            return

        # Load channel config
        if not load_channel_config():
            logger.error("❌ Failed to load channel configuration")
            await cleanup_client()
            return

        # Setup handlers
        if not await setup_handlers():
            logger.error("❌ Failed to setup handlers")
            await cleanup_client()
            return

        # Start config monitor
        start_monitor()

        # Log system state
        logger.info("\n🤖 System is ready")
        logger.info(f"📱 Source channel: {SOURCE_CHANNEL}")
        logger.info(f"📱 Destination channel: {DESTINATION_CHANNEL}")
        logger.info(f"👤 Current user: {CURRENT_USER_ID}")
        logger.info(f"📚 Active replacements: {len(TEXT_REPLACEMENTS)}")

        # Run client
        await client.run_until_disconnected()

    except Exception as e:
        logger.error(f"❌ Critical error: {str(e)}")
        import traceback
        logger.error(f"❌ Traceback:\n{traceback.format_exc()}")
    finally:
        # Cleanup
        stop_monitor_thread()
        await cleanup_client()

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
    except Exception as e:
        logger.error(f"❌ Startup error: {str(e)}")
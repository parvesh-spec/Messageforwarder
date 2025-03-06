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
        logger.error(f"❌ Database connection error: {e}")
        raise

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
    finally:
        if 'conn' in locals():
            conn.close()

def get_user_id_by_phone(phone):
    try:
        conn = get_db()
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM users WHERE phone = %s", (phone,))
            result = cur.fetchone()
            return result[0] if result else None
    finally:
        if 'conn' in locals():
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
            logger.info(f"✅ Loaded {len(TEXT_REPLACEMENTS)} replacements for user {user_id}")
            return True
    except Exception as e:
        logger.error(f"❌ Error loading replacements: {str(e)}")
        TEXT_REPLACEMENTS = {}
        CURRENT_USER_ID = None
        return False
    finally:
        if 'conn' in locals():
            conn.close()

def apply_text_replacements(text):
    if not text:
        return text
    if not TEXT_REPLACEMENTS:
        return text

    result = text
    for original, replacement in TEXT_REPLACEMENTS.items():
        if original in result:
            result = result.replace(original, replacement)
            logger.info(f"✅ Replaced '{original}' with '{replacement}'")
    return result

async def setup_client():
    global client

    try:
        client = TelegramClient(
            'anon',
            API_ID,
            API_HASH,
            device_model="Replit Web",
            system_version="Linux",
            app_version="1.0"
        )

        # Connect and verify
        if not client.is_connected():
            await client.connect()
            logger.info("✅ Connected to Telegram")

        if not await client.is_user_authorized():
            logger.error("❌ User not authorized")
            return False

        me = await client.get_me()
        logger.info(f"✅ Client active as: {me.first_name} (ID: {me.id})")

        # Get phone from session
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
                    logger.info(f"📱 Found phone: {session_phone}")
        except Exception as e:
            logger.error(f"❌ Error reading session: {str(e)}")
            return False

        # Load user data
        if session_phone:
            user_id = get_user_id_by_phone(session_phone)
            if user_id:
                logger.info(f"👤 Found user ID: {user_id}")
                if load_user_replacements(user_id):
                    logger.info("✅ Loaded replacements")
                else:
                    logger.warning("❌ Failed to load replacements")

        return True

    except Exception as e:
        logger.error(f"❌ Setup error: {str(e)}")
        return False

async def setup_handlers():
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
                logger.info("\n📨 New message received")
                logger.info(f"- Chat ID: {event.chat_id}")
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

                logger.info(f"🔍 Comparing channels:")
                logger.info(f"- Source: {source_id}")
                logger.info(f"- Message from: {chat_id}")

                if chat_id != source_id:
                    logger.info("👉 Not from source channel")
                    return

                logger.info("✅ Message is from source channel")

                # Process message
                message_text = event.message.text if event.message.text else ""
                logger.info(f"📥 Original message: {message_text}")

                # Apply replacements
                if message_text and TEXT_REPLACEMENTS:
                    old_text = message_text
                    message_text = apply_text_replacements(message_text)
                    logger.info(f"📝 After replacements: {message_text}")

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
                logger.error(f"❌ Forward error: {str(e)}")
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

async def main():
    global client

    try:
        # Setup client
        if not await setup_client():
            logger.error("❌ Failed to setup client")
            return

        # Load channel config
        if not load_channel_config():
            logger.error("❌ Failed to load channel configuration")
            return

        # Setup handlers
        if not await setup_handlers():
            logger.error("❌ Failed to setup handlers")
            return

        # Start monitor
        def config_monitor():
            while True:
                try:
                    if client and client.is_connected():
                        if load_channel_config():
                            logger.info("✅ Channel config refreshed")
                        if CURRENT_USER_ID:
                            if load_user_replacements(CURRENT_USER_ID):
                                logger.info("✅ Replacements refreshed")
                    time.sleep(30)
                except Exception as e:
                    logger.error(f"❌ Monitor error: {str(e)}")
                    time.sleep(1)

        Thread(target=config_monitor, daemon=True).start()
        logger.info("✅ Started config monitor")

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
        if client and client.is_connected():
            await client.disconnect()
        raise

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
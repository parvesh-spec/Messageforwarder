import os
import logging
import json
import time
from threading import Thread
import asyncio
from telethon import TelegramClient, events
from telethon.errors import SessionPasswordNeededError, AuthKeyUnregisteredError
from telethon.sessions import StringSession
from sqlalchemy import create_engine, text
from sqlalchemy.orm import scoped_session, sessionmaker
from sqlalchemy.pool import QueuePool

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

# Configure database engine with proper pooling
engine = create_engine(
    os.getenv('DATABASE_URL'),
    poolclass=QueuePool,
    pool_size=5,
    max_overflow=10,
    pool_timeout=30,
    pool_recycle=1800
)

# Create session factory
Session = scoped_session(sessionmaker(bind=engine))

def get_db():
    """Get a database session with proper error handling"""
    session = Session()
    try:
        return session
    except Exception as e:
        logger.error(f"❌ Database error: {str(e)}")
        session.rollback()
        raise
    finally:
        session.close()

def load_channel_config():
    """Load channel configuration from database"""
    global SOURCE_CHANNEL, DESTINATION_CHANNEL
    try:
        session = get_db()
        sql = text("""
            SELECT source_channel, destination_channel 
            FROM channel_config 
            ORDER BY updated_at DESC 
            LIMIT 1
        """)
        result = session.execute(sql)
        config = result.fetchone()

        if config:
            SOURCE_CHANNEL = config['source_channel']
            DESTINATION_CHANNEL = config['destination_channel']
            logger.info(f"📱 Loaded channels - Source: {SOURCE_CHANNEL}, Dest: {DESTINATION_CHANNEL}")
            return True
        else:
            logger.warning("❌ No channel configuration found")
            return False
    except Exception as e:
        logger.error(f"❌ Error loading channels: {str(e)}")
        return False

def load_user_replacements(user_id):
    """Load text replacements for user from database"""
    global TEXT_REPLACEMENTS, CURRENT_USER_ID
    try:
        session = get_db()
        sql = text("""
            SELECT original_text, replacement_text 
            FROM text_replacements 
            WHERE user_id = :user_id
            ORDER BY LENGTH(original_text) DESC
        """)
        result = session.execute(sql, {"user_id": user_id})

        TEXT_REPLACEMENTS = {row['original_text']: row['replacement_text'] for row in result}
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

def apply_text_replacements(text):
    """Apply text replacements to message"""
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

async def setup_client():
    """Initialize and setup Telegram client"""
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
    """Setup message handlers for the client"""
    global client

    try:
        # Clear existing handlers
        if client.list_event_handlers():
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

                if chat_id != source_id:
                    logger.info("👉 Not from source channel")
                    return

                logger.info("✅ Message is from source channel")

                try:
                    # Process message
                    message_text = event.message.text if event.message.text else ""
                    logger.info(f"📥 Original message: {message_text}")

                    if message_text and TEXT_REPLACEMENTS:
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

async def main():
    """Main function to run the Telegram bot"""
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
        import traceback
        logger.error(f"❌ Traceback:\n{traceback.format_exc()}")
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

        # Run main function
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(main())
    except KeyboardInterrupt:
        logger.info("\n👋 System stopped by user")
    except Exception as e:
        logger.error(f"❌ Startup error: {str(e)}")
    finally:
        loop.close()
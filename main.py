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
handler = logging.StreamHandler()
handler.setLevel(logging.DEBUG)
formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
handler.setFormatter(formatter)
logger = logging.getLogger(__name__)
logger.addHandler(handler)
logger.setLevel(logging.DEBUG)

# Global variables
MESSAGE_IDS = {}  # Will store source_msg_id: destination_msg_id mapping
TEXT_REPLACEMENTS = {}
CURRENT_USER_ID = None
SOURCE_CHANNEL = None
DESTINATION_CHANNEL = None

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

def get_user_id_by_phone(phone):
    try:
        conn = get_db()
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM users WHERE phone = %s", (phone,))
            result = cur.fetchone()
            if result:
                return result[0]
            logger.warning(f"❌ No user found for phone: {phone}")
            return None
    except Exception as e:
        logger.error(f"❌ Database error: {str(e)}")
        return None
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

            # Test replacements
            test_text = "hello what are you doing?"
            logger.info("\n🧪 Testing replacements:")
            logger.info(f"📝 Input: '{test_text}'")
            test_result = apply_text_replacements(test_text)
            logger.info(f"📝 Output: '{test_result}'\n")

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
        logger.info("❌ Empty text, skipping replacements")
        return text

    if not TEXT_REPLACEMENTS:
        logger.info("❌ No replacements available")
        return text

    logger.info(f"🔄 Processing text: '{text}'")
    logger.info(f"📚 Using replacements: {TEXT_REPLACEMENTS}")

    result = text
    for original, replacement in sorted(TEXT_REPLACEMENTS.items(), key=lambda x: len(x[0]), reverse=True):
        if original in result:
            old_text = result
            result = result.replace(original, replacement)
            logger.info(f"✅ Replaced '{original}' with '{replacement}'")
            logger.info(f"📝 Text changed: '{old_text}' → '{result}'")

    return result

async def forward_message(client, event, source_id, dest_id):
    try:
        # Get message text
        message_text = event.message.text if event.message.text else ""
        logger.info(f"📥 Received message: '{message_text}'")

        # Apply replacements if needed
        if message_text:
            message_text = apply_text_replacements(message_text)
            logger.info(f"📤 Final message: '{message_text}'")

        # Get destination channel
        try:
            dest_channel = await client.get_entity(int(dest_id))
            logger.info(f"✅ Found destination channel")
        except Exception as e:
            logger.error(f"❌ Error getting destination channel: {str(e)}")
            return False

        # Send message
        try:
            sent_message = await client.send_message(
                dest_channel,
                message_text,
                formatting_entities=event.message.entities
            )
            MESSAGE_IDS[event.message.id] = sent_message.id
            logger.info("✅ Message forwarded successfully")
            return True
        except Exception as e:
            logger.error(f"❌ Error sending message: {str(e)}")
            return False

    except Exception as e:
        logger.error(f"❌ Forward error: {str(e)}")
        return False

async def main():
    try:
        # Initialize client
        logger.info("🔄 Starting Telegram client...")
        client = TelegramClient(
            'anon',
            API_ID,
            API_HASH,
            device_model="Replit Bot",
            system_version="Linux",
            app_version="1.0",
            connection_retries=None
        )

        try:
            # Connect to Telegram
            await client.connect()
            logger.info("✅ Connected to Telegram")

            # Check authorization
            if not await client.is_user_authorized():
                logger.error("❌ Bot not authorized")
                return

            # Get session info
            me = await client.get_me()
            logger.info(f"✅ Logged in as {me.first_name} (ID: {me.id})")

            # Get phone number from session
            session_phone = None
            try:
                with open('anon.session', 'rb') as f:
                    f.seek(20)
                    phone_len = int.from_bytes(f.read(1), 'little')
                    if phone_len > 0:
                        phone_bytes = f.read(phone_len)
                        session_phone = phone_bytes.decode('utf-8')
                        if not session_phone.startswith('+'):
                            session_phone = f"+{session_phone}"
                        logger.info(f"📱 Session phone: {session_phone}")
            except Exception as e:
                logger.error(f"❌ Error reading session: {str(e)}")

            # Load user data and replacements
            if session_phone:
                user_id = get_user_id_by_phone(session_phone)
                if user_id:
                    logger.info(f"👤 Found user ID: {user_id}")
                    if load_user_replacements(user_id):
                        logger.info("✅ Loaded text replacements")
                    else:
                        logger.warning("❌ Failed to load replacements")
                else:
                    logger.warning(f"❌ No user found for phone: {session_phone}")

            # Load channel configuration
            if not load_channel_config():
                logger.error("❌ Failed to load channel configuration")
                return

            # Set up message handlers
            @client.on(events.NewMessage())
            async def handle_new_message(event):
                try:
                    if not SOURCE_CHANNEL or not DESTINATION_CHANNEL:
                        logger.warning("❌ Channels not configured")
                        return

                    # Format channel IDs
                    source_id = str(SOURCE_CHANNEL)
                    if not source_id.startswith('-100'):
                        source_id = f"-100{source_id.lstrip('-')}"

                    chat_id = str(event.chat_id)
                    if not chat_id.startswith('-100'):
                        chat_id = f"-100{chat_id.lstrip('-')}"

                    # Check if message is from source channel
                    if chat_id != source_id:
                        return

                    logger.info(f"📨 New message from source channel")

                    # Forward message
                    dest_id = str(DESTINATION_CHANNEL)
                    if not dest_id.startswith('-100'):
                        dest_id = f"-100{dest_id.lstrip('-')}"

                    success = await forward_message(client, event, source_id, dest_id)
                    if not success:
                        logger.error("❌ Failed to forward message")

                except Exception as e:
                    logger.error(f"❌ Message handler error: {str(e)}")

            @client.on(events.MessageEdited())
            async def handle_message_edited(event):
                try:
                    if not SOURCE_CHANNEL or not DESTINATION_CHANNEL:
                        return

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

                    message_text = event.message.text if event.message.text else ""
                    if message_text:
                        message_text = apply_text_replacements(message_text)

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
                    logger.error(f"❌ Edit handler error: {str(e)}")


            # Start monitoring
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

            Thread(target=config_monitor, daemon=True).start()
            logger.info("✅ Started config monitor")

            logger.info("\n🤖 Bot is running")
            logger.info(f"📱 Source channel: {SOURCE_CHANNEL}")
            logger.info(f"📱 Destination: {DESTINATION_CHANNEL}")
            logger.info(f"👤 Current user ID: {CURRENT_USER_ID}")
            logger.info(f"📚 Active replacements: {TEXT_REPLACEMENTS}")


            await client.run_until_disconnected()

        except Exception as e:
            logger.error(f"❌ Client error: {str(e)}")
            if client and client.connected:
                await client.disconnect()
            raise

    except Exception as e:
        logger.error(f"❌ Critical error: {str(e)}")
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
        logger.info("\n👋 Bot stopped by user")
    except Exception as e:
        logger.error(f"❌ Startup error: {str(e)}")
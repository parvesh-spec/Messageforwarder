import os
import logging
import threading
import time
from datetime import datetime
from telethon import TelegramClient, events
from telethon.sessions import StringSession
import asyncio
import psycopg2
from psycopg2.extras import DictCursor

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)

# Disable debug logs from telethon
logging.getLogger('telethon').setLevel(logging.WARNING)
logging.getLogger('asyncio').setLevel(logging.WARNING)


# Global variables for multi-user support
USER_SESSIONS = {}  # user_id: {client, source, destination, replacements}
MESSAGE_IDS = {}    # user_id: {source_msg_id: destination_msg_id}

# API credentials
API_ID = int(os.getenv('API_ID', '27202142'))
API_HASH = os.getenv('API_HASH', 'db4dd0d95dc68d46b77518bf997ed165')


# Database connection 
def get_db():
    """Get database connection from pool"""
    try:
        conn = psycopg2.connect(os.getenv('DATABASE_URL'))
        conn.autocommit = True
        return conn
    except Exception as e:
        logger.error(f"❌ Database connection error: {str(e)}")
        return None

def release_db(conn):
    """Release connection back to pool"""
    if conn:
        conn.close()


async def setup_client(user_id, session_string, max_retries=3, retry_delay=5):
    """Initialize Telegram client for a specific user"""
    for attempt in range(max_retries):
        try:
            # Create new client instance with in-memory session
            client = TelegramClient(
                StringSession(session_string),
                API_ID,
                API_HASH,
                device_model="Replit Bot",
                system_version="Linux",
                app_version="1.0"
            )

            # Connect and verify
            try:
                await client.connect()

                if not await client.is_user_authorized():
                    logger.error(f"❌ Client not authorized for user {user_id}")
                    await client.disconnect()
                    return None

                return client

            except Exception as e:
                logger.error(f"❌ Connection error: {str(e)}")
                if client:
                    await client.disconnect()
                await asyncio.sleep(retry_delay)
                continue

        except Exception as e:
            logger.error(f"❌ Setup error (attempt {attempt + 1}/{max_retries}): {str(e)}")
            await asyncio.sleep(retry_delay)

    return None

def load_user_config(user_id):
    """Load channel configuration for a specific user"""
    conn = None
    try:
        conn = get_db()
        if not conn:
            return None, None

        with conn.cursor(cursor_factory=DictCursor) as cur:
            cur.execute("""
                SELECT source_channel, destination_channel 
                FROM channel_config 
                WHERE user_id = %s
                ORDER BY updated_at DESC 
                LIMIT 1
            """, (user_id,))
            result = cur.fetchone()

            if result:
                return result['source_channel'], result['destination_channel']
            else:
                logger.warning(f"❌ No channel configuration found for user {user_id}")
                return None, None

    except Exception as e:
        logger.error(f"❌ Channel config error for user {user_id}: {str(e)}")
        return None, None
    finally:
        if conn:
            release_db(conn)

def load_user_replacements(user_id):
    """Load text replacements for a specific user"""
    conn = None
    try:
        conn = get_db()
        if not conn:
            return {}

        with conn.cursor(cursor_factory=DictCursor) as cur:
            # Convert telegram_id to integer for query
            telegram_id = int(user_id) if isinstance(user_id, str) else user_id

            cur.execute("""
                SELECT t.original_text, t.replacement_text
                FROM text_replacements t
                JOIN users u ON u.id = t.user_id
                WHERE u.telegram_id = %s AND t.is_active = true
                ORDER BY LENGTH(original_text) DESC
            """, (telegram_id,))

            replacements = {}
            for row in cur.fetchall():
                replacements[row['original_text']] = row['replacement_text']
                logger.info(f"✅ Loaded active replacement: '{row['original_text']}' → '{row['replacement_text']}'")

            logger.info(f"✅ Loaded {len(replacements)} active replacements for telegram_id {telegram_id}")
            return replacements

    except Exception as e:
        logger.error(f"❌ Failed to load replacements for user {user_id}: {str(e)}")
        return {}
    finally:
        if conn:
            release_db(conn)

def apply_text_replacements(text, user_id):
    """Apply text replacements for a specific user"""
    if not text or user_id not in USER_SESSIONS:
        return text

    result = text
    replacements = USER_SESSIONS[user_id].get('replacements', {})
    logger.info(f"Processing text: '{text}' with {len(replacements)} replacements")

    # Sort replacements by length (longest first) to avoid partial replacements
    sorted_replacements = sorted(
        replacements.items(),
        key=lambda x: len(x[0]),
        reverse=True
    )

    # Apply each replacement
    for original, replacement in sorted_replacements:
        if original in result:
            result = result.replace(original, replacement)
            logger.info(f"✅ Applied replacement: '{original}' → '{replacement}'")

    if result != text:
        logger.info(f"Text after replacements: '{result}'")

    return result

async def setup_user_handlers(user_id, client):
    """Set up message handlers for a specific user"""
    if not client:
        return False

    try:
        session = USER_SESSIONS.get(user_id, {})
        source = session.get('source')
        destination = session.get('destination')

        async def handle_new_message(event):
            try:
                # Format channel IDs for comparison
                chat_id = str(event.chat_id)
                if not chat_id.startswith('-100'):
                    chat_id = f"-100{chat_id.lstrip('-')}"

                source_id = str(source)
                if not source_id.startswith('-100'):
                    source_id = f"-100{source_id.lstrip('-')}"

                # Compare exact channel IDs
                if chat_id != source_id:
                    return

                # Process message
                message = event.message
                message_text = message.text if message.text else ""
                if message_text:
                    message_text = apply_text_replacements(message_text, user_id)

                try:
                    # Format destination ID
                    dest_id = str(destination)
                    if not dest_id.startswith('-100'):
                        dest_id = f"-100{dest_id.lstrip('-')}"

                    # Forward message
                    dest_channel = await client.get_entity(int(dest_id))
                    logger.info(f"📥 Forwarding message to destination channel")

                    forward_start = int(time.time())

                    sent_message = await client.send_message(
                        dest_channel,
                        message_text,
                        file=message.media if message.media else None,
                        formatting_entities=message.entities
                    )

                    forward_end = int(time.time())

                    # Store message mapping
                    if user_id not in MESSAGE_IDS:
                        MESSAGE_IDS[user_id] = {}
                    MESSAGE_IDS[user_id][message.id] = sent_message.id

                    # Store forwarding logs in database
                    conn = get_db()
                    if conn:
                        try:
                            with conn.cursor() as cur:
                                cur.execute("""
                                    INSERT INTO forwarding_logs 
                                    (user_id, source_message_id, dest_message_id, source_chat_id, 
                                     dest_chat_id, message_text, received_at, forwarded_at, created_at)
                                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, CURRENT_TIMESTAMP)
                                """, (
                                    user_id, message.id, sent_message.id, 
                                    source_id, dest_id, message_text,
                                    forward_start, forward_end
                                ))
                            logger.info("✅ Message forwarded successfully")
                        except Exception as db_error:
                            logger.error(f"❌ Database error: {str(db_error)}")
                        finally:
                            release_db(conn)

                except Exception as e:
                    logger.error(f"❌ Message forward error: {str(e)}")

            except Exception as e:
                logger.error(f"❌ Handler error: {str(e)}")

        # Add event handler
        client.add_event_handler(handle_new_message, events.NewMessage())
        return True

    except Exception as e:
        logger.error(f"❌ Handler setup error: {str(e)}")
        return False

async def manage_user_session(user_id):
    """Manage a user's bot session"""
    while True:
        try:
            # Check if bot should still be running
            conn = get_db()
            if conn:
                try:
                    with conn.cursor(cursor_factory=DictCursor) as cur:
                        cur.execute("""
                            SELECT is_running, session_string 
                            FROM bot_status 
                            WHERE user_id = %s
                        """, (user_id,))
                        result = cur.fetchone()

                        if not result or not result['is_running']:
                            logger.info(f"👋 Bot stopped for user {user_id}")
                            break

                        # Update session if client is not connected
                        if user_id in USER_SESSIONS:
                            client = USER_SESSIONS[user_id].get('client')
                            if not client or not client.is_connected():
                                logger.error(f"❌ Client disconnected for user {user_id}, reconnecting...")
                                client = await setup_client(user_id, result['session_string'])
                                if client:
                                    # Get latest channel config
                                    cur.execute("""
                                        SELECT source_channel, destination_channel
                                        FROM channel_config
                                        WHERE user_id = %s
                                    """, (user_id,))
                                    channels = cur.fetchone()

                                    if channels:
                                        USER_SESSIONS[user_id] = {
                                            'client': client,
                                            'source': channels['source_channel'],
                                            'destination': channels['destination_channel'],
                                            'replacements': load_user_replacements(user_id)
                                        }
                                        success = await setup_user_handlers(user_id, client)
                                        if success:
                                            logger.info(f"✅ Successfully reconnected bot for user {user_id}")
                                        else:
                                            logger.error(f"❌ Failed to setup handlers for user {user_id}")
                finally:
                    release_db(conn)

            # Wait before next check
            await asyncio.sleep(30)

        except Exception as e:
            logger.error(f"❌ Session management error for user {user_id}: {str(e)}")
            await asyncio.sleep(30)

def add_user_session(user_id, session_string, source_channel=None, destination_channel=None):
    """Add or update a user's session"""
    async def setup_session():
        try:
            client = await setup_client(user_id, session_string)
            if client:
                # Initialize or update user session
                USER_SESSIONS[user_id] = {
                    'client': client,
                    'source': source_channel,
                    'destination': destination_channel,
                    'replacements': load_user_replacements(user_id)
                }

                # Setup handlers and start client
                success = await setup_user_handlers(user_id, client)
                if success:
                    try:
                        await client.run_until_disconnected()
                        return True
                    except Exception as e:
                        logger.error(f"❌ Client run error: {str(e)}")
                        return False
            return False
        except Exception as e:
            logger.error(f"❌ Session setup error: {str(e)}")
            return False

    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        return loop.run_until_complete(setup_session())
    except Exception as e:
        logger.error(f"❌ Add session error: {str(e)}")
        return False

def remove_user_session(user_id):
    """Remove a user's session"""
    if user_id in USER_SESSIONS:
        try:
            session = USER_SESSIONS[user_id]
            client = session.get('client')
            if client:
                try:
                    loop = asyncio.new_event_loop()
                    asyncio.set_event_loop(loop)
                    loop.run_until_complete(client.disconnect())
                    loop.close()
                    logger.info(f"✅ Client disconnected for user {user_id}")
                except Exception as e:
                    logger.error(f"❌ Client disconnect error: {str(e)}")

            USER_SESSIONS.pop(user_id)
            if user_id in MESSAGE_IDS:
                MESSAGE_IDS.pop(user_id)
            logger.info(f"✅ Session removed for user {user_id}")
            return True
        except Exception as e:
            logger.error(f"❌ Session removal error for user {user_id}: {str(e)}")
    return False

def update_user_channels(user_id, source, destination):
    """Update a user's channel configuration"""
    if user_id in USER_SESSIONS:
        USER_SESSIONS[user_id].update({
            'source': source,
            'destination': destination
        })
        logger.info(f"✅ Channels updated for user {user_id}")

def update_user_replacements(user_id):
    """Update a user's text replacements"""
    if user_id in USER_SESSIONS:
        logger.info(f"Updating replacements for user {user_id}")
        replacements = load_user_replacements(user_id)
        USER_SESSIONS[user_id]['replacements'] = replacements
        logger.info(f"✅ Updated {len(replacements)} replacements for user {user_id}")

if __name__ == "__main__":
    try:
        # Keep the main thread running
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_forever()
        except KeyboardInterrupt:
            logger.info("👋 Bot stopped by user")
        finally:
            loop.close()

    except Exception as e:
        logger.error(f"❌ Fatal error: {str(e)}")
        if not USER_SESSIONS:
            logger.warning("⚠️ No session string provided, waiting for configuration")
import os
import logging
import threading
import psycopg2
from psycopg2.extras import DictCursor
from psycopg2 import pool
import asyncio
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from flask import Flask, jsonify
import time

# Global variables for multi-user support
USER_SESSIONS = {}  # user_id: {client, source, destination, replacements}
MESSAGE_IDS = {}  # user_id: {source_msg_id: destination_msg_id}

# Set up logging
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# API credentials
API_ID = int(os.getenv('API_ID', '27202142'))
API_HASH = os.getenv('API_HASH', 'db4dd0d95dc68d46b77518bf997ed165')

# Create a small Flask app for health checks
health_app = Flask(__name__)

@health_app.route('/')
def health_check():
    return jsonify({"status": "ok"}), 200

# Database connection pool
db_pool = psycopg2.pool.ThreadedConnectionPool(
    minconn=1,
    maxconn=10,
    dsn=os.getenv('DATABASE_URL')
)

# Lock for thread safety
db_lock = threading.Lock()

def get_db():
    """Get database connection from pool"""
    try:
        conn = db_pool.getconn()
        conn.autocommit = True
        return conn
    except Exception as e:
        logger.error(f"❌ Database connection error: {str(e)}")
        return None

def release_db(conn):
    """Release connection back to pool"""
    if conn:
        db_pool.putconn(conn)

async def setup_client(user_id, session_string, max_retries=3, retry_delay=5):
    """Initialize Telegram client for a specific user"""
    for attempt in range(max_retries):
        try:
            if not session_string:
                logger.warning(f"⚠️ No session string for user {user_id}")
                return None

            # Create new client instance
            client = TelegramClient(
                StringSession(session_string),
                API_ID,
                API_HASH,
                device_model="Replit Bot",
                system_version="Linux",
                app_version="1.0",
                retry_delay=retry_delay
            )

            # Connect with timeout
            try:
                await asyncio.wait_for(client.connect(), timeout=30)
            except asyncio.TimeoutError:
                logger.error(f"❌ Connection timeout for user {user_id}, retrying...")
                if client:
                    await client.disconnect()
                time.sleep(retry_delay)
                continue

            # Verify authorization
            if not await client.is_user_authorized():
                logger.error(f"❌ Bot not authorized for user {user_id}")
                await client.disconnect()
                return None

            me = await client.get_me()
            logger.info(f"✅ Bot running for user {user_id} as: {me.first_name} (ID: {me.id})")
            return client

        except Exception as e:
            logger.error(f"❌ Client setup error for user {user_id} (attempt {attempt + 1}/{max_retries}): {str(e)}")
            time.sleep(retry_delay)

    logger.error(f"❌ All connection attempts failed for user {user_id}")
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
            cur.execute("""
                SELECT original_text, replacement_text 
                FROM text_replacements 
                WHERE user_id = %s
                ORDER BY LENGTH(original_text) DESC
            """, (user_id,))

            replacements = {}
            for row in cur.fetchall():
                replacements[row['original_text']] = row['replacement_text']

            logger.info(f"✅ Loaded {len(replacements)} replacements for user {user_id}")
            return replacements

    except Exception as e:
        logger.error(f"❌ Replacements error for user {user_id}: {str(e)}")
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
    for original, replacement in replacements.items():
        if original in result:
            result = result.replace(original, replacement)
            logger.info(f"✅ Replaced: {original} → {replacement} for user {user_id}")

    return result

async def setup_user_handlers(user_id, client):
    """Set up message handlers for a specific user"""
    if not client:
        return False

    try:
        @client.on(events.NewMessage())
        async def handle_new_message(event):
            try:
                if user_id not in USER_SESSIONS:
                    logger.info(f"No session found for user {user_id}")
                    return

                session = USER_SESSIONS[user_id]
                source = session.get('source')
                destination = session.get('destination')

                if not source or not destination:
                    logger.info(f"No source or destination channel configured for user {user_id}")
                    return

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

                # Get destination channel ID
                dest_id = str(destination)
                if not dest_id.startswith('-100'):
                    dest_id = f"-100{dest_id.lstrip('-')}"

                try:
                    # Get destination channel
                    dest_channel = await client.get_entity(int(dest_id))
                    logger.info(f"✅ Forwarding message to {dest_channel.title}")

                    # Forward message
                    sent_message = await client.send_message(
                        dest_channel,
                        message_text,
                        file=message.media if message.media else None,
                        formatting_entities=message.entities
                    )

                    # Store message mapping
                    if user_id not in MESSAGE_IDS:
                        MESSAGE_IDS[user_id] = {}
                    MESSAGE_IDS[user_id][message.id] = sent_message.id
                    logger.info(f"✅ Message forwarded successfully")

                except Exception as e:
                    logger.error(f"❌ Message forward error: {str(e)}")

            except Exception as e:
                logger.error(f"❌ Message handler error: {str(e)}")

        @client.on(events.MessageEdited())
        async def handle_edit(event):
            try:
                if user_id not in USER_SESSIONS:
                    return

                session = USER_SESSIONS[user_id]
                source = session.get('source')
                destination = session.get('destination')

                if not source or not destination:
                    return

                # Format channel IDs
                chat_id = str(event.chat_id)
                if not chat_id.startswith('-100'):
                    chat_id = f"-100{chat_id.lstrip('-')}"

                source_id = str(source)
                if not source_id.startswith('-100'):
                    source_id = f"-100{source_id.lstrip('-')}"

                # Compare exact channel IDs
                if chat_id != source_id:
                    return

                # Get message mapping
                msg_ids = MESSAGE_IDS.get(user_id, {})
                dest_msg_id = msg_ids.get(event.message.id)
                if not dest_msg_id:
                    return

                # Process edited message
                message = event.message
                message_text = message.text if message.text else ""
                if message_text:
                    message_text = apply_text_replacements(message_text, user_id)

                # Format destination ID
                dest_id = str(destination)
                if not dest_id.startswith('-100'):
                    dest_id = f"-100{dest_id.lstrip('-')}"

                try:
                    # Edit destination message
                    dest_channel = await client.get_entity(int(dest_id))
                    await client.edit_message(
                        dest_channel,
                        dest_msg_id,
                        message_text,
                        file=message.media if message.media else None,
                        formatting_entities=message.entities
                    )
                    logger.info(f"✅ Message edit synced")

                except Exception as e:
                    logger.error(f"❌ Edit sync error: {str(e)}")

            except Exception as e:
                logger.error(f"❌ Edit handler error: {str(e)}")

        logger.info(f"✅ Event handlers set up for user {user_id}")
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

                # Setup handlers
                if await setup_user_handlers(user_id, client):
                    # Start session management loop
                    asyncio.create_task(manage_user_session(user_id))
                    logger.info(f"✅ Session started for user {user_id}")
                    return True
            return False
        except Exception as e:
            logger.error(f"❌ Session setup error for user {user_id}: {str(e)}")
            return False

    # Run setup in event loop
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    success = loop.run_until_complete(setup_session())
    loop.close()
    return success

def remove_user_session(user_id):
    """Remove a user's session"""
    if user_id in USER_SESSIONS:
        try:
            session = USER_SESSIONS[user_id]
            client = session.get('client')
            if client:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                loop.run_until_complete(client.disconnect())
                loop.close()
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
        USER_SESSIONS[user_id]['replacements'] = load_user_replacements(user_id)
        logger.info(f"✅ Replacements updated for user {user_id}")

if __name__ == "__main__":
    try:
        # Start health check server in a separate thread
        health_thread = threading.Thread(
            target=lambda: health_app.run(host='0.0.0.0', port=9001, debug=False),
            daemon=True
        )
        health_thread.start()
        logger.info("✅ Started health check server")

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
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
from collections import deque

# Create a small Flask app for health checks
health_app = Flask(__name__)

@health_app.route('/')
def health_check():
    return jsonify({"status": "ok"}), 200

# Global variables
MESSAGE_QUEUE = deque(maxlen=1000)  # Store messages when client is reconnecting
MESSAGE_IDS = {}  # source_msg_id: destination_msg_id mapping
TEXT_REPLACEMENTS = {}
SOURCE_CHANNEL = None
DESTINATION_CHANNEL = None
client = None

# Set up logging
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# API credentials
API_ID = int(os.getenv('API_ID', '27202142'))
API_HASH = os.getenv('API_HASH', 'db4dd0d95dc68d46b77518bf997ed165')

# Database connection pool
db_pool = psycopg2.pool.ThreadedConnectionPool(
    minconn=1,
    maxconn=10,
    dsn=os.getenv('DATABASE_URL')
)

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

def get_bot_state():
    """Get current bot state from database"""
    conn = None
    try:
        conn = get_db()
        if not conn:
            return None

        with conn.cursor(cursor_factory=DictCursor) as cur:
            # First check if we need to update schema
            cur.execute("""
                SELECT column_name 
                FROM information_schema.columns 
                WHERE table_name = 'bot_state' 
                AND column_name = 'reconnect_attempts'
            """)
            if not cur.fetchone():
                cur.execute("""
                    ALTER TABLE bot_state 
                    ADD COLUMN reconnect_attempts INTEGER DEFAULT 0
                """)

            cur.execute("""
                SELECT 
                    session_string, 
                    source_channel, 
                    destination_channel, 
                    is_running,
                    COALESCE(reconnect_attempts, 0) as reconnect_attempts
                FROM bot_state
                WHERE is_running = true
                ORDER BY updated_at DESC
                LIMIT 1
            """)
            return cur.fetchone()
    except Exception as e:
        logger.error(f"❌ Bot state query error: {str(e)}")
        return None
    finally:
        if conn:
            release_db(conn)

def update_reconnect_attempts(attempts):
    """Update reconnection attempts counter"""
    conn = None
    try:
        conn = get_db()
        if not conn:
            return False

        with conn.cursor() as cur:
            cur.execute("""
                UPDATE bot_state 
                SET reconnect_attempts = %s,
                    updated_at = CURRENT_TIMESTAMP
                WHERE is_running = true
            """, (attempts,))
            conn.commit()  # Ensure changes are committed
        return True
    except Exception as e:
        logger.error(f"❌ Reconnect attempts update error: {str(e)}")
        return False
    finally:
        if conn:
            release_db(conn)

def load_channel_config():
    """Load channel configuration from database"""
    global SOURCE_CHANNEL, DESTINATION_CHANNEL
    conn = None
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
        logger.error(f"❌ Channel config error: {str(e)}")
        return False
    finally:
        if conn:
            release_db(conn)

def load_replacements():
    """Load text replacements from database"""
    global TEXT_REPLACEMENTS
    conn = None
    try:
        conn = get_db()
        if not conn:
            return False

        with conn.cursor(cursor_factory=DictCursor) as cur:
            # Clear existing replacements first
            TEXT_REPLACEMENTS.clear()

            cur.execute("""
                SELECT original_text, replacement_text 
                FROM text_replacements 
                ORDER BY LENGTH(original_text) DESC
            """)

            for row in cur.fetchall():
                TEXT_REPLACEMENTS[row['original_text']] = row['replacement_text']

            logger.info(f"✅ Loaded {len(TEXT_REPLACEMENTS)} replacements")
            return True

    except Exception as e:
        logger.error(f"❌ Replacements error: {str(e)}")
        TEXT_REPLACEMENTS.clear()
        return False
    finally:
        if conn:
            release_db(conn)

def apply_text_replacements(text):
    """Apply text replacements to message"""
    if not text:
        return text

    result = text
    for original, replacement in TEXT_REPLACEMENTS.items():
        if original in result:
            result = result.replace(original, replacement)
            logger.info(f"✅ Replaced: {original} → {replacement}")

    return result

async def process_queued_messages():
    """Process any messages that were queued during client disconnection"""
    global MESSAGE_QUEUE

    if not client or not client.is_connected():
        return

    while MESSAGE_QUEUE:
        try:
            message = MESSAGE_QUEUE.popleft()
            text = message.get('text', '')
            entities = message.get('entities', None)

            if text and TEXT_REPLACEMENTS:
                text = apply_text_replacements(text)

            dest_id = str(DESTINATION_CHANNEL)
            if not dest_id.startswith('-100'):
                dest_id = f"-100{dest_id.lstrip('-')}"

            dest_channel = await client.get_entity(int(dest_id))
            sent_message = await client.send_message(
                dest_channel,
                text,
                formatting_entities=entities
            )
            MESSAGE_IDS[message['id']] = sent_message.id
            logger.info("✅ Queued message processed")
        except Exception as e:
            logger.error(f"❌ Error processing queued message: {str(e)}")
            # Put message back in queue if processing failed
            MESSAGE_QUEUE.appendleft(message)
            break

async def setup_client(session_string=None):
    """Initialize Telegram client with session string"""
    global client
    try:
        # Check if we already have a working client
        if client and client.is_connected() and await client.is_user_authorized():
            return True

        # Get bot state if no session string provided
        if not session_string:
            bot_state = get_bot_state()
            if not bot_state or not bot_state['is_running']:
                logger.warning("⚠️ No active bot state found")
                return False
            session_string = bot_state['session_string']

        if not session_string:
            logger.warning("⚠️ No session string available")
            return False

        # Create new client instance with persistent connection settings
        client = TelegramClient(
            StringSession(session_string),
            API_ID,
            API_HASH,
            device_model="Replit Bot",
            system_version="Linux",
            app_version="1.0",
            retry_delay=1,
            connection_retries=None,  # Infinite retries
            auto_reconnect=True,
            request_retries=10,
            flood_sleep_threshold=60  # Increase flood wait handling
        )

        await client.connect()
        if not await client.is_user_authorized():
            logger.error("❌ Bot not authorized")
            await client.disconnect()
            client = None
            return False

        # Setup event handlers with improved error handling and persistence
        @client.on(events.NewMessage())
        async def handle_new_message(event):
            try:
                if not SOURCE_CHANNEL or not DESTINATION_CHANNEL:
                    return

                chat_id = str(event.chat_id)
                source_id = str(SOURCE_CHANNEL)
                if not chat_id.startswith('-100'):
                    chat_id = f"-100{chat_id.lstrip('-')}"
                if not source_id.startswith('-100'):
                    source_id = f"-100{source_id.lstrip('-')}"

                if chat_id != source_id:
                    return

                # Queue message if client is reconnecting
                if not client or not client.is_connected():
                    MESSAGE_QUEUE.append({
                        'id': event.message.id,
                        'text': event.message.text,
                        'entities': event.message.entities
                    })
                    logger.info("✅ Message queued for later processing")
                    return

                message_text = event.message.text if event.message.text else ""
                if message_text and TEXT_REPLACEMENTS:
                    message_text = apply_text_replacements(message_text)

                dest_id = str(DESTINATION_CHANNEL)
                if not dest_id.startswith('-100'):
                    dest_id = f"-100{dest_id.lstrip('-')}"

                for retry in range(3):  # Add retries for sending messages
                    try:
                        dest_channel = await client.get_entity(int(dest_id))
                        sent_message = await client.send_message(
                            dest_channel,
                            message_text,
                            formatting_entities=event.message.entities
                        )
                        MESSAGE_IDS[event.message.id] = sent_message.id
                        logger.info("✅ Message forwarded")
                        break
                    except Exception as e:
                        if retry == 2:  # Last retry
                            raise
                        await asyncio.sleep(1)  # Wait before retry

            except Exception as e:
                logger.error(f"❌ Message handler error: {str(e)}")
                # Queue message on error
                if event.message:
                    MESSAGE_QUEUE.append({
                        'id': event.message.id,
                        'text': event.message.text,
                        'entities': event.message.entities
                    })
                    logger.info("✅ Message queued after error")

        @client.on(events.MessageEdited())
        async def handle_edit(event):
            try:
                if not SOURCE_CHANNEL or not DESTINATION_CHANNEL:
                    return

                chat_id = str(event.chat_id)
                source_id = str(SOURCE_CHANNEL)
                if not chat_id.startswith('-100'):
                    chat_id = f"-100{chat_id.lstrip('-')}"
                if not source_id.startswith('-100'):
                    source_id = f"-100{source_id.lstrip('-')}"

                if chat_id != source_id:
                    return

                dest_msg_id = MESSAGE_IDS.get(event.message.id)
                if not dest_msg_id:
                    return

                message_text = event.message.text
                if message_text and TEXT_REPLACEMENTS:
                    message_text = apply_text_replacements(message_text)

                dest_id = str(DESTINATION_CHANNEL)
                if not dest_id.startswith('-100'):
                    dest_id = f"-100{dest_id.lstrip('-')}"

                dest_channel = await client.get_entity(int(dest_id))
                await client.edit_message(
                    dest_channel,
                    dest_msg_id,
                    message_text,
                    formatting_entities=event.message.entities
                )
                logger.info("✅ Edit synced")

            except Exception as e:
                logger.error(f"❌ Edit handler error: {str(e)}")

        @client.on(events.Raw())
        async def handle_disconnect(event):
            if isinstance(event, events.ConnectionLost):
                logger.warning("⚠️ Connection lost, will auto-reconnect...")

        me = await client.get_me()
        logger.info(f"✅ Bot running as: {me.first_name} (ID: {me.id})")
        return True

    except Exception as e:
        logger.error(f"❌ Client setup error: {str(e)}")
        if client:
            try:
                await client.disconnect()
            except:
                pass
            client = None
        return False

class HealthServer:
    def __init__(self):
        self.port = 8084
        self.max_retries = 3
        self.current_retry = 0

    def start(self):
        """Start health check server with port fallback"""
        while self.current_retry < self.max_retries:
            try:
                logger.info(f"Starting health server on port {self.port}")
                health_app.run(host='0.0.0.0', port=self.port)
                break
            except Exception as e:
                logger.error(f"❌ Health server error on port {self.port}: {str(e)}")
                self.current_retry += 1
                if self.current_retry < self.max_retries:
                    self.port = 9000 + self.current_retry  # Try ports 9001, 9002
                    continue
                logger.error("❌ All health server attempts failed")
                break

async def main():
    """Main bot function"""
    global client, SOURCE_CHANNEL, DESTINATION_CHANNEL

    while True:  # Keep checking for bot state
        try:
            # Get current bot state
            bot_state = get_bot_state()
            if not bot_state or not bot_state['is_running']:
                logger.info("⏸️ Bot is paused")
                await asyncio.sleep(5)  # Check every 5 seconds
                continue

            # Update global variables
            SOURCE_CHANNEL = bot_state['source_channel']
            DESTINATION_CHANNEL = bot_state['destination_channel']
            reconnect_attempts = bot_state['reconnect_attempts']

            # Check reconnect attempts
            if reconnect_attempts >= 10:
                logger.error("❌ Max reconnection attempts reached")
                with get_db() as conn:
                    with conn.cursor() as cur:
                        cur.execute("UPDATE bot_state SET is_running = false WHERE is_running = true")
                await asyncio.sleep(60)  # Wait before allowing new attempts
                continue

            # Setup client if needed
            if not client or not client.is_connected():
                # Try to setup client
                if not await setup_client(bot_state['session_string']):
                    reconnect_attempts += 1
                    update_reconnect_attempts(reconnect_attempts)
                    await asyncio.sleep(5 * (reconnect_attempts + 1))  # Exponential backoff
                    continue

                # Reset reconnect attempts on success
                update_reconnect_attempts(0)
                logger.info("✅ Client connected and ready")

                # Load configuration
                if not load_channel_config():
                    logger.error("❌ Failed to load channels")
                    continue

                # Load replacements
                if not load_replacements():
                    logger.warning("⚠️ No replacements loaded")

                # Process any queued messages
                await process_queued_messages()

            # Keep the bot running and check state periodically
            await asyncio.sleep(30)  # Check every 30 seconds

        except Exception as e:
            logger.error(f"❌ Main loop error: {str(e)}")
            if client:
                try:
                    await client.disconnect()
                except:
                    pass
                client = None
            await asyncio.sleep(5)

if __name__ == "__main__":
    try:
        # Start health check server in a separate thread
        health_server = HealthServer()
        health_thread = threading.Thread(
            target=health_server.start,
            daemon=True
        )
        health_thread.start()

        # Run bot
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(main())
        except KeyboardInterrupt:
            logger.info("👋 Bot stopped by user")
        finally:
            if client and client.is_connected():
                loop.run_until_complete(client.disconnect())
            loop.close()

    except Exception as e:
        logger.error(f"❌ Fatal error: {str(e)}")
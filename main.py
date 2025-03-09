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

# Global variables
MESSAGE_IDS = {}  # source_msg_id: destination_msg_id mapping
TEXT_REPLACEMENTS = {}
SOURCE_CHANNEL = None
DESTINATION_CHANNEL = None
client = None
SESSION_STRING = None

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

async def setup_client(max_retries=3, retry_delay=5):
    """Initialize Telegram client with session string and retry logic"""
    global client, SESSION_STRING

    for attempt in range(max_retries):
        try:
            # Get latest session string from database if not set
            if not SESSION_STRING:
                conn = get_db()
                if conn:
                    try:
                        with conn.cursor(cursor_factory=DictCursor) as cur:
                            cur.execute("""
                                SELECT session_string 
                                FROM bot_status 
                                WHERE is_running = true 
                                ORDER BY updated_at DESC 
                                LIMIT 1
                            """)
                            result = cur.fetchone()
                            if result and result['session_string']:
                                SESSION_STRING = result['session_string']
                    finally:
                        release_db(conn)

            if not SESSION_STRING:
                logger.warning("⚠️ No session string available")
                return False

            # Create new client instance
            client = TelegramClient(
                StringSession(SESSION_STRING),
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
                logger.error("❌ Connection timeout, retrying...")
                if client:
                    await client.disconnect()
                time.sleep(retry_delay)
                continue

            # Verify authorization
            if not await client.is_user_authorized():
                logger.error("❌ Bot not authorized")
                await client.disconnect()
                client = None
                return False

            me = await client.get_me()
            logger.info(f"✅ Bot running as: {me.first_name} (ID: {me.id})")
            return True

        except Exception as e:
            logger.error(f"❌ Client setup error (attempt {attempt + 1}/{max_retries}): {str(e)}")
            if client:
                try:
                    await client.disconnect()
                except:
                    pass
            client = None
            time.sleep(retry_delay)

    logger.error("❌ All connection attempts failed")
    return False

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
    # Always reload replacements before applying
    load_replacements()

    if not text:
        return text

    result = text
    for original, replacement in TEXT_REPLACEMENTS.items():
        if original in result:
            result = result.replace(original, replacement)
            logger.info(f"✅ Replaced: {original} → {replacement}")

    return result

async def setup_handlers():
    """Set up message handlers"""
    global client
    try:
        @client.on(events.NewMessage())
        async def handle_new_message(event):
            try:
                if not SOURCE_CHANNEL or not DESTINATION_CHANNEL:
                    return

                # Format channel IDs
                chat_id = str(event.chat_id)
                source_id = str(SOURCE_CHANNEL)
                if not chat_id.startswith('-100'):
                    chat_id = f"-100{chat_id.lstrip('-')}"
                if not source_id.startswith('-100'):
                    source_id = f"-100{source_id.lstrip('-')}"

                # Verify source channel
                if chat_id != source_id:
                    return

                # Process message
                message_text = event.message.text if event.message.text else ""
                if message_text and TEXT_REPLACEMENTS:
                    message_text = apply_text_replacements(message_text)

                # Format destination channel ID
                dest_id = str(DESTINATION_CHANNEL)
                if not dest_id.startswith('-100'):
                    dest_id = f"-100{dest_id.lstrip('-')}"

                # Send to destination
                dest_channel = await client.get_entity(int(dest_id))
                sent_message = await client.send_message(
                    dest_channel,
                    message_text,
                    formatting_entities=event.message.entities
                )
                MESSAGE_IDS[event.message.id] = sent_message.id
                logger.info("✅ Message forwarded")

            except Exception as e:
                logger.error(f"❌ Message handler error: {str(e)}")

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

        return True

    except Exception as e:
        logger.error(f"❌ Handler setup error: {str(e)}")
        return False

async def main():
    """Main bot function"""
    global client, SOURCE_CHANNEL, DESTINATION_CHANNEL

    try:
        # Setup client
        if not await setup_client():
            return False

        # Load configuration
        if not load_channel_config():
            logger.error("❌ Failed to load channels")
            return False

        # Load replacements
        if not load_replacements():
            logger.warning("⚠️ No replacements loaded")

        # Setup handlers
        if not await setup_handlers():
            logger.error("❌ Failed to setup handlers")
            return False

        logger.info("\n🤖 Bot is ready")
        logger.info(f"📱 Source: {SOURCE_CHANNEL}")
        logger.info(f"📱 Destination: {DESTINATION_CHANNEL}")
        logger.info(f"📚 Replacements: {len(TEXT_REPLACEMENTS)}")

        # Keep the bot running
        try:
            while True:
                # Check if bot should still be running from database
                conn = get_db()
                if conn:
                    try:
                        with conn.cursor(cursor_factory=DictCursor) as cur:
                            cur.execute("SELECT is_running FROM bot_status ORDER BY updated_at DESC LIMIT 1")
                            result = cur.fetchone()
                            if not result or not result['is_running']:
                                logger.info("👋 Bot stopped by user")
                                break
                    finally:
                        release_db(conn)

                # Check client connection and reconnect if needed
                if not client or not client.is_connected():
                    logger.error("❌ Client disconnected, attempting to reconnect")
                    if not await setup_client():
                        # If reconnection fails, check if we should still be running
                        continue

                # Reload configuration and replacements
                if load_channel_config():
                    logger.info("✅ Channel config refreshed")
                if load_replacements():
                    logger.info("✅ Replacements refreshed")

                # Wait before next check
                await asyncio.sleep(30)

        except KeyboardInterrupt:
            logger.info("👋 Bot stopped by user")
            return True
        except asyncio.CancelledError:
            logger.info("👋 Bot stopping...")
            return True
        except Exception as e:
            logger.error(f"❌ Runtime error: {str(e)}")
            return False

    except Exception as e:
        logger.error(f"❌ Bot error: {str(e)}")
        return False

    finally:
        if client:
            try:
                if client.is_connected():
                    await client.disconnect()
            except:
                pass
            client = None
        return True

def start_health_server():
    """Start health check server in a separate thread"""
    try:
        # Try ports in sequence until one works
        ports = [8084, 9001, 9002]
        for port in ports:
            try:
                health_app.run(host='0.0.0.0', port=port, debug=False)
                logger.info(f"✅ Health check server started on port {port}")
                break
            except Exception as e:
                logger.warning(f"⚠️ Failed to start health check server on port {port}: {str(e)}")
                if port == ports[-1]:
                    logger.error("❌ Could not start health check server on any port")
                continue

    except Exception as e:
        logger.error(f"❌ Health check server error: {str(e)}")

if __name__ == "__main__":
    try:
        # Only start health check server if bot is not running
        if not SESSION_STRING:
            # Start health check server in a separate thread
            health_thread = threading.Thread(
                target=start_health_server,
                daemon=True
            )
            health_thread.start()
            logger.info("✅ Started health check server thread")

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
        # Ensure the bot still runs even if health check fails
        if not SESSION_STRING:
            logger.warning("⚠️ No session string provided, waiting for configuration")
import os
import logging
import threading
from flask import Flask, render_template, request, session, redirect, url_for, jsonify
from telethon import TelegramClient, events
from telethon.errors import SessionPasswordNeededError, PhoneNumberInvalidError, PhoneCodeExpiredError, PhoneCodeInvalidError
from telethon.sessions import StringSession
import asyncio
from functools import wraps
import psycopg2
from psycopg2.extras import DictCursor
from psycopg2 import pool
from flask_session import Session
from datetime import timedelta
from contextlib import contextmanager

# Set up logging
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

class EventLoopManager:
    _instance = None
    _lock = threading.Lock()
    _loop = None

    @classmethod
    def get_loop(cls):
        if cls._loop is None:
            with cls._lock:
                if cls._loop is None:
                    try:
                        cls._loop = asyncio.get_event_loop()
                    except RuntimeError:
                        cls._loop = asyncio.new_event_loop()
                        asyncio.set_event_loop(cls._loop)
                        logger.info("✅ Created new event loop")
        return cls._loop

    @classmethod
    def ensure_loop(cls):
        """Ensure we have a valid event loop"""
        with cls._lock:
            try:
                loop = cls.get_loop()
                if not loop.is_running():
                    asyncio.set_event_loop(loop)
                return loop
            except Exception as e:
                logger.error(f"❌ Loop setup error: {str(e)}")
                cls.reset()
                return cls.get_loop()

    @classmethod
    def reset(cls):
        with cls._lock:
            try:
                if cls._loop and cls._loop.is_running():
                    cls._loop.stop()
                if cls._loop:
                    cls._loop.close()
            except:
                pass
            finally:
                cls._loop = None
                logger.info("✅ Reset event loop")

class TelegramManager:
    def __init__(self, api_id, api_hash):
        self.api_id = api_id
        self.api_hash = api_hash
        self._lock = threading.Lock()
        self._client = None

    async def get_auth_client(self):
        """Get a client for authentication or dashboard operations"""
        with self._lock:
            try:
                # Check if we have a valid session
                if self._client and self._client.is_connected() and await self._client.is_user_authorized():
                    return self._client

                # Get current session string
                session_string = session.get('session_string')

                # Create new client if needed
                if not self._client:
                    self._client = TelegramClient(
                        StringSession(session_string) if session_string else StringSession(),
                        self.api_id,
                        self.api_hash,
                        device_model="Replit Web",
                        system_version="Linux",
                        app_version="1.0",
                        loop=EventLoopManager.ensure_loop()
                    )

                # Connect if needed
                if not self._client.is_connected():
                    await self._client.connect()

                # Verify authorization if we have a session
                if session_string and not await self._client.is_user_authorized():
                    # Session expired, clear it
                    session.pop('session_string', None)
                    await self._cleanup_client()

                    # Create fresh client
                    self._client = TelegramClient(
                        StringSession(),
                        self.api_id,
                        self.api_hash,
                        device_model="Replit Web",
                        system_version="Linux",
                        app_version="1.0",
                        loop=EventLoopManager.ensure_loop()
                    )
                    await self._client.connect()

                return self._client

            except Exception as e:
                logger.error(f"❌ Client creation error: {str(e)}")
                await self._cleanup_client()
                raise

    async def _cleanup_client(self):
        """Internal method to cleanup client"""
        if self._client:
            try:
                if self._client.is_connected():
                    await self._client.disconnect()
            except:
                pass
            self._client = None


app = Flask(__name__)

# Configure Flask application
app.config.update(
    SECRET_KEY=os.urandom(24),
    SESSION_TYPE='filesystem',
    PERMANENT_SESSION_LIFETIME=timedelta(days=7),
    SESSION_PERMANENT=True
)

# Initialize session
Session(app)

# Database pool for connections
db_pool = psycopg2.pool.ThreadedConnectionPool(
    minconn=1,
    maxconn=10,
    dsn=os.getenv('DATABASE_URL')
)

# Database lock for thread safety
db_lock = threading.Lock()

# Database connection context manager
@contextmanager
def get_db():
    conn = db_pool.getconn()
    try:
        conn.autocommit = True
        yield conn
    finally:
        db_pool.putconn(conn)

def handle_db_error(e, operation):
    """Handle database errors and return appropriate messages"""
    error_msg = str(e)
    if "violates foreign key constraint" in error_msg:
        logger.error(f"❌ Foreign key error in {operation}: {error_msg}")
        return "Session expired, please login again"
    elif "violates unique constraint" in error_msg:
        logger.error(f"❌ Unique constraint error in {operation}: {error_msg}")
        if "text_replacements" in error_msg:
            return "This text replacement already exists"
        elif "channel_config" in error_msg:
            return "Channel configuration already exists"
        return "Operation failed due to duplicate entry"
    else:
        logger.error(f"❌ Database error in {operation}: {error_msg}")
        return "An unexpected error occurred"


# Authentication decorator
def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not session.get('logged_in') or not session.get('telegram_id'):
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function

# Async route decorator
def async_route(f):
    @wraps(f)
    def wrapped(*args, **kwargs):
        try:
            loop = EventLoopManager.get_loop()
            return loop.run_until_complete(f(*args, **kwargs))
        except Exception as e:
            logger.error(f"❌ Async route error: {str(e)}")
            raise
    return wrapped

# Create global Telegram manager
telegram_manager = TelegramManager(
    int(os.getenv('API_ID', '27202142')),
    os.getenv('API_HASH', 'db4dd0d95dc68d46b77518bf997ed165')
)

@app.route('/')
def login():
    try:
        if session.get('logged_in') and session.get('telegram_id'):
            logger.info("✅ User already logged in, redirecting to dashboard")
            return redirect(url_for('dashboard'))

        session.clear()
        return render_template('login.html')
    except Exception as e:
        logger.error(f"❌ Login route error: {str(e)}")
        return render_template('login.html')

@app.route('/send-otp', methods=['POST'])
@async_route
async def send_otp():
    try:
        phone = request.form.get('phone')
        if not phone:
            return jsonify({'error': 'Phone number is required'}), 400

        if not phone.startswith('+91'):
            return jsonify({'error': 'Phone number must start with +91'}), 400

        client = await telegram_manager.get_auth_client()
        session.clear()

        # Send OTP
        sent = await client.send_code_request(phone)
        session['user_phone'] = phone
        session['phone_code_hash'] = sent.phone_code_hash
        return jsonify({'message': 'OTP sent successfully'})

    except PhoneNumberInvalidError:
        return jsonify({'error': 'Invalid phone number'}), 400
    except Exception as e:
        logger.error(f"❌ Send OTP error: {str(e)}")
        return jsonify({'error': str(e)}), 500

@app.route('/verify-otp', methods=['POST'])
@async_route
async def verify_otp():
    try:
        phone = session.get('user_phone')
        phone_code_hash = session.get('phone_code_hash')
        otp = request.form.get('otp')
        password = request.form.get('password')

        if not phone or not phone_code_hash:
            session.clear()
            return jsonify({'error': 'Session expired. Please request a new OTP.'}), 400

        if not otp:
            return jsonify({'error': 'OTP is required'}), 400

        try:
            client = await telegram_manager.get_auth_client()

            try:
                await client.sign_in(phone, otp, phone_code_hash=phone_code_hash)
            except SessionPasswordNeededError:
                if not password:
                    return jsonify({
                        'error': 'two_factor_needed',
                        'message': 'Two-factor authentication required'
                    })
                try:
                    await client.sign_in(password=password)
                except Exception as e:
                    logger.error(f"❌ 2FA error: {str(e)}")
                    session.clear()
                    return jsonify({'error': 'Invalid 2FA password'}), 400

            if await client.is_user_authorized():
                # Get user info
                me = await client.get_me()

                # Store or update user in database with login status
                with get_db() as conn:
                    with conn.cursor() as cur:
                        cur.execute("""
                            INSERT INTO users (telegram_id, first_name, username, is_logged_in, last_login_at)
                            VALUES (%s, %s, %s, true, CURRENT_TIMESTAMP)
                            ON CONFLICT (telegram_id) DO UPDATE
                            SET first_name = EXCLUDED.first_name,
                                username = EXCLUDED.username,
                                is_logged_in = true,
                                last_login_at = CURRENT_TIMESTAMP
                            RETURNING id;
                        """, (me.id, me.first_name, me.username))

                # Set session data
                session['logged_in'] = True
                session['telegram_id'] = me.id
                session['session_string'] = client.session.save()
                logger.info(f"✅ Login successful for user {me.id}")
                return jsonify({'message': 'Login successful'})
            else:
                session.clear()
                return jsonify({'error': 'Authentication failed. Please try again.'}), 400

        except Exception as e:
            logger.error(f"❌ Sign in error: {str(e)}")
            session.clear()
            return jsonify({'error': str(e)}), 500

    except Exception as e:
        logger.error(f"❌ Critical error in verify_otp: {str(e)}")
        session.clear()
        return jsonify({'error': str(e)}), 500

@app.route('/logout')
def logout():
    try:
        user_id = session.get('telegram_id')
        if user_id:
            # Update login status in database
            with get_db() as conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        UPDATE users 
                        SET is_logged_in = false 
                        WHERE telegram_id = %s
                    """, (user_id,))

                    # Stop bot if running
                    cur.execute("""
                        UPDATE bot_status 
                        SET is_running = false,
                            session_string = NULL
                        WHERE user_id = %s
                    """, (user_id,))

            # Remove user session from main.py
            import main
            main.remove_user_session(user_id)

        session.clear()
        return redirect(url_for('login'))
    except Exception as e:
        logger.error(f"❌ Logout error: {str(e)}")
        session.clear()
        return redirect(url_for('login'))

@app.before_request
def check_session_expiry():
    if request.endpoint not in ['login', 'static', 'send-otp', 'verify-otp', 'check-auth', 'logout']:
        telegram_id = session.get('telegram_id')
        if not telegram_id:
            session.clear()
            return redirect(url_for('login'))

        # Verify user is still logged in in database
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT is_logged_in 
                    FROM users 
                    WHERE telegram_id = %s
                """, (telegram_id,))
                result = cur.fetchone()
                if not result or not result[0]:
                    session.clear()
                    return redirect(url_for('login'))

@app.route('/check-auth')
def check_auth():
    try:
        telegram_id = session.get('telegram_id')
        if not telegram_id:
            return jsonify({'authenticated': False}), 401

        # Verify user is logged in in database
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT is_logged_in 
                    FROM users 
                    WHERE telegram_id = %s
                """, (telegram_id,))
                result = cur.fetchone()
                if not result or not result[0]:
                    session.clear()
                    return jsonify({'authenticated': False}), 401

        return jsonify({'authenticated': True})
    except Exception as e:
        logger.error(f"❌ Auth check error: {str(e)}")
        return jsonify({'authenticated': False}), 401

@app.route('/dashboard')
@login_required
@async_route
async def dashboard():
    try:
        telegram_id = session.get('telegram_id')
        if not telegram_id:
            logger.error("❌ No telegram_id in session")
            session.clear()
            return redirect(url_for('login'))

        # Get client
        try:
            client = await telegram_manager.get_auth_client()
            if not await client.is_user_authorized():
                logger.error("❌ Client not authorized")
                session.clear()
                return redirect(url_for('login'))
        except Exception as e:
            logger.error(f"❌ Client error: {str(e)}")
            session.clear()
            return redirect(url_for('login'))

        # Get channels list
        channels = []
        try:
            async for dialog in client.iter_dialogs():
                if dialog.is_channel:
                    channels.append({
                        'id': dialog.id,
                        'name': dialog.name
                    })
        except Exception as e:
            logger.error(f"❌ Channel list error: {str(e)}")
            channels = []

        # Get user configuration
        last_config = None
        initial_bot_status = False
        try:
            with get_db() as conn:
                with conn.cursor(cursor_factory=DictCursor) as cur:
                    # Get channel config
                    cur.execute("""
                        SELECT source_channel, destination_channel 
                        FROM channel_config 
                        WHERE user_id = %s
                        ORDER BY updated_at DESC
                        LIMIT 1
                    """, (telegram_id,))
                    last_config = cur.fetchone()

                    # Get bot status
                    cur.execute("""
                        SELECT is_running, session_string 
                        FROM bot_status 
                        WHERE user_id = %s
                    """, (telegram_id,))
                    status_row = cur.fetchone()

                    if status_row and status_row['is_running']:
                        initial_bot_status = True
                        if last_config and status_row['session_string']:
                            import main
                            logger.info(f"✅ Initializing bot session for user {telegram_id}")
                            success = main.add_user_session(
                                user_id=telegram_id,
                                session_string=session.get('session_string'),
                                source_channel=last_config['source_channel'],
                                destination_channel=last_config['destination_channel']
                            )
                            if not success:
                                logger.error(f"❌ Failed to initialize bot session for user {telegram_id}")
                                # Update database to reflect actual status
                                cur.execute("""
                                    UPDATE bot_status 
                                    SET is_running = false,
                                        session_string = NULL
                                    WHERE user_id = %s
                                """, (telegram_id,))
                                initial_bot_status = False

        except Exception as e:
            logger.error(f"❌ Database error: {str(e)}")

        return render_template('dashboard.html',
                            channels=channels,
                            last_source=last_config['source_channel'] if last_config else None,
                            last_dest=last_config['destination_channel'] if last_config else None,
                            initial_bot_status=initial_bot_status)

    except Exception as e:
        logger.error(f"❌ Dashboard error: {str(e)}")
        session.clear()
        return redirect(url_for('login'))

@app.route('/update-channels', methods=['POST'])
@login_required
def update_channels():
    try:
        source = request.form.get('source')
        destination = request.form.get('destination')
        telegram_id = session.get('telegram_id')

        if not all([source, destination, telegram_id]):
            return jsonify({'error': 'Missing required data'}), 400

        if source == destination:
            return jsonify({'error': 'Source and destination channels cannot be the same'}), 400

        # Format channel IDs
        if not source.startswith('-100'):
            source = f"-100{source.lstrip('-')}"
        if not destination.startswith('-100'):
            destination = f"-100{destination.lstrip('-')}"

        with get_db() as conn:
            with conn.cursor() as cur:
                try:
                    cur.execute("""
                        INSERT INTO channel_config (user_id, source_channel, destination_channel)
                        VALUES (%s, %s, %s)
                        ON CONFLICT (user_id) 
                        DO UPDATE SET 
                            source_channel = EXCLUDED.source_channel,
                            destination_channel = EXCLUDED.destination_channel,
                            updated_at = CURRENT_TIMESTAMP
                    """, (telegram_id, source, destination))
                except psycopg2.Error as e:
                    error_msg = handle_db_error(e, "update_channels")
                    return jsonify({'error': error_msg}), 400

        # Update running bot if exists
        import main
        main.update_user_channels(telegram_id, source, destination)

        return jsonify({'message': 'Channels updated successfully'})

    except Exception as e:
        logger.error(f"❌ Channel update error: {str(e)}")
        return jsonify({'error': str(e)}), 500

@app.route('/bot/toggle', methods=['POST'])
@login_required
def toggle_bot():
    try:
        status = request.form.get('status') == 'true'
        telegram_id = session.get('telegram_id')
        session_string = session.get('session_string')

        if not telegram_id:
            return jsonify({'error': 'User not authenticated'}), 401

        if not session_string:
            return jsonify({'error': 'Session expired, please login again'}), 401

        # Get channels
        with get_db() as conn:
            with conn.cursor(cursor_factory=DictCursor) as cur:
                cur.execute("""
                    SELECT source_channel, destination_channel
                    FROM channel_config
                    WHERE user_id = %s
                """, (telegram_id,))
                channels = cur.fetchone()

        if not channels:
            return jsonify({'error': 'Please configure channels first'}), 400

        if channels['source_channel'] == channels['destination_channel']:
            return jsonify({'error': 'Source and destination channels cannot be the same'}), 400

        import main
        if status:
            try:
                # Update database first
                with get_db() as conn:
                    with conn.cursor() as cur:
                        cur.execute("""
                            INSERT INTO bot_status (user_id, is_running, session_string)
                            VALUES (%s, true, %s)
                            ON CONFLICT (user_id) DO UPDATE
                            SET is_running = true,
                                session_string = EXCLUDED.session_string,
                                updated_at = CURRENT_TIMESTAMP
                        """, (telegram_id, session_string))

                # Start bot
                success = main.add_user_session(
                    user_id=telegram_id,
                    session_string=session_string,
                    source_channel=channels['source_channel'],
                    destination_channel=channels['destination_channel']
                )

                if not success:
                    return jsonify({'error': 'Failed to start bot. Please try again.'}), 500

                return jsonify({
                    'status': True,
                    'message': 'Bot is now running'
                })

            except Exception as e:
                logger.error(f"❌ Bot start error: {str(e)}")
                return jsonify({'error': str(e)}), 500
        else:
            try:
                # Update database first
                with get_db() as conn:
                    with conn.cursor() as cur:
                        cur.execute("""
                            INSERT INTO bot_status (user_id, is_running, session_string)
                            VALUES (%s, false, NULL)
                            ON CONFLICT (user_id) DO UPDATE
                            SET is_running = false,
                                session_string = NULL,
                                updated_at = CURRENT_TIMESTAMP
                        """, (telegram_id,))

                # Stop bot
                main.remove_user_session(telegram_id)

                return jsonify({
                    'status': False,
                    'message': 'Bot is now stopped'
                })

            except Exception as e:
                logger.error(f"❌ Bot stop error: {str(e)}")
                return jsonify({'error': str(e)}), 500

    except Exception as e:
        logger.error(f"❌ Bot toggle error: {str(e)}")
        return jsonify({'error': str(e)}), 500

@app.route('/get-replacements')
@login_required
def get_replacements():
    try:
        telegram_id = session.get('telegram_id')

        with get_db() as conn:
            with conn.cursor(cursor_factory=DictCursor) as cur:
                cur.execute("""
                    SELECT original_text, replacement_text 
                    FROM text_replacements
                    WHERE user_id = %s
                    ORDER BY id DESC
                """, (telegram_id,))
                replacements = {row['original_text']: row['replacement_text'] for row in cur.fetchall()}
                return jsonify(replacements)
    except Exception as e:
        logger.error(f"❌ Get replacements error: {str(e)}")
        return jsonify({'error': str(e)}), 500

@app.route('/add-replacement', methods=['POST'])
@login_required
def add_replacement():
    try:
        original = request.form.get('original')
        replacement = request.form.get('replacement')
        telegram_id = session.get('telegram_id')

        if not all([original, replacement, telegram_id]):
            return jsonify({'error': 'Missing required data'}), 400

        with get_db() as conn:
            with conn.cursor() as cur:
                try:
                    cur.execute("""
                        INSERT INTO text_replacements (user_id, original_text, replacement_text)
                        VALUES (%s, %s, %s)
                    """, (telegram_id, original, replacement))
                except psycopg2.Error as e:
                    error_msg = handle_db_error(e, "add_replacement")
                    return jsonify({'error': error_msg}), 400

        # Update bot
        import main
        main.update_user_replacements(telegram_id)

        return jsonify({'message': 'Replacement added successfully'})
    except Exception as e:
        logger.error(f"❌ Add replacement error: {str(e)}")
        return jsonify({'error': str(e)}), 500

@app.route('/remove-replacement', methods=['POST'])
@login_required
def remove_replacement():
    try:
        original = request.form.get('original')
        telegram_id = session.get('telegram_id')

        if not all([original, telegram_id]):
            return jsonify({'error': 'Missing required data'}), 400

        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    DELETE FROM text_replacements 
                    WHERE user_id = %s AND original_text = %s
                """, (telegram_id, original))

        # Update bot
        import main
        main.update_user_replacements(telegram_id)

        return jsonify({'message': 'Replacement removed successfully'})
    except Exception as e:
        logger.error(f"❌ Remove replacement error: {str(e)}")
        return jsonify({'error': str(e)}), 500

@app.route('/clear-replacements', methods=['POST'])
@login_required
def clear_replacements():
    try:
        telegram_id = session.get('telegram_id')

        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    DELETE FROM text_replacements
                    WHERE user_id = %s
                """, (telegram_id,))

        # Update bot
        import main
        main.update_user_replacements(telegram_id)

        return jsonify({'message': 'All replacements cleared'})
    except Exception as e:
        logger.error(f"❌ Clear replacements error: {str(e)}")
        return jsonify({'error': str(e)}), 500

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
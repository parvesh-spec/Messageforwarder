import os
import logging
import threading
from flask import Flask, render_template, request, session, redirect, url_for, jsonify, send_from_directory
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

app = Flask(__name__)

# Configure Flask application
app.config.update(
    SECRET_KEY=os.getenv('SESSION_SECRET', os.urandom(24)),
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

# Event Loop Manager
class EventLoopManager:
    _instance = None
    _lock = threading.Lock()
    _loop = None
    _client = None

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
                cls._client = None
                logger.info("✅ Reset event loop")

    @classmethod
    def ensure_loop(cls):
        """Ensure we have a valid event loop"""
        try:
            loop = cls.get_loop()
            if not loop.is_running():
                asyncio.set_event_loop(loop)
            return loop
        except Exception as e:
            logger.error(f"❌ Loop setup error: {str(e)}")
            cls.reset()
            return cls.get_loop()

# Database connection context manager
@contextmanager
def get_db():
    conn = db_pool.getconn()
    try:
        conn.autocommit = True
        yield conn
    finally:
        db_pool.putconn(conn)

def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not session.get('logged_in'):
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function

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

# Telegram client manager
class TelegramManager:
    def __init__(self, api_id, api_hash):
        self.api_id = api_id
        self.api_hash = api_hash
        self._lock = threading.Lock()
        self._client = None
        self._session_string = None

    async def get_auth_client(self):
        """Get a client for authentication or dashboard operations"""
        with self._lock:
            try:
                # Check if we have a valid session
                if self._client and self._client.is_connected() and await self._client.is_user_authorized():
                    return self._client

                # Get current session string
                session_string = session.get('session_string')

                # If session string changed, cleanup old client
                if session_string != self._session_string:
                    await self._cleanup_client()
                    self._session_string = session_string

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
                    self._session_string = None
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

    def cleanup(self):
        """Clean up resources"""
        with self._lock:
            if self._client:
                try:
                    if self._client.is_connected():
                        loop = EventLoopManager.get_loop()
                        loop.run_until_complete(self._client.disconnect())
                except:
                    pass
                self._client = None
            self._session_string = None
            EventLoopManager.reset()

    @property
    def current_session(self):
        """Get current session string"""
        return self._session_string

# Create global Telegram manager
telegram_manager = TelegramManager(
    int(os.getenv('API_ID', '27202142')),
    os.getenv('API_HASH', 'db4dd0d95dc68d46b77518bf997ed165')
)

@app.route('/')
def root():
    """Redirect to login page"""
    if session.get('logged_in'):
        logger.info("✅ User already logged in, redirecting to dashboard")
        return redirect(url_for('dashboard'))
    return redirect(url_for('login'))

@app.route('/health')
def health():
    """Health check endpoint"""
    return jsonify({'status': 'healthy'}), 200

@app.route('/login')
def login():
    if session.get('logged_in'):
        logger.info("✅ User already logged in, redirecting to dashboard")
        return redirect(url_for('dashboard'))
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

        try:
            client = await telegram_manager.get_auth_client()

            # Clear any existing session data
            session.clear()

            # Check if already authorized
            if await client.is_user_authorized():
                session['user_phone'] = phone
                session['logged_in'] = True
                session['session_string'] = client.session.save()
                return jsonify({'message': 'Already authorized', 'already_authorized': True})

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

    except Exception as e:
        logger.error(f"❌ Critical error in send_otp: {str(e)}")
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
            return jsonify({'error': 'Session expired. Please request a new OTP.'}), 400

        if not otp:
            return jsonify({'error': 'OTP is required'}), 400

        try:
            client = await telegram_manager.get_auth_client()

            try:
                # Try signing in
                await client.sign_in(phone, otp, phone_code_hash=phone_code_hash)

            except PhoneCodeExpiredError:
                session.pop('phone_code_hash', None)
                return jsonify({'error': 'OTP has expired. Please request a new one.'}), 400

            except PhoneCodeInvalidError:
                return jsonify({'error': 'Invalid OTP. Please try again.'}), 400

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
                    session.pop('phone_code_hash', None)
                    return jsonify({'error': 'Invalid 2FA password'}), 400

            if await client.is_user_authorized():
                session['logged_in'] = True
                session['session_string'] = client.session.save()
                logger.info("✅ Login successful")
                return jsonify({'message': 'Login successful'})
            else:
                session.pop('phone_code_hash', None)
                return jsonify({'error': 'Authentication failed. Please try again.'}), 400

        except Exception as e:
            logger.error(f"❌ Sign in error: {str(e)}")
            session.pop('phone_code_hash', None)
            return jsonify({'error': str(e)}), 500

    except Exception as e:
        logger.error(f"❌ Critical error in verify_otp: {str(e)}")
        return jsonify({'error': str(e)}), 500

@app.route('/dashboard')
@login_required
@async_route
async def dashboard():
    try:
        # Verify session string exists
        if not session.get('session_string'):
            logger.error("❌ No session string found")
            return redirect(url_for('login'))

        # Get client using event loop manager
        client = await telegram_manager.get_auth_client()
        if not await client.is_user_authorized():
            logger.error("❌ Client not authorized")
            return redirect(url_for('login'))

        channels = []
        async for dialog in client.iter_dialogs():
            if dialog.is_channel:
                channels.append({
                    'id': dialog.id,
                    'name': dialog.name
                })

        # Get last selected channels
        with get_db() as conn:
            with conn.cursor(cursor_factory=DictCursor) as cur:
                cur.execute("""
                    SELECT source_channel, destination_channel 
                    FROM channel_config 
                    ORDER BY updated_at DESC 
                    LIMIT 1
                """)
                last_config = cur.fetchone()

        return render_template('dashboard.html', 
                            channels=channels,
                            last_source=last_config['source_channel'] if last_config else None,
                            last_dest=last_config['destination_channel'] if last_config else None)

    except Exception as e:
        logger.error(f"❌ Dashboard error: {str(e)}")
        return redirect(url_for('login'))

@app.route('/logout')
def logout():
    try:
        # Clean up Telegram resources
        telegram_manager.cleanup()

        # Clear flask session
        session.clear()
        logger.info("✅ Logged out successfully")
        return redirect(url_for('login'))
    except Exception as e:
        logger.error(f"❌ Logout error: {str(e)}")
        return redirect(url_for('login'))

@app.route('/update-channels', methods=['POST'])
@login_required
def update_channels():
    try:
        source = request.form.get('source')
        destination = request.form.get('destination')

        if not source or not destination:
            return jsonify({'error': 'Both channels required'}), 400

        # Format channel IDs
        if not source.startswith('-100'):
            source = f"-100{source.lstrip('-')}"
        if not destination.startswith('-100'):
            destination = f"-100{destination.lstrip('-')}"

        # Save to database
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO channel_config (source_channel, destination_channel)
                    VALUES (%s, %s)
                """, (source, destination))

        # Update session
        session['source_channel'] = source
        session['dest_channel'] = destination

        return jsonify({'message': 'Channels updated successfully'})

    except Exception as e:
        logger.error(f"❌ Channel update error: {str(e)}")
        return jsonify({'error': str(e)}), 500

@app.route('/bot/toggle', methods=['POST'])
@login_required
def toggle_bot():
    try:
        status = request.form.get('status') == 'true'
        source = session.get('source_channel')
        destination = session.get('dest_channel')

        if not source or not destination:
            logger.error("❌ Missing channel configuration")
            return jsonify({'error': 'Configure channels first'}), 400

        session_string = session.get('session_string')
        if not session_string:
            logger.error("❌ No session string found")
            return jsonify({'error': 'Session expired, please login again'}), 401

        import main
        if status:
            try:
                # Share session with main.py
                main.SESSION_STRING = session_string
                main.SOURCE_CHANNEL = source
                main.DESTINATION_CHANNEL = destination

                # Start bot in a daemon thread
                def start_bot():
                    try:
                        # Create new event loop for bot thread
                        loop = asyncio.new_event_loop()
                        asyncio.set_event_loop(loop)

                        try:
                            loop.run_until_complete(main.main())
                        except Exception as e:
                            logger.error(f"❌ Bot startup error: {str(e)}")
                        finally:
                            try:
                                # Cleanup loop
                                if loop.is_running():
                                    loop.stop()
                                loop.close()
                            except:
                                pass
                    except Exception as e:
                        logger.error(f"❌ Thread error: {str(e)}")

                # Stop any existing bot instance
                if session.get('bot_running'):
                    main.SESSION_STRING = None
                    main.SOURCE_CHANNEL = None
                    main.DESTINATION_CHANNEL = None
                    EventLoopManager.reset()

                # Start new bot thread
                bot_thread = threading.Thread(target=start_bot, daemon=True)
                bot_thread.start()
                session['bot_running'] = True
                logger.info("✅ Bot started successfully")

                return jsonify({
                    'status': True,
                    'message': 'Bot is now running'
                })

            except Exception as e:
                logger.error(f"❌ Bot start error: {str(e)}")
                return jsonify({'error': str(e)}), 500
        else:
            try:
                # Stop bot by clearing its session
                main.SESSION_STRING = None
                main.SOURCE_CHANNEL = None
                main.DESTINATION_CHANNEL = None
                session['bot_running'] = False
                EventLoopManager.reset()
                logger.info("✅ Bot stopped successfully")

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

@app.route('/static/<path:filename>')
def serve_static(filename):
    return send_from_directory('static', filename)


@app.route('/get-replacements')
@login_required
def get_replacements():
    try:
        with get_db() as conn:
            with conn.cursor(cursor_factory=DictCursor) as cur:
                cur.execute("""
                    SELECT original_text, replacement_text 
                    FROM text_replacements
                    ORDER BY id DESC
                """)
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

        if not original or not replacement:
            return jsonify({'error': 'Both original and replacement text required'}), 400

        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO text_replacements (original_text, replacement_text)
                    VALUES (%s, %s)
                """, (original, replacement))

        return jsonify({'message': 'Replacement added successfully'})
    except Exception as e:
        logger.error(f"❌ Add replacement error: {str(e)}")
        return jsonify({'error': str(e)}), 500

@app.route('/remove-replacement', methods=['POST'])
@login_required
def remove_replacement():
    try:
        original = request.form.get('original')
        if not original:
            return jsonify({'error': 'Original text required'}), 400

        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    DELETE FROM text_replacements 
                    WHERE original_text = %s
                """, (original,))

        return jsonify({'message': 'Replacement removed successfully'})
    except Exception as e:
        logger.error(f"❌ Remove replacement error: {str(e)}")
        return jsonify({'error': str(e)}), 500

@app.route('/clear-replacements', methods=['POST'])
@login_required
def clear_replacements():
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM text_replacements")
        return jsonify({'message': 'All replacements cleared'})
    except Exception as e:
        logger.error(f"❌ Clear replacements error: {str(e)}")
        return jsonify({'error': str(e)}), 500

if __name__ == '__main__':
    try:
        # Initialize database tables
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS channel_config (
                        id SERIAL PRIMARY KEY,
                        source_channel TEXT NOT NULL,
                        destination_channel TEXT NOT NULL,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    );

                    CREATE TABLE IF NOT EXISTS text_replacements (
                        id SERIAL PRIMARY KEY,
                        original_text TEXT NOT NULL,
                        replacement_text TEXT NOT NULL,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    );
                """)

        # Start production server
        from waitress import serve
        logger.info("🚀 Starting production server on port 5000...")
        serve(app, host='0.0.0.0', port=5000, threads=4)
    except Exception as e:
        logger.error(f"❌ Server startup error: {str(e)}")
        raise
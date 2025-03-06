import os
import logging
import threading
from flask import Flask, render_template, request, session, redirect, url_for, jsonify, send_from_directory
from telethon import TelegramClient, events
from telethon.errors import SessionPasswordNeededError, PhoneNumberInvalidError
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
        return cls._loop

    @classmethod
    def get_client(cls):
        return cls._client

    @classmethod
    def set_client(cls, client):
        cls._client = client

    @classmethod
    def reset(cls):
        with cls._lock:
            if cls._loop:
                try:
                    cls._loop.close()
                except:
                    pass
                cls._loop = None
            cls._client = None

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

    async def get_auth_client(self):
        """Get a client for authentication or dashboard operations"""
        with self._lock:
            try:
                session_string = session.get('session_string')
                client = TelegramClient(
                    StringSession(session_string) if session_string else StringSession(),
                    self.api_id,
                    self.api_hash,
                    device_model="Replit Web",
                    system_version="Linux",
                    app_version="1.0",
                    loop=EventLoopManager.get_loop()
                )

                if not client.is_connected():
                    await client.connect()

                EventLoopManager.set_client(client)
                return client
            except Exception as e:
                logger.error(f"❌ Client creation error: {str(e)}")
                raise

    def cleanup(self):
        """Clean up resources"""
        EventLoopManager.reset()

# Create global Telegram manager
telegram_manager = TelegramManager(
    int(os.getenv('API_ID', '27202142')),
    os.getenv('API_HASH', 'db4dd0d95dc68d46b77518bf997ed165')
)

@app.route('/')
def login():
    if session.get('logged_in'):
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
            session.pop('phone_code_hash', None)
            session.pop('session_string', None)

            # Check existing session
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
            return jsonify({'error': 'Session expired. Please try again.'}), 400

        if not otp:
            return jsonify({'error': 'OTP is required'}), 400

        try:
            client = await telegram_manager.get_auth_client()
            try:
                # Clear any existing session
                if await client.is_user_authorized():
                    await client.log_out()

                # Try signing in
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
                    session.pop('phone_code_hash', None)  # Clear failed OTP
                    return jsonify({'error': 'Invalid 2FA password'}), 400
            except Exception as e:
                if "phone code expired" in str(e).lower() or "code expired" in str(e).lower():
                    # Clear expired OTP from session
                    session.pop('phone_code_hash', None)
                    return jsonify({'error': 'OTP has expired. Please request a new one.'}), 400
                elif "invalid" in str(e).lower():
                    return jsonify({'error': 'Invalid OTP. Please try again.'}), 400
                else:
                    logger.error(f"❌ Sign in error: {str(e)}")
                    return jsonify({'error': str(e)}), 400

            if await client.is_user_authorized():
                # Save session string and mark as logged in
                session['logged_in'] = True
                session['session_string'] = client.session.save()
                return jsonify({'message': 'Login successful'})
            else:
                session.pop('phone_code_hash', None)  # Clear invalid OTP
                return jsonify({'error': 'Authentication failed. Please try again.'}), 400

        except Exception as e:
            logger.error(f"❌ Verify OTP error: {str(e)}")
            # Clear failed session
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
            return redirect(url_for('login'))

        # Get client using event loop manager
        client = await telegram_manager.get_auth_client()
        if not await client.is_user_authorized():
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
        # Clean up sessions
        telegram_manager.cleanup()

        # Clear flask session
        session.clear()
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
            return jsonify({'error': 'Configure channels first'}), 400

        session_string = session.get('session_string')
        if not session_string:
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
                        loop = asyncio.new_event_loop()
                        asyncio.set_event_loop(loop)
                        try:
                            loop.run_until_complete(main.main())
                        except Exception as e:
                            logger.error(f"❌ Bot startup error: {str(e)}")
                        finally:
                            loop.close()
                    except Exception as e:
                        logger.error(f"❌ Thread error: {str(e)}")

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
                # Stop bot
                main.SESSION_STRING = None
                main.SOURCE_CHANNEL = None
                main.DESTINATION_CHANNEL = None
                session['bot_running'] = False
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

if __name__ == '__main__':
    # Ensure directories exist
    os.makedirs('static/css', exist_ok=True)

    # Create default CSS if not exists
    if not os.path.exists('static/css/style.css'):
        with open('static/css/style.css', 'w') as f:
            f.write("""
                body {
                    font-family: Arial, sans-serif;
                    margin: 0;
                    padding: 20px;
                    background-color: #f0f2f5;
                }
                .login-container {
                    max-width: 400px;
                    margin: 50px auto;
                    padding: 20px;
                    background: white;
                    border-radius: 8px;
                    box-shadow: 0 2px 4px rgba(0, 0, 0, 0.1);
                }
                .input-group {
                    margin-bottom: 15px;
                }
                input {
                    width: 100%;
                    padding: 8px;
                    margin-bottom: 10px;
                    border: 1px solid #ddd;
                    border-radius: 4px;
                }
                button {
                    width: 100%;
                    padding: 10px;
                    background-color: #0066cc;
                    color: white;
                    border: none;
                    border-radius: 4px;
                    cursor: pointer;
                }
                button:hover {
                    background-color: #0052a3;
                }
            """)

    app.run(host='0.0.0.0', port=5000)
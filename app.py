import os
import logging
import threading
from flask import Flask, render_template, request, session, redirect, url_for, jsonify, g, send_from_directory
from telethon import TelegramClient, events
from telethon.errors import SessionPasswordNeededError, PhoneNumberInvalidError
import asyncio
from functools import wraps
from asgiref.sync import async_to_sync
import psycopg2
from psycopg2.extras import DictCursor
from psycopg2 import pool
from flask_session import Session
from datetime import timedelta
from flask_sqlalchemy import SQLAlchemy
from threading import Thread, Lock
from contextlib import contextmanager

# Set up logging
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

app = Flask(__name__)
app.secret_key = os.urandom(24)

# Configure Flask application
app.config.update(
    SESSION_TYPE='sqlalchemy',
    PERMANENT_SESSION_LIFETIME=timedelta(days=7),
    SESSION_PERMANENT=True,
    SQLALCHEMY_DATABASE_URI=os.getenv('DATABASE_URL'),
    SQLALCHEMY_TRACK_MODIFICATIONS=False,
    SQLALCHEMY_ENGINE_OPTIONS={
        'pool_size': 10,
        'pool_timeout': 30,
        'pool_recycle': 1800,
    }
)

# Initialize database
db = SQLAlchemy(app)

# Database pool for other connections
db_pool = psycopg2.pool.ThreadedConnectionPool(
    minconn=1,
    maxconn=20,
    dsn=os.getenv('DATABASE_URL')
)

# Database lock for concurrent operations
db_lock = Lock()

# Configure session
class FlaskSession(db.Model):
    __tablename__ = 'session'
    id = db.Column(db.Integer, primary_key=True)
    session_id = db.Column(db.String(255), unique=True, nullable=False)
    data = db.Column(db.LargeBinary)
    expiry = db.Column(db.DateTime)

app.config['SESSION_SQLALCHEMY'] = db
Session(app)

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

# Database connection
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
        self._lock = Lock()

    async def get_auth_client(self):
        """Client for authentication only"""
        with self._lock:
            client = TelegramClient(
                'auth_session',
                self.api_id,
                self.api_hash,
                device_model="Replit Web Auth",
                system_version="Linux",
                app_version="1.0",
                loop=EventLoopManager.get_loop()
            )

            if not client.is_connected():
                await client.connect()

            return client

    def cleanup(self):
        """Clean up authentication resources"""
        EventLoopManager.reset()
        for session_file in ['auth_session.session', 'auth_session.session-journal']:
            if os.path.exists(session_file):
                try:
                    os.remove(session_file)
                except:
                    pass

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

            # Check existing session
            if await client.is_user_authorized():
                session['user_phone'] = phone
                session['logged_in'] = True
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
                    return jsonify({'error': 'Invalid 2FA password'}), 400

            if await client.is_user_authorized():
                session['logged_in'] = True
                return jsonify({'message': 'Login successful'})
            else:
                return jsonify({'error': 'Invalid OTP'}), 400

        except Exception as e:
            return jsonify({'error': str(e)}), 500

    except Exception as e:
        logger.error(f"❌ Critical error in verify_otp: {str(e)}")
        return jsonify({'error': str(e)}), 500

@app.route('/dashboard')
@login_required
@async_route
async def dashboard():
    try:
        client = await telegram_manager.get_auth_client()
        channels = []

        # Get channels
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

        import main
        if status:
            # Start bot
            def start_bot():
                try:
                    main.SOURCE_CHANNEL = source
                    main.DESTINATION_CHANNEL = destination
                    loop = asyncio.new_event_loop()
                    asyncio.set_event_loop(loop)
                    try:
                        loop.run_until_complete(main.main())
                    finally:
                        loop.close()
                except Exception as e:
                    logger.error(f"❌ Bot error: {str(e)}")

            bot_thread = Thread(target=start_bot, daemon=True)
            bot_thread.start()
            session['bot_running'] = True
        else:
            # Stop bot
            main.SOURCE_CHANNEL = None
            main.DESTINATION_CHANNEL = None
            session['bot_running'] = False

        return jsonify({
            'status': status,
            'message': f"Bot is now {'running' if status else 'stopped'}"
        })

    except Exception as e:
        logger.error(f"❌ Bot toggle error: {str(e)}")
        return jsonify({'error': str(e)}), 500

@app.route('/static/<path:filename>')
def serve_static(filename):
    return send_from_directory('static', filename)

if __name__ == '__main__':
    # Ensure directories exist
    os.makedirs('static/css', exist_ok=True)

    # Create default CSS
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

    app.run(host='0.0.0.0', port=5000, debug=True)
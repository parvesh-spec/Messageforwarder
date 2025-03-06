import os
import logging
from flask import Flask, render_template, request, session, redirect, url_for, jsonify, g, send_from_directory
from telethon import TelegramClient, events
from telethon.errors import SessionPasswordNeededError, PhoneNumberInvalidError
import asyncio
from functools import wraps
from asgiref.sync import async_to_sync
from sqlalchemy import create_engine, Integer, String, LargeBinary, DateTime, Column
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import scoped_session, sessionmaker
from sqlalchemy.pool import QueuePool
from flask_session import Session
from datetime import timedelta
from flask_sqlalchemy import SQLAlchemy
from threading import Thread, Lock

# Configure logging
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

app = Flask(__name__)
app.secret_key = os.urandom(24)

# Configure database engine with proper pooling
engine = create_engine(
    os.getenv('DATABASE_URL'),
    poolclass=QueuePool,
    pool_size=10,
    max_overflow=20,
    pool_timeout=30,
    pool_recycle=1800
)

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

# Initialize SQLAlchemy
db = SQLAlchemy(app)
DBSession = scoped_session(sessionmaker(bind=engine))

# Define models
Base = declarative_base()

class FlaskSession(Base):
    __tablename__ = 'session'
    id = Column(Integer, primary_key=True)
    session_id = Column(String(255), unique=True, nullable=False)
    data = Column(LargeBinary)
    expiry = Column(DateTime)

# Create tables
Base.metadata.create_all(engine)

# Configure Flask-Session
app.config['SESSION_SQLALCHEMY'] = db
Session(app)

# Database lock for concurrent operations
db_lock = Lock()

class TelegramManager:
    def __init__(self, session_name, api_id, api_hash):
        self.session_name = session_name
        self.api_id = api_id
        self.api_hash = api_hash
        self.client = None
        self.loop = None
        self._lock = Lock()

    async def get_client(self):
        with self._lock:
            if not self.client:
                if not self.loop:
                    self.loop = asyncio.get_event_loop()
                self.client = TelegramClient(
                    self.session_name,
                    self.api_id,
                    self.api_hash,
                    device_model="Replit Web",
                    system_version="Linux",
                    app_version="1.0",
                    loop=self.loop
                )

            if not self.client.is_connected():
                await self.client.connect()

            return self.client

    async def disconnect(self):
        with self._lock:
            if self.client and self.client.is_connected():
                await self.client.disconnect()
                self.client = None

    def cleanup(self):
        with self._lock:
            self.client = None
            self.loop = None

# Create global Telegram manager
telegram_manager = TelegramManager(
    'anon',
    int(os.getenv('API_ID', '27202142')),
    os.getenv('API_HASH', 'db4dd0d95dc68d46b77518bf997ed165')
)

# Database connection management
def get_db():
    """Get SQLAlchemy session with proper error handling"""
    if not hasattr(g, 'db_session'):
        g.db_session = DBSession()
    return g.db_session

@app.teardown_appcontext
def cleanup_session(exception=None):
    """Cleanup database session and client on request end"""
    db_session = g.pop('db_session', None)
    if db_session is not None:
        db_session.close()
    DBSession.remove()

def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_phone' not in session:
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function

def async_route(f):
    @wraps(f)
    def wrapped(*args, **kwargs):
        return async_to_sync(f)(*args, **kwargs)
    return wrapped

@app.route('/')
def login():
    """Render login page or redirect to dashboard if already logged in"""
    logger.info("Accessing login page")
    if 'logged_in' in session and session['logged_in']:
        logger.info("User already logged in, redirecting to dashboard")
        return redirect(url_for('dashboard'))
    return render_template('login.html')

@app.route('/send-otp', methods=['POST'])
@async_route
async def send_otp():
    """Handle OTP request and send code"""
    try:
        phone = request.form.get('phone')
        if not phone:
            logger.error("❌ Phone number missing")
            return jsonify({'error': 'Phone number is required'}), 400

        if not phone.startswith('+91'):
            logger.error(f"❌ Invalid phone format: {phone}")
            return jsonify({'error': 'Phone number must start with +91'}), 400

        client = None
        try:
            client = await telegram_manager.get_client()

            # Check existing session
            if await client.is_user_authorized():
                logger.info(f"✅ Found valid session for {phone}")
                session['user_phone'] = phone
                session['logged_in'] = True
                return jsonify({'message': 'Already authorized', 'already_authorized': True})

            # Send OTP
            sent = await client.send_code_request(phone)
            session['user_phone'] = phone
            session['phone_code_hash'] = sent.phone_code_hash
            logger.info("✅ OTP sent successfully")
            return jsonify({'message': 'OTP sent successfully'})

        except PhoneNumberInvalidError:
            logger.error(f"❌ Invalid phone: {phone}")
            return jsonify({'error': 'Invalid phone number'}), 400
        except Exception as e:
            logger.error(f"❌ Send OTP error: {str(e)}")
            return jsonify({'error': str(e)}), 500
        finally:
            if client:
                await telegram_manager.disconnect()

    except Exception as e:
        logger.error(f"❌ Critical error in send_otp: {str(e)}")
        return jsonify({'error': 'Server error'}), 500

@app.route('/verify-otp', methods=['POST'])
@async_route
async def verify_otp():
    """Verify OTP and handle 2FA if needed"""
    try:
        phone = session.get('user_phone')
        phone_code_hash = session.get('phone_code_hash')
        otp = request.form.get('otp')
        password = request.form.get('password')

        if not phone or not phone_code_hash:
            logger.error("❌ Missing session data")
            return jsonify({'error': 'Session expired. Please try again.'}), 400

        if not otp:
            logger.error("❌ OTP missing")
            return jsonify({'error': 'OTP is required'}), 400

        client = None
        try:
            client = await telegram_manager.get_client()

            try:
                await client.sign_in(phone, otp, phone_code_hash=phone_code_hash)
                logger.info("✅ Sign in successful")
            except SessionPasswordNeededError:
                logger.info("⚠️ 2FA needed")
                if not password:
                    return jsonify({
                        'error': 'two_factor_needed',
                        'message': 'Two-factor authentication required'
                    })
                try:
                    await client.sign_in(password=password)
                    logger.info("✅ 2FA verification successful")
                except Exception as e:
                    logger.error(f"❌ 2FA failed: {e}")
                    return jsonify({'error': 'Invalid 2FA password'}), 400

            if await client.is_user_authorized():
                session['logged_in'] = True
                return jsonify({'message': 'Login successful'})
            else:
                return jsonify({'error': 'Invalid OTP'}), 400

        finally:
            if client:
                await telegram_manager.disconnect()

    except Exception as e:
        logger.error(f"❌ Verify OTP error: {str(e)}")
        return jsonify({'error': str(e)}), 500

@app.route('/logout')
def logout():
    """Handle user logout and cleanup resources"""
    try:
        phone = session.get('user_phone')
        if phone:
            session_file = "anon.session"
            if os.path.exists(session_file):
                try:
                    os.remove(session_file)
                    logger.info("✅ Removed session file")
                except Exception as e:
                    logger.error(f"❌ Cleanup error: {str(e)}")

        # Clean up resources
        try:
            telegram_manager.cleanup()
            DBSession.remove()
            session.clear()
        except Exception as e:
            logger.error(f"❌ Cleanup error: {str(e)}")

        return redirect(url_for('login'))
    except Exception as e:
        logger.error(f"❌ Error in logout: {str(e)}")
        return redirect(url_for('login'))

@app.route('/dashboard')
@login_required
@async_route
async def dashboard():
    """Render dashboard with channel list and configuration"""
    try:
        client = await telegram_manager.get_client()
        channels = []

        try:
            async for dialog in client.iter_dialogs():
                if dialog.is_channel:
                    channels.append({
                        'id': dialog.id,
                        'name': dialog.name
                    })

            # Get last selected channels from database
            with get_db() as session:
                result = session.execute("""
                    SELECT source_channel, destination_channel 
                    FROM channel_config 
                    ORDER BY updated_at DESC 
                    LIMIT 1
                """)
                last_config = result.fetchone()

            return render_template('dashboard.html', 
                                channels=channels,
                                last_source=last_config['source_channel'] if last_config else None,
                                last_dest=last_config['destination_channel'] if last_config else None)

        except Exception as e:
            logger.error(f"Error loading dashboard data: {str(e)}")
            raise

        finally:
            await telegram_manager.disconnect()

    except Exception as e:
        logger.error(f"Critical error in dashboard: {str(e)}")
        return redirect(url_for('login'))

@app.route('/static/<path:filename>')
def serve_static(filename):
    """Serve static files"""
    return send_from_directory('static', filename)

# Ensure required directories exist
os.makedirs('static/css', exist_ok=True)

# Create default CSS if it doesn't exist
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
                margin-bottom: 10px;
            }
            button:hover {
                background-color: #0052a3;
            }
            #message {
                padding: 10px;
                margin-top: 10px;
                border-radius: 4px;
            }
            .error {
                color: red;
            }
            .success {
                color: green;
            }
        """)

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)
import os
import logging
from flask import Flask, render_template, request, session, redirect, url_for, jsonify, g, send_from_directory
from telethon import TelegramClient, events
from telethon.errors import SessionPasswordNeededError, PhoneNumberInvalidError
import asyncio
from functools import wraps
from asgiref.sync import async_to_sync
from sqlalchemy import create_engine, Integer, String, LargeBinary, DateTime, Column, text
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

# Initialize Flask
app = Flask(__name__)
app.secret_key = os.urandom(24)

# Configure database engine with proper pooling
engine = create_engine(
    os.getenv('DATABASE_URL'),
    poolclass=QueuePool,
    pool_size=3,
    max_overflow=5,
    pool_timeout=30,
    pool_recycle=1800,
    pool_pre_ping=True,
    echo=True
)

# Configure Flask application
app.config.update(
    SESSION_TYPE='sqlalchemy',
    PERMANENT_SESSION_LIFETIME=timedelta(days=7),
    SESSION_PERMANENT=True,
    SQLALCHEMY_DATABASE_URI=os.getenv('DATABASE_URL'),
    SQLALCHEMY_TRACK_MODIFICATIONS=False,
    SQLALCHEMY_ENGINE_OPTIONS={
        'pool_size': 3,
        'max_overflow': 5,
        'pool_timeout': 30,
        'pool_recycle': 1800,
        'pool_pre_ping': True
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

class TelegramManager:
    _instance = None
    _lock = Lock()
    _initialized = False

    def __new__(cls, *args, **kwargs):
        with cls._lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
            return cls._instance

    def __init__(self, session_name=None, api_id=None, api_hash=None):
        with self._lock:
            if not self._initialized:
                self.session_name = session_name or 'anon'
                self.api_id = api_id or int(os.getenv('API_ID', '27202142'))
                self.api_hash = api_hash or os.getenv('API_HASH', 'db4dd0d95dc68d46b77518bf997ed165')
                self.client = None
                self._client_lock = Lock()
                self._loop = None
                self._initialized = True

    async def _create_client(self):
        """Create a new Telegram client"""
        if not self._loop:
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)

        client = TelegramClient(
            self.session_name,
            self.api_id,
            self.api_hash,
            device_model="Replit Web",
            system_version="Linux",
            app_version="1.0",
            loop=self._loop
        )
        return client

    async def get_client(self):
        """Get or create a Telegram client with proper event loop management"""
        try:
            with self._client_lock:
                if not self.client:
                    self.client = await self._create_client()

                if not self.client.is_connected():
                    await self.client.connect()

                return self.client
        except Exception as e:
            logger.error(f"❌ Client initialization error: {str(e)}")
            if self.client:
                await self.disconnect()
            raise

    async def disconnect(self):
        """Safely disconnect the client and cleanup resources"""
        try:
            with self._client_lock:
                if self.client:
                    if self.client.is_connected():
                        await self.client.disconnect()
                    self.client = None
        except Exception as e:
            logger.error(f"❌ Disconnect error: {str(e)}")
            self.client = None

    def cleanup(self):
        """Complete cleanup of all resources"""
        with self._client_lock:
            if self._loop:
                try:
                    loop = asyncio.new_event_loop()
                    asyncio.set_event_loop(loop)
                    loop.run_until_complete(self.disconnect())
                    loop.close()
                except Exception as e:
                    logger.error(f"❌ Cleanup error: {str(e)}")
                finally:
                    self.client = None
                    if self._loop:
                        try:
                            self._loop.close()
                        except:
                            pass
                        self._loop = None

# Create singleton Telegram manager
telegram_manager = TelegramManager()

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

        try:
            client = await telegram_manager.get_client()

            try:
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
                await telegram_manager.disconnect()

        except Exception as e:
            logger.error(f"❌ Client error: {str(e)}")
            return jsonify({'error': 'Failed to initialize Telegram client'}), 500

    except Exception as e:
        logger.error(f"❌ Critical error in send_otp: {str(e)}")
        return jsonify({'error': str(e)}), 500

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
            await telegram_manager.disconnect()

    except Exception as e:
        logger.error(f"❌ Verify OTP error: {str(e)}")
        return jsonify({'error': str(e)}), 500

@app.route('/logout')
def logout():
    """Handle user logout and cleanup resources"""
    try:
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
        # Initialize client
        client = await telegram_manager.get_client()
        channels = []

        try:
            # Get channels list
            async for dialog in client.iter_dialogs():
                if dialog.is_channel:
                    channels.append({
                        'id': dialog.id,
                        'name': dialog.name
                    })

            # Get last selected channels from database
            with get_db() as session:
                sql = text("""
                    SELECT source_channel as src, destination_channel as dst
                    FROM channel_config 
                    ORDER BY updated_at DESC 
                    LIMIT 1
                """)
                result = session.execute(sql).first()

                # Handle result properly
                last_source = None
                last_dest = None
                if result:
                    last_source = result.src
                    last_dest = result.dst
                    logger.info(f"✅ Loaded last config - Source: {last_source}, Dest: {last_dest}")
                else:
                    logger.info("ℹ️ No previous channel configuration found")

            return render_template('dashboard.html', 
                                channels=channels,
                                last_source=last_source,
                                last_dest=last_dest)

        except Exception as e:
            logger.error(f"❌ Error loading dashboard data: {str(e)}")
            raise

        finally:
            await telegram_manager.disconnect()

    except Exception as e:
        logger.error(f"❌ Critical error in dashboard: {str(e)}")
        return redirect(url_for('login'))

@app.route('/update-channels', methods=['POST'])
def update_channels():
    """Update channel configuration in database"""
    try:
        source = request.form.get('source')
        destination = request.form.get('destination')

        if not source or not destination:
            logger.error("❌ Missing channel IDs")
            return jsonify({'error': 'Both source and destination channels are required'}), 400

        if source == destination:
            logger.error("❌ Source and destination channels are same")
            return jsonify({'error': 'Source and destination channels must be different'}), 400

        try:
            # Format channel IDs
            if not source.startswith('-100'):
                source = f"-100{source.lstrip('-')}"
            if not destination.startswith('-100'):
                destination = f"-100{destination.lstrip('-')}"

            # Update session
            session['source_channel'] = source
            session['dest_channel'] = destination

            # Save to database
            with get_db() as db_session:
                # Create table if not exists
                sql = text("""
                    CREATE TABLE IF NOT EXISTS channel_config (
                        id SERIAL PRIMARY KEY,
                        source_channel TEXT NOT NULL,
                        destination_channel TEXT NOT NULL,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                """)
                db_session.execute(sql)

                # Insert new configuration
                sql = text("""
                    INSERT INTO channel_config (source_channel, destination_channel)
                    VALUES (:source, :destination)
                """)
                db_session.execute(sql, {"source": source, "destination": destination})
                db_session.commit()

            logger.info(f"✅ Channel config updated - Source: {source}, Destination: {destination}")
            return jsonify({'message': 'Channel configuration updated successfully'})

        except Exception as e:
            logger.error(f"❌ Channel update error: {e}")
            return jsonify({'error': str(e)}), 500

    except Exception as e:
        logger.error(f"❌ Route error: {str(e)}")
        return jsonify({'error': str(e)}), 500

@app.route('/get-replacements')
@login_required
def get_replacements():
    """Get text replacements for current user"""
    try:
        user_phone = session.get('user_phone')
        if not user_phone:
            return jsonify({'error': 'User not found'}), 404

        with get_db() as db_session:
            sql = text("""
                SELECT original_text as orig, replacement_text as repl
                FROM text_replacements 
                WHERE user_id = (SELECT id FROM users WHERE phone = :phone)
                ORDER BY LENGTH(original_text) DESC
            """)
            result = db_session.execute(sql, {"phone": user_phone})
            replacements = {row.orig: row.repl for row in result}
            logger.info(f"✅ Retrieved {len(replacements)} replacements")

            return jsonify(replacements)

    except Exception as e:
        logger.error(f"❌ Error getting replacements: {str(e)}")
        return jsonify({'error': str(e)}), 500

@app.route('/bot/toggle', methods=['POST'])
@login_required
def toggle_bot():
    """Toggle bot status"""
    try:
        status = request.form.get('status') == 'true'
        source = session.get('source_channel')
        destination = session.get('dest_channel')

        if not source or not destination:
            logger.error("❌ Channel configuration missing")
            return jsonify({'error': 'Please configure source and destination channels first'}), 400

        try:
            import main
            main.SOURCE_CHANNEL = source
            main.DESTINATION_CHANNEL = destination

            if status:
                logger.info("🔄 Starting bot...")
                if not hasattr(main, 'client') or not main.client:
                    loop = asyncio.new_event_loop()
                    asyncio.set_event_loop(loop)
                    loop.run_until_complete(main.setup_client())
                    loop.run_until_complete(main.setup_handlers())
                    logger.info("✅ Bot initialized successfully")

            else:
                logger.info("🔄 Stopping bot...")
                if hasattr(main, 'client') and main.client:
                    loop = asyncio.new_event_loop()
                    asyncio.set_event_loop(loop)
                    loop.run_until_complete(main.client.disconnect())
                    main.client = None
                    logger.info("✅ Bot stopped successfully")

            session['bot_running'] = status
            return jsonify({
                'status': status,
                'message': f"Bot is now {'running' if status else 'stopped'}"
            })

        except Exception as e:
            logger.error(f"❌ Bot toggle error: {str(e)}")
            return jsonify({'error': str(e)}), 500

    except Exception as e:
        logger.error(f"❌ Route error: {str(e)}")
        return jsonify({'error': str(e)}), 500

@app.route('/static/<path:filename>')
def serve_static(filename):
    """Serve static files"""
    return send_from_directory('static', filename)

# Create required directories and CSS
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
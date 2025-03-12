import os
import logging
import threading
import time
from datetime import datetime, timedelta
from flask import Flask, render_template, request, session, redirect, url_for, jsonify, flash
from telethon import TelegramClient, events
from telethon.errors import SessionPasswordNeededError, PhoneNumberInvalidError
from telethon.sessions import StringSession
import asyncio
from functools import wraps
import psycopg2
from psycopg2.extras import DictCursor
from psycopg2 import pool
from flask_session import Session
from contextlib import contextmanager
from werkzeug.security import generate_password_hash, check_password_hash
from forms import LoginForm, RegisterForm
from flask_wtf.csrf import CSRFProtect
from urllib.parse import urlparse, parse_qs

# Set up logging
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Configure Flask application
app = Flask(__name__)

# Session configuration
app.config.update(
    SECRET_KEY=os.environ.get('FLASK_SECRET_KEY', os.urandom(24)),
    SESSION_TYPE='filesystem',
    PERMANENT_SESSION_LIFETIME=timedelta(days=7),
    SESSION_PERMANENT=True,
    SESSION_FILE_DIR='flask_session',  # Directory for session files
    SESSION_FILE_THRESHOLD=500,  # Maximum number of session files
    SESSION_USE_SIGNER=True,  # Sign the session cookie
    SESSION_KEY_PREFIX='session:',  # Session key prefix
    SESSION_COOKIE_NAME='session_id',  # Session cookie name
    SESSION_COOKIE_SECURE=False,  # Set to True in production
    SESSION_COOKIE_HTTPONLY=True,  # Prevent JavaScript access
    SESSION_COOKIE_SAMESITE='Lax',  # CSRF protection
    WTF_CSRF_TIME_LIMIT=None,  # No time limit for CSRF tokens
    WTF_CSRF_SSL_STRICT=False,  # Don't require HTTPS for CSRF
    DEBUG=True
)

# Initialize session after config
Session(app)

# Initialize CSRF protection after session
csrf = CSRFProtect(app)
csrf.init_app(app)

try:
    # Parse database URL to properly handle query parameters
    db_url = os.getenv('DATABASE_URL')
    parsed_url = urlparse(db_url)

    # Get existing query parameters
    query_params = parse_qs(parsed_url.query)

    # Add sslmode=disable if not present
    if 'sslmode' not in query_params:
        new_query = f"{parsed_url.query}&sslmode=disable" if parsed_url.query else "sslmode=disable"
        db_url = f"{db_url}{'&' if parsed_url.query else '?'}{new_query}"

    logger.info("Initializing database connection pool...")

    # Database pool for connections
    db_pool = psycopg2.pool.ThreadedConnectionPool(
        minconn=1,
        maxconn=10,
        dsn=db_url
    )
    logger.info("✅ Database connection pool initialized successfully")

except Exception as e:
    logger.error(f"❌ Failed to initialize database connection pool: {str(e)}")
    raise

# Database connection context manager
@contextmanager
def get_db():
    conn = None
    try:
        conn = db_pool.getconn()
        conn.autocommit = True
        yield conn
    except psycopg2.OperationalError as e:
        logger.error(f"❌ Database connection error: {str(e)}")
        if conn:
            db_pool.putconn(conn)
        raise
    finally:
        if conn:
            db_pool.putconn(conn)

class TelegramManager:
    def __init__(self, api_id, api_hash):
        self.api_id = api_id
        self.api_hash = api_hash
        self._lock = threading.Lock()
        self._client = None
        self._loop = asyncio.new_event_loop()
        threading.Thread(target=self._run_loop, daemon=True).start()
        self._current_phone = None
        self._current_hash = None

    def _run_loop(self):
        """Run event loop in background thread"""
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    async def _initialize_client(self, session_string=None):
        """Initialize the Telegram client"""
        try:
            if not self._client:
                self._client = TelegramClient(
                    StringSession(session_string) if session_string else StringSession(),
                    self.api_id,
                    self.api_hash,
                    device_model="Replit Web",
                    system_version="Linux",
                    app_version="1.0",
                    loop=self._loop
                )
                await self._client.connect()
                logger.info("✅ Telegram client initialized")
        except Exception as e:
            logger.error(f"❌ Client initialization error: {str(e)}")
            self._client = None
            raise

    def save_verification_data(self, phone, hash_value):
        """Save phone and hash for verification"""
        self._current_phone = phone
        self._current_hash = hash_value
        logger.info("✅ Saved verification data")

    def get_verification_data(self):
        """Get saved verification data"""
        return self._current_phone, self._current_hash

    async def get_client(self, session_string=None):
        """Get the Telegram client instance"""
        with self._lock:
            try:
                if self._client and self._client.is_connected():
                    return self._client

                await self._cleanup_client()
                await self._initialize_client(session_string)
                return self._client

            except Exception as e:
                logger.error(f"❌ Client connection error: {str(e)}")
                await self._cleanup_client()
                raise

    async def _cleanup_client(self):
        """Cleanup the client connection"""
        if self._client:
            try:
                if self._client.is_connected():
                    await self._client.disconnect()
            except:
                pass
            finally:
                self._client = None

# Initialize the Telegram manager
telegram_manager = TelegramManager(
    int(os.getenv('API_ID')),
    os.getenv('API_HASH')
)

# Async route decorator that uses the telegram manager's event loop
def async_route(f):
    @wraps(f)
    def wrapped(*args, **kwargs):
        try:
            future = asyncio.run_coroutine_threadsafe(
                f(*args, **kwargs),
                telegram_manager._loop
            )
            return future.result(timeout=30)  # Add timeout to prevent hanging
        except Exception as e:
            logger.error(f"❌ Async route error: {str(e)}")
            return jsonify({'error': str(e)}), 500
    return wrapped

# Authentication decorator
def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not session.get('user_id'):
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function


@app.route('/')
def login():
    if session.get('user_id'):
        return redirect(url_for('dashboard'))
    form = LoginForm()
    return render_template('auth/login.html', form=form)

@app.route('/login', methods=['POST'])
def login_post():
    form = LoginForm()
    if not form.validate_on_submit():
        return render_template('auth/login.html', form=form)

    email = form.email.data
    password = form.password.data

    with get_db() as conn:
        with conn.cursor(cursor_factory=DictCursor) as cur:
            cur.execute("SELECT * FROM users WHERE email = %s", (email,))
            user = cur.fetchone()

            if not user or not check_password_hash(user['password_hash'], password):
                form.email.errors.append('Please check your email and password')
                return render_template('auth/login.html', form=form)

            # Update login status
            cur.execute("""
                UPDATE users 
                SET is_logged_in = true,
                    last_login_at = CURRENT_TIMESTAMP
                WHERE id = %s
            """, (user['id'],))

            session['user_id'] = user['id']
            session['telegram_id'] = user['telegram_id'] #This line might need adjustment depending on how telegram id is handled in the new schema.
            return redirect(url_for('dashboard'))

@app.route('/register')
def register():
    form = RegisterForm()
    return render_template('auth/register.html', form=form)

@app.route('/register', methods=['POST'])
def register_post():
    form = RegisterForm()
    if not form.validate_on_submit():
        return render_template('auth/register.html', form=form)

    email = form.email.data
    password = form.password.data

    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM users WHERE email = %s", (email,))
            if cur.fetchone():
                form.email.errors.append('Email already registered')
                return render_template('auth/register.html', form=form)

            cur.execute("""
                INSERT INTO users (email, password_hash)
                VALUES (%s, %s)
                RETURNING id
            """, (email, generate_password_hash(password)))

            user_id = cur.fetchone()[0]
            session['user_id'] = user_id
            return redirect(url_for('dashboard'))

@app.route('/logout')
def logout():
    user_id = session.get('user_id')
    if user_id:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    UPDATE users 
                    SET is_logged_in = false 
                    WHERE id = %s
                """, (user_id,))

    session.clear()
    return redirect(url_for('login'))

@app.route('/dashboard')
@login_required
def dashboard():
    user_id = session.get('user_id')
    with get_db() as conn:
        with conn.cursor(cursor_factory=DictCursor) as cur:
            # Get primary Telegram account
            cur.execute("""
                SELECT telegram_id, telegram_username, auth_date, session_string
                FROM telegram_accounts
                WHERE user_id = %s AND is_primary = true
            """, (user_id,))
            primary_account = cur.fetchone()

            # Get forwarding config
            cur.execute("""
                SELECT *
                FROM forwarding_configs
                WHERE user_id = %s
            """, (user_id,))
            config = cur.fetchone()

            # Get replacement count
            cur.execute("""
                SELECT COUNT(*)
                FROM text_replacements
                WHERE user_id = %s
            """, (user_id,))
            replacements_count = cur.fetchone()[0]

            # Get recent forwarding logs
            cur.execute("""
                SELECT source_message_id, dest_message_id, 
                       message_text, received_at, forwarded_at
                FROM forwarding_logs
                WHERE user_id = %s
                ORDER BY created_at DESC
                LIMIT 5
            """, (user_id,))
            forwarding_logs = cur.fetchall()

    return render_template('dashboard/overview.html',
                       telegram_authorized=bool(primary_account),
                       telegram_username=primary_account['telegram_username'] if primary_account else None,
                       telegram_auth_date=primary_account['auth_date'] if primary_account else None,
                       source_channel=config['source_channel'] if config else None,
                       dest_channel=config['destination_channel'] if config else None,
                       is_active=config['is_active'] if config else False,
                       replacements_count=replacements_count,
                       forwarding_logs=forwarding_logs)

@app.route('/authorization')
@login_required
def authorization():
    """Authorization page route handler"""
    with get_db() as conn:
        with conn.cursor(cursor_factory=DictCursor) as cur:
            cur.execute("""
                SELECT telegram_id, telegram_username, auth_date, session_string, is_primary
                FROM telegram_accounts 
                WHERE user_id = %s
                ORDER BY is_primary DESC, auth_date DESC
            """, (session.get('user_id'),))
            accounts = cur.fetchall()

    return render_template('dashboard/authorization.html',
                         telegram_accounts=accounts)

@app.route('/replacements')
@login_required
def replacements():
    with get_db() as conn:
        with conn.cursor(cursor_factory=DictCursor) as cur:
            cur.execute("""
                SELECT original_text, replacement_text
                FROM text_replacements
                WHERE user_id = %s
                ORDER BY id DESC
            """, (session.get('user_id'),))
            replacements = {row['original_text']: row['replacement_text'] 
                          for row in cur.fetchall()}

    return render_template('dashboard/replacements.html',
                         replacements=replacements)

@app.route('/forwarding')
@login_required
@async_route
async def forwarding():
    """Forwarding page route handler"""
    try:
        user_id = session.get('user_id')

        with get_db() as conn:
            with conn.cursor(cursor_factory=DictCursor) as cur:
                # Get user data with telegram auth info
                cur.execute("""
                    SELECT telegram_id, telegram_username, auth_date, session_string
                    FROM users
                    WHERE id = %s
                """, (user_id,))
                user = cur.fetchone()

                if not bool(user and user['telegram_id'] and user['session_string']): #Simplified check
                    return render_template('dashboard/forwarding.html',
                                      telegram_authorized=False)

                # Get forwarding config
                cur.execute("""
                    SELECT source_channel, destination_channel, is_active
                    FROM forwarding_configs
                    WHERE user_id = %s
                """, (user_id,))
                config = cur.fetchone()

                # Get replacements
                cur.execute("""
                    SELECT original_text, replacement_text
                    FROM text_replacements
                    WHERE user_id = %s
                """, (user_id,))
                replacements = {row['original_text']: row['replacement_text'] 
                              for row in cur.fetchall()}

        # Get channel list from Telegram
        channels = []
        try:
            # Create a new client instance for this request
            client = await telegram_manager.get_client(user['session_string'])
            logger.info("✅ Got Telegram client")

            # Get all dialogs (channels)
            async for dialog in client.iter_dialogs():
                if dialog.is_channel:
                    channel_id = str(dialog.id)
                    if not channel_id.startswith('-100'):
                        channel_id = f'-100{channel_id.lstrip("-")}'

                    channels.append({
                        'id': channel_id,
                        'name': dialog.name
                    })

            logger.info(f"✅ Found {len(channels)} channels")

            # Clean up client after use
            await telegram_manager._cleanup_client()

        except Exception as e:
            logger.error(f"❌ Channel list error: {str(e)}")
            return render_template('dashboard/forwarding.html',
                              telegram_authorized=True,
                              error="Failed to fetch channels. Please try logging out and authorizing your Telegram account again.")

        return render_template('dashboard/forwarding.html',
                          telegram_authorized=True,
                          channels=channels,
                          source_channel=config['source_channel'] if config else None,
                          dest_channel=config['destination_channel'] if config else None,
                          bot_status=config['is_active'] if config else False,
                          replacements=replacements)

    except Exception as e:
        logger.error(f"❌ Forwarding page error: {str(e)}")
        return render_template('dashboard/forwarding.html',
                          telegram_authorized=False,
                          error="An error occurred loading the forwarding page. Please try again.")

@app.route('/send-otp', methods=['POST'])
@async_route
async def send_otp():
    """Send OTP for Telegram authorization"""
    try:
        # Store important session data
        important_data = {k: session.get(k) for k in ['user_id', 'csrf_token']}

        phone = request.form.get('phone')
        if not phone:
            return jsonify({'error': 'Phone number is required'}), 400

        # Clean phone number format
        phone = phone.strip()
        if not phone.startswith('+'):
            phone = '+' + phone

        try:
            # Initialize client
            client = await telegram_manager.get_client()
            if not client:
                raise Exception("Failed to initialize Telegram client")
            logger.info("✅ Got Telegram client")

            # Send code request
            try:
                sent = await client.send_code_request(phone)
                logger.info(f"✅ Successfully sent OTP to {phone}")

                # Save verification data
                telegram_manager.save_verification_data(phone, sent.phone_code_hash)

                # Update session
                session.clear()
                session.update(important_data)
                session['user_phone'] = phone
                session['phone_code_hash'] = sent.phone_code_hash
                session['otp_sent_at'] = int(time.time())
                session.permanent = True

                return jsonify({'message': 'OTP sent successfully'})

            except PhoneNumberInvalidError:
                logger.error(f"❌ Invalid phone number format: {phone}")
                return jsonify({'error': 'Please enter a valid phone number with country code'}), 400
            except Exception as e:
                error_msg = str(e).lower()
                if "resendcoderequest" in error_msg:
                    return jsonify({'error': 'Please wait a few minutes before requesting a new OTP'}), 429
                logger.error(f"❌ Failed to send OTP: {str(e)}")
                return jsonify({'error': 'Failed to send OTP. Please try again.'}), 500

        except Exception as e:
            logger.error(f"❌ Critical error: {str(e)}")
            return jsonify({'error': 'Failed to connect to Telegram. Please try again.'}), 500

    except Exception as e:
        logger.error(f"❌ Unexpected error: {str(e)}")
        return jsonify({'error': 'An unexpected error occurred'}), 500

@app.route('/verify-otp', methods=['POST'])
@async_route
async def verify_otp():
    """Verify OTP and complete Telegram authorization"""
    try:
        # Get verification data
        stored_phone, stored_hash = telegram_manager.get_verification_data()
        phone = session.get('user_phone', stored_phone)
        phone_code_hash = session.get('phone_code_hash', stored_hash)
        otp = request.form.get('otp')
        password = request.form.get('password')

        if not all([phone, phone_code_hash, otp]):
            logger.error("❌ Missing verification data")
            return jsonify({'error': 'OTP session expired. Please request a new OTP'}), 400

        try:
            client = await telegram_manager.get_client()
            if not client:
                raise Exception("Failed to initialize Telegram client")
            logger.info("✅ Got Telegram client for verification")

            try:
                # First try to sign in with OTP
                await client.sign_in(phone=phone, code=otp, phone_code_hash=phone_code_hash)
            except SessionPasswordNeededError:
                # If 2FA is enabled and password is provided
                if password:
                    try:
                        await client.sign_in(password=password)
                    except Exception as e:
                        logger.error(f"❌ 2FA verification failed: {str(e)}")
                        return jsonify({'error': 'Invalid 2FA password'}), 400
                else:
                    logger.info("2FA required")
                    return jsonify({
                        'error': 'two_factor_needed',
                        'message': 'Two-factor authentication required'
                    })

            # Check if successfully authorized
            if await client.is_user_authorized():
                me = await client.get_me()
                session_string = client.session.save()

                with get_db() as conn:
                    with conn.cursor() as cur:
                        # Check if this Telegram account is already connected to the same user
                        cur.execute("""
                            SELECT user_id, is_active 
                            FROM telegram_accounts 
                            WHERE telegram_id = %s
                        """, (me.id,))
                        existing = cur.fetchone()

                        if existing:
                            if existing['user_id'] == session.get('user_id'):
                                if existing['is_active']:
                                    return jsonify({'error': 'This Telegram account is already connected to your account'}), 400
                                else:
                                    # If account exists but is inactive, reactivate it
                                    cur.execute("""
                                        UPDATE telegram_accounts 
                                        SET session_string = %s,
                                            auth_date = CURRENT_TIMESTAMP,
                                            is_active = true
                                        WHERE telegram_id = %s AND user_id = %s
                                        RETURNING id
                                    """, (session_string, me.id, session.get('user_id')))

                                    if cur.fetchone():
                                        logger.info(f"✅ Successfully reactivated Telegram account {me.id}")
                                        return jsonify({'message': 'Account reactivated successfully'})
                                    else:
                                        return jsonify({'error': 'Failed to reactivate account'}), 500
                            else:
                                # Check if the account is active for another user
                                if existing['is_active']:
                                    return jsonify({'error': 'This Telegram account is connected to another user'}), 400

                        # If no active connection exists, create a new one
                        cur.execute("""
                            INSERT INTO telegram_accounts 
                            (user_id, telegram_id, telegram_username, auth_date, session_string, is_primary, is_active)
                            VALUES (%s, %s, %s, CURRENT_TIMESTAMP, %s, 
                                   NOT EXISTS(SELECT 1 FROM telegram_accounts WHERE user_id = %s AND is_active = true),
                                   true)
                            RETURNING id
                        """, (session.get('user_id'), me.id, me.username, session_string, session.get('user_id')))

                        result = cur.fetchone()
                        if result:
                            logger.info(f"✅ Successfully added new Telegram account {me.id}")
                            return jsonify({'message': 'Authorization successful'})
                        else:
                            return jsonify({'error': 'Failed to add account'}), 500

            else:
                logger.error("❌ Authorization failed")
                return jsonify({'error': 'Authorization failed'}), 400

        except Exception as e:
            error_msg = str(e).lower()
            logger.error(f"❌ Sign in error: {error_msg}")

            if "phone code expired" in error_msg:
                return jsonify({'error': 'OTP expired. Please request a new one.'}), 400
            elif "phone code invalid" in error_msg:
                return jsonify({'error': 'Invalid OTP. Please try again.'}), 400
            elif "resendcoderequest" in error_msg:
                return jsonify({'error': 'Please wait a few minutes before requesting a new OTP.'}), 429

            return jsonify({'error': 'Failed to verify OTP'}), 400

    except Exception as e:
        logger.error(f"❌ Verification error: {str(e)}")
        return jsonify({'error': str(e)}), 500

@app.route('/disconnect/<int:telegram_id>', methods=['POST'])
@login_required
@async_route
async def disconnect_account(telegram_id):
    """Disconnect specific Telegram account"""
    try:
        user_id = session.get('user_id')

        with get_db() as conn:
            with conn.cursor() as cur:
                # Verify ownership
                cur.execute("""
                    SELECT session_string, is_primary 
                    FROM telegram_accounts 
                    WHERE user_id = %s AND telegram_id = %s
                """, (user_id, telegram_id))
                account = cur.fetchone()

                if not account:
                    return jsonify({'error': 'Account not found'}), 404

                if account[1]:  # is_primary
                    return jsonify({'error': 'Cannot disconnect primary account. Make another account primary first.'}), 400

                # Remove account
                cur.execute("""
                    DELETE FROM telegram_accounts 
                    WHERE user_id = %s AND telegram_id = %s
                    RETURNING id
                """, (user_id, telegram_id))

                if cur.fetchone():
                    # Clean up any running sessions
                    import main
                    main.remove_user_session(telegram_id)

                    logger.info(f"✅ Successfully disconnected Telegram account {telegram_id}")
                    return jsonify({'message': 'Successfully disconnected'})
                else:
                    return jsonify({'error': 'Failed to disconnect account'}), 500

    except Exception as e:
        logger.error(f"❌ Disconnect error: {str(e)}")
        return jsonify({'error': str(e)}), 500

@app.route('/make-primary/<int:telegram_id>', methods=['POST'])
@login_required
def make_primary(telegram_id):
    """Make specific Telegram account primary"""
    try:
        user_id = session.get('user_id')

        with get_db() as conn:
            with conn.cursor() as cur:
                # Verify ownership
                cur.execute("""
                    SELECT 1 FROM telegram_accounts 
                    WHERE user_id = %s AND telegram_id = %s
                """, (user_id, telegram_id))

                if not cur.fetchone():
                    return jsonify({'error': 'Account not found'}), 404

                # Update primary status
                cur.execute("""
                    UPDATE telegram_accounts 
                    SET is_primary = CASE
                        WHEN telegram_id = %s THEN true
                        ELSE false
                    END
                    WHERE user_id = %s
                """, (telegram_id, user_id))

                conn.commit()
                logger.info(f"✅ Successfully set Telegram account {telegram_id} as primary")
                return jsonify({'message': 'Successfully updated primary account'})

    except Exception as e:
        logger.error(f"❌ Make primary error: {str(e)}")
        return jsonify({'error': str(e)}), 500

@app.route('/update-channels', methods=['POST'])
@login_required
def update_channels():
    """Save channel configuration without starting the bot"""
    try:
        source = request.form.get('source')
        destination = request.form.get('destination')
        user_id = session.get('user_id')

        if not all([source, destination, user_id]):
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
                    # First, delete any existing config
                    cur.execute("""
                        DELETE FROM forwarding_configs 
                        WHERE user_id = %s
                    """, (user_id,))

                    # Then insert new config
                    cur.execute("""
                        INSERT INTO forwarding_configs 
                        (user_id, source_channel, destination_channel, is_active)
                        VALUES (%s, %s, %s, false)
                    """, (user_id, source, destination))

                    # Stop any running forwarding
                    import main
                    main.remove_user_session(session.get('telegram_id'))

                    return jsonify({'message': 'Channels updated successfully'})
                except psycopg2.Error as e:
                    conn.rollback()
                    logger.error(f"❌ Database error in update_channels: {str(e)}")
                    return jsonify({'error': 'Failed to save channel configuration'}), 400

    except Exception as e:
        logger.error(f"❌ Channel update error: {str(e)}")
        return jsonify({'error': str(e)}), 500

@app.route('/bot/toggle', methods=['POST'])
@login_required
def toggle_bot():
    """Toggle bot status based on saved configuration"""
    try:
        status = request.form.get('status') == 'true'
        user_id = session.get('user_id')
        telegram_id = session.get('telegram_id')
        session_string = session.get('session_string')

        if not telegram_id or not session_string:
            return jsonify({'error': 'Please authorize Telegram first'}), 401

        with get_db() as conn:
            with conn.cursor(cursor_factory=DictCursor) as cur:
                # Get forwarding config
                cur.execute("""
                    SELECT source_channel, destination_channel, is_active
                    FROM forwarding_configs
                    WHERE user_id = %s
                """, (user_id,))
                config = cur.fetchone()

                if not config:
                    return jsonify({'error': 'Please configure channels first'}), 400

                if status:
                    try:
                        # Update database first
                        cur.execute("""
                            UPDATE forwarding_configs
                            SET is_active = true,
                                updated_at = CURRENT_TIMESTAMP
                            WHERE user_id = %s
                            RETURNING id
                        """, (user_id,))

                        if not cur.fetchone():
                            return jsonify({'error': 'Failed to update forwarding status'}), 500

                        # Start bot
                        import main
                        source_channel = str(config['source_channel'])
                        dest_channel = str(config['destination_channel'])

                        # Ensure proper channel ID format
                        if not source_channel.startswith('-100'):
                            source_channel = f"-100{source_channel.lstrip('-')}"
                        if not dest_channel.startswith('-100'):
                            dest_channel = f"-100{dest_channel.lstrip('-')}"

                        success = main.add_user_session(
                            user_id=int(telegram_id),
                            session_string=session_string,
                            source_channel=source_channel,
                            destination_channel=dest_channel
                        )

                        if not success:
                            # Rollback on failure
                            cur.execute("""
                                UPDATE forwarding_configs 
                                SET is_active = false 
                                WHERE user_id = %s
                            """, (user_id,))
                            return jsonify({'error': 'Failed to start bot. Please try again.'}), 500

                        return jsonify({
                            'status': True,
                            'message': 'Bot is now running'
                        })

                    except Exception as e:
                        logger.error(f"❌ Bot start error: {str(e)}")
                        # Ensure inactive on error
                        cur.execute("""
                            UPDATE forwarding_configs 
                            SET is_active = false 
                            WHERE user_id = %s
                        """, (user_id,))
                        return jsonify({'error': str(e)}), 500
                else:
                    try:
                        # Update database first
                        cur.execute("""
                            UPDATE forwarding_configs 
                            SET is_active = false,
                                updated_at = CURRENT_TIMESTAMP
                            WHERE user_id = %s
                        """, (user_id,))

                        # Stop bot
                        import main
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
        user_id = session.get('user_id')

        with get_db() as conn:
            with conn.cursor(cursor_factory=DictCursor) as cur:
                cur.execute("""
                    SELECT original_text, replacement_text, is_active 
                    FROM text_replacements
                    WHERE user_id = %s
                    ORDER BY id DESC
                """, (user_id,))
                replacements = {row['original_text']: {
                    'text': row['replacement_text'],
                    'is_active': row['is_active']
                } for row in cur.fetchall()}
                return jsonify(replacements)
    except Exception as e:
        logger.error(f"❌ Get replacements error: {str(e)}")
        return jsonify({'error': str(e)}), 500

@app.route('/add-replacement', methods=['POST'])
@login_required
def add_replacement():
    try:
        if not request.form:
            return jsonify({'error': 'No form data received'}), 400

        original = request.form.get('original')
        replacement = request.form.get('replacement')
        user_id = session.get('user_id')

        if not all([original, replacement, user_id]):
            return jsonify({'error': 'Missing required data'}), 400

        # Validate input lengths
        if len(original) > 500 or len(replacement) > 500:
            return jsonify({'error': 'Text too long (max 500 characters)'}), 400

        with get_db() as conn:
            with conn.cursor() as cur:
                try:
                    # Check if replacement already exists
                    cur.execute("""
                        SELECT COUNT(*) 
                        FROM text_replacements 
                        WHERE user_id = %s AND original_text = %s
                    """, (user_id, original))

                    if cur.fetchone()[0] > 0:
                        return jsonify({'error': 'This replacement already exists'}), 400

                    # Add new replacement
                    cur.execute("""
                        INSERT INTO text_replacements (user_id, original_text, replacement_text, is_active)
                        VALUES (%s, %s, %s, true)
                        RETURNING id
                    """, (user_id, original, replacement))

                    replacement_id = cur.fetchone()[0]
                    logger.info(f"Added replacement {replacement_id} for user {user_id}: '{original}' → '{replacement}'")

                    # Update bot replacements if running
                    import main
                    main.update_user_replacements(session.get('telegram_id'))

                    return jsonify({
                        'message': 'Replacement added successfully',                        'original': original,
                        'replacement': replacement
                    })

                except psycopg2.Error as e:
                    logger.error(f"Database error in add_replacement: {str(e)}")
                    return jsonify({'error': 'Failed to add replacement'}), 400

    except Exception as e:
        logger.error(f"Error in add_replacement: {str(e)}")
        return jsonify({'error': 'An error occurred while adding the replacement'}), 500

@app.route('/remove-replacement', methods=['POST'])
@login_required
def remove_replacement():
    try:
        original = request.form.get('original')
        user_id = session.get('user_id')

        if not all([original, user_id]):
            return jsonify({'error': 'Missing required data'}), 400

        with get_db() as conn:
            with conn.cursor() as cur:
                # Remove replacement
                cur.execute("""
                    DELETE FROM text_replacements 
                    WHERE user_id = %s AND original_text = %s
                    RETURNING id
                """, (user_id, original))

                result = cur.fetchone()
                if result:
                    logger.info(f"Removed replacement {result[0]} for user {user_id}")

                    # Update bot replacements if running
                    import main
                    main.update_user_replacements(session.get('telegram_id'))

                    return jsonify({'message': 'Replacement removed successfully'})
                else:
                    return jsonify({'error': 'Replacement not found'}), 404

    except Exception as e:
        logger.error(f"❌ Remove replacement error: {str(e)}")
        return jsonify({'error': str(e)}), 500

@app.route('/clear-replacements', methods=['POST'])
@login_required
def clear_replacements():
    try:
        user_id = session.get('user_id')

        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    DELETE FROM text_replacements
                    WHERE user_id = %s
                """, (user_id,))

        # Update bot
        import main
        main.update_user_replacements(session.get('telegram_id'))

        return jsonify({'message': 'All replacements cleared'})
    except Exception as e:
        logger.error(f"❌ Clear replacements error: {str(e)}")
        return jsonify({'error': str(e)}), 500

@app.route('/toggle-replacement', methods=['POST'])
@login_required
def toggle_replacement():
    try:
        if not request.form:
            return jsonify({'error': 'No form data received'}), 400

        original = request.form.get('original')
        user_id = session.get('user_id')

        if not all([original, user_id]):
            return jsonify({'error': 'Missing required data'}), 400

        with get_db() as conn:
            with conn.cursor() as cur:
                # Toggle is_active status
                cur.execute("""
                    UPDATE text_replacements 
                    SET is_active = NOT is_active
                    WHERE user_id = %s AND original_text = %s
                    RETURNING id, is_active
                """, (user_id, original))

                result = cur.fetchone()
                if result:
                    logger.info(f"Toggled replacement {result[0]} for user {user_id} to {result[1]}")

                    # Update bot replacements if running
                    import main
                    main.update_user_replacements(session.get('telegram_id'))

                    return jsonify({'message': 'Replacement updated successfully'})
                else:
                    return jsonify({'error': 'Replacement not found'}), 404

    except Exception as e:
        logger.error(f"❌ Toggle replacement error: {str(e)}")
        return jsonify({'error': str(e)}), 500

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


# Add datetime filter
@app.template_filter('datetime')
def format_datetime(timestamp):
    return datetime.fromtimestamp(timestamp).strftime('%Y-%m-%d %H:%M:%S')

@app.route('/accounts')
@login_required
def accounts():
    """Account management dashboard"""
    try:
        user_id = session.get('user_id')
        with get_db() as conn:
            with conn.cursor(cursor_factory=DictCursor) as cur:
                # Get all telegram accounts with their forwarding configs
                cur.execute("""
                    SELECT 
                        ta.*,
                        fc.source_channel,
                        fc.destination_channel,
                        fc.is_active,
                        COUNT(fl.id) as messages_count
                    FROM telegram_accounts ta
                    LEFT JOIN forwarding_configs fc ON fc.user_id = ta.user_id 
                        AND fc.telegram_id = ta.telegram_id
                    LEFT JOIN forwarding_logs fl ON fl.user_id = ta.user_id 
                        AND fl.telegram_id = ta.telegram_id
                    WHERE ta.user_id = %s
                    GROUP BY ta.id, fc.id
                    ORDER BY ta.is_primary DESC, ta.auth_date DESC
                """, (user_id,))
                accounts = cur.fetchall()

                return render_template('dashboard/accounts.html', accounts=accounts)

    except Exception as e:
        logger.error(f"❌ Account dashboard error: {str(e)}")
        flash('Failed to load accounts dashboard', 'error')
        return redirect(url_for('dashboard'))

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
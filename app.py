import os
import logging
import threading
from flask import Flask, render_template, request, session, redirect, url_for, jsonify
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
from werkzeug.security import generate_password_hash, check_password_hash
from forms import LoginForm, RegisterForm
from flask_wtf.csrf import CSRFProtect

# Set up logging
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Configure Flask application
app = Flask(__name__)
app.config.update(
    SECRET_KEY=os.environ.get('FLASK_SECRET_KEY', os.urandom(24)),
    SESSION_TYPE='filesystem',
    PERMANENT_SESSION_LIFETIME=timedelta(days=7),
    SESSION_PERMANENT=True,
    DEBUG=True
)

# Initialize CSRF protection
csrf = CSRFProtect(app)

# Initialize session
Session(app)

# Create event loop for the application
loop = asyncio.new_event_loop()
asyncio.set_event_loop(loop)

class TelegramManager:
    def __init__(self, api_id, api_hash):
        self.api_id = api_id
        self.api_hash = api_hash
        self._lock = threading.Lock()
        self._client = None
        self._loop = None

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
                    app_version="1.0"
                )
                await self._client.connect()
                logger.info("✅ Telegram client initialized")
        except Exception as e:
            logger.error(f"❌ Client initialization error: {str(e)}")
            self._client = None
            raise

    async def get_client(self, session_string=None):
        """Get the Telegram client instance"""
        with self._lock:
            try:
                if self._client and self._client.is_connected():
                    if session_string and self._client.session.save() != session_string:
                        await self._cleanup_client()
                    elif await self._client.is_user_authorized():
                        return self._client
                    else:
                        await self._cleanup_client()

                # Initialize new client with session string
                await self._initialize_client(session_string)
                if not await self._client.is_user_authorized():
                    await self._cleanup_client()
                    raise Exception("User not authorized")
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

# Database pool for connections
db_pool = psycopg2.pool.ThreadedConnectionPool(
    minconn=1,
    maxconn=10,
    dsn=os.getenv('DATABASE_URL')
)

# Async route decorator
def async_route(f):
    @wraps(f)
    def wrapped(*args, **kwargs):
        try:
            return loop.run_until_complete(f(*args, **kwargs))
        except Exception as e:
            logger.error(f"❌ Async route error: {str(e)}")
            raise
    return wrapped

# Database connection context manager
@contextmanager
def get_db():
    conn = db_pool.getconn()
    try:
        conn.autocommit = True
        yield conn
    finally:
        db_pool.putconn(conn)

# Authentication decorator
def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not session.get('user_id'):
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function

def check_telegram_auth(user_data):
    """Helper to check Telegram authorization status"""
    return bool(user_data and user_data['telegram_id'] and user_data['session_string'])

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
            session['telegram_id'] = user['telegram_id']
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

                # Stop forwarding if active
                cur.execute("""
                    UPDATE forwarding_configs 
                    SET is_active = false 
                    WHERE user_id = %s
                """, (user_id,))

        # Remove user session from main.py if telegram was authorized
        if session.get('telegram_id'):
            import main
            main.remove_user_session(session['telegram_id'])

    session.clear()
    return redirect(url_for('login'))

@app.route('/dashboard')
@login_required
def dashboard():
    user_id = session.get('user_id')
    with get_db() as conn:
        with conn.cursor(cursor_factory=DictCursor) as cur:
            # Get user data
            cur.execute("""
                SELECT telegram_id, telegram_username, auth_date, session_string
                FROM users
                WHERE id = %s
            """, (user_id,))
            user = cur.fetchone()

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

    return render_template('dashboard/overview.html',
                         telegram_authorized=check_telegram_auth(user),
                         telegram_username=user['telegram_username'] if user else None,
                         telegram_auth_date=user['auth_date'] if user else None,
                         source_channel=config['source_channel'] if config else None,
                         dest_channel=config['destination_channel'] if config else None,
                         is_active=config['is_active'] if config else False,
                         replacements_count=replacements_count)


@app.route('/authorization')
@login_required
def authorization():
    """Authorization page route handler"""
    with get_db() as conn:
        with conn.cursor(cursor_factory=DictCursor) as cur:
            cur.execute("""
                SELECT telegram_id, telegram_username, auth_date, session_string
                FROM users 
                WHERE id = %s
            """, (session.get('user_id'),))
            user = cur.fetchone()

    return render_template('dashboard/authorization.html',
                         telegram_authorized=check_telegram_auth(user),
                         telegram_username=user['telegram_username'] if user else None,
                         telegram_auth_date=user['auth_date'] if user else None)

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

                if not check_telegram_auth(user):
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
    try:
        # Check if user already has a valid session
        with get_db() as conn:
            with conn.cursor(cursor_factory=DictCursor) as cur:
                cur.execute("""
                    SELECT telegram_id, telegram_username, auth_date, session_string
                    FROM users
                    WHERE id = %s AND session_string IS NOT NULL
                """, (session.get('user_id'),))
                existing_session = cur.fetchone()

                if existing_session and existing_session['session_string']:
                    try:
                        # Try to use existing session
                        client = await telegram_manager.get_client(existing_session['session_string'])
                        if await client.is_user_authorized():
                            # Update session data
                            session['telegram_id'] = existing_session['telegram_id']
                            session['session_string'] = existing_session['session_string']
                            logger.info("✅ Reused existing Telegram session")
                            return jsonify({'message': 'Already authorized'}), 200
                    except Exception as e:
                        logger.warning(f"⚠️ Failed to reuse session: {str(e)}")
                        # If session is invalid, continue with new auth
                        cur.execute("""
                            UPDATE users 
                            SET session_string = NULL 
                            WHERE id = %s
                        """, (session.get('user_id'),))

        # Store important session data
        important_data = {
            'user_id': session.get('user_id'),
            'csrf_token': session.get('csrf_token')
        }

        # Initialize new client for OTP
        phone = request.form.get('phone')
        if not phone:
            return jsonify({'error': 'Phone number is required'}), 400

        if not phone.startswith('+91'):
            return jsonify({'error': 'Phone number must start with +91'}), 400

        try:
            # Get client and send OTP
            client = await telegram_manager.get_client()
            sent = await client.send_code_request(phone)

            # Clear session but preserve important data
            session.clear()
            for key, value in important_data.items():
                session[key] = value

            # Store phone data
            session['user_phone'] = phone
            session['phone_code_hash'] = sent.phone_code_hash

            logger.info(f"✅ OTP sent successfully to {phone}")
            return jsonify({'message': 'OTP sent successfully'})

        except PhoneNumberInvalidError:
            logger.error(f"❌ Invalid phone number: {phone}")
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
            client = await telegram_manager.get_client()

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
                    return jsonify({'error': 'Invalid 2FA password'}), 400

            if await client.is_user_authorized():
                # Get user info
                me = await client.get_me()
                session_string = client.session.save()

                # Store or update user in database with telegram info
                with get_db() as conn:
                    with conn.cursor(cursor_factory=DictCursor) as cur:
                        cur.execute("""
                            UPDATE users 
                            SET telegram_id = %s,
                                first_name = %s,
                                telegram_username = %s,
                                auth_date = CURRENT_TIMESTAMP,
                                session_string = %s
                            WHERE id = %s
                            RETURNING id
                        """, (me.id, me.first_name, me.username, session_string, session.get('user_id')))

                        if not cur.fetchone():
                            return jsonify({'error': 'User not found'}), 404

                # Set session data
                session['telegram_id'] = me.id
                session['session_string'] = session_string
                logger.info(f"✅ Telegram authorization successful for user {me.id}")
                return jsonify({'message': 'Authorization successful'})
            else:
                return jsonify({'error': 'Authentication failed. Please try again.'}), 400

        except Exception as e:
            logger.error(f"❌ Sign in error: {str(e)}")
            return jsonify({'error': str(e)}), 500

    except Exception as e:
        logger.error(f"❌ Critical error in verify_otp: {str(e)}")
        return jsonify({'error': str(e)}), 500

@app.before_request
def check_session_expiry():
    if request.endpoint not in ['login', 'static', 'send-otp', 'verify-otp', 'check-auth', 'logout', 'register', 'register_post', 'login_post']:
        # Get current user data
        user_id = session.get('user_id')
        if not user_id:
            session.clear()
            return redirect(url_for('login'))

        with get_db() as conn:
            with conn.cursor(cursor_factory=DictCursor) as cur:
                # Check user login status and telegram auth
                cur.execute("""
                    SELECT is_logged_in, telegram_id, session_string 
                    FROM users 
                    WHERE id = %s
                """, (user_id,))
                user = cur.fetchone()

                if not user or not user['is_logged_in']:
                    session.clear()
                    return redirect(url_for('login'))

                # Update session with telegram data if available
                if user['telegram_id'] and user['session_string']:
                    session['telegram_id'] = user['telegram_id']
                    session['session_string'] = user['session_string']

@app.route('/check-auth')
def check_auth():
    try:
        user_id = session.get('user_id')
        if not user_id:
            return jsonify({'authenticated': False}), 401

        # Verify user is logged in in database
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT is_logged_in 
                    FROM users 
                    WHERE id = %s
                """, (user_id,))
                result = cur.fetchone()
                if not result or not result[0]:
                    session.clear()
                    return jsonify({'authenticated': False}), 401

        return jsonify({'authenticated': True})
    except Exception as e:
        logger.error(f"❌ Auth check error: {str(e)}")
        return jsonify({'authenticated': False}), 401

@app.route('/update-channels', methods=['POST'])
@login_required
def update_channels():
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
                    cur.execute("""
                        INSERT INTO forwarding_configs (user_id, source_channel, destination_channel)
                        VALUES (%s, %s, %s)
                        ON CONFLICT (user_id) 
                        DO UPDATE SET 
                            source_channel = EXCLUDED.source_channel,
                            destination_channel = EXCLUDED.destination_channel,
                            updated_at = CURRENT_TIMESTAMP
                    """, (user_id, source, destination))
                except psycopg2.Error as e:
                    error_msg = handle_db_error(e, "update_channels")
                    return jsonify({'error': error_msg}), 400

        # Update running bot if exists
        import main
        main.update_user_channels(session.get('telegram_id'), source, destination)

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
                    FROM forwarding_configs
                    WHERE user_id = %s
                """, (session.get('user_id'),))
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
                        """, (session.get('user_id'), session_string))

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
                        """, (session.get('user_id'),))

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
        user_id = session.get('user_id')

        with get_db() as conn:
            with conn.cursor(cursor_factory=DictCursor) as cur:
                cur.execute("""
                    SELECT original_text, replacement_text 
                    FROM text_replacements
                    WHERE user_id = %s
                    ORDER BY id DESC
                """, (user_id,))
                replacements = {row['original_text']: row['replacement_text'] for row in cur.fetchall()}
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
                        INSERT INTO text_replacements (user_id, original_text, replacement_text)
                        VALUES (%s, %s, %s)
                    """, (user_id, original, replacement))

                    # Update bot if needed
                    import main
                    main.update_user_replacements(session.get('telegram_id'))

                    return jsonify({'message': 'Replacement added successfully'})
                except psycopg2.Error as e:
                    error_msg = handle_db_error(e, "add_replacement")
                    return jsonify({'error': error_msg}), 400

    except Exception as e:
        logger.error(f"❌ Add replacement error: {str(e)}")
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
                cur.execute("""
                    DELETE FROM text_replacements 
                    WHERE user_id = %s AND original_text = %s
                """, (user_id, original))

        # Update bot
        import main
        main.update_user_replacements(session.get('telegram_id'))

        return jsonify({'message': 'Replacement removed successfully'})
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


# Initialize the Telegram manager
telegram_manager = TelegramManager(
    int(os.getenv('API_ID')),
    os.getenv('API_HASH')
)

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
import os
import logging
from flask import Flask, render_template, request, session, redirect, url_for, jsonify, g
from telethon import TelegramClient, events
from telethon.errors import SessionPasswordNeededError, PhoneNumberInvalidError
import asyncio
from functools import wraps
from asgiref.sync import async_to_sync
import psycopg2
from psycopg2.extras import DictCursor
from datetime import timedelta
import tempfile
from telethon.sessions import StringSession

# Set up logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

app = Flask(__name__)
app.debug = False
app.secret_key = os.getenv('SESSION_SECRET', os.urandom(24))

# Configure session
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(days=7)
app.config['SESSION_TYPE'] = 'filesystem'

# Telegram API credentials
API_ID = int(os.getenv('API_ID', '27202142'))
API_HASH = os.getenv('API_HASH', 'db4dd0d95dc68d46b77518bf997ed165')

def get_db():
    if 'db' not in g:
        try:
            g.db = psycopg2.connect(
                os.getenv('DATABASE_URL'),
                application_name='telegram_bot_web'
            )
            g.db.autocommit = True
            return g.db
        except Exception as e:
            logger.error(f"Database connection error: {str(e)}")
            raise
    return g.db

@app.teardown_appcontext
def close_db(e=None):
    db = g.pop('db', None)
    if db is not None:
        db.close()

# Add healthcheck endpoints
@app.route('/')
def root():
    if session.get('logged_in') and session.get('user_phone'):
        return redirect(url_for('dashboard'))
    return redirect(url_for('login'))

@app.route('/health')
def health_check():
    try:
        # Check database connection
        db = get_db()
        with db.cursor() as cur:
            cur.execute('SELECT 1')
        return jsonify({'status': 'healthy'}), 200
    except Exception as e:
        logger.error(f"Health check failed: {str(e)}")
        return jsonify({'status': 'unhealthy', 'error': str(e)}), 500

def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not session.get('logged_in') or not session.get('user_phone'):
            logger.warning("Session invalid, redirecting to login")
            session.clear()
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function

def get_user_id(phone):
    db = get_db()
    with db.cursor() as cur:
        cur.execute("SELECT id FROM users WHERE phone = %s", (phone,))
        result = cur.fetchone()
        if result:
            return result[0]
        cur.execute("INSERT INTO users (phone) VALUES (%s) RETURNING id", (phone,))
        db.commit()
        return cur.fetchone()[0]

@app.route('/login')
def login():
    if session.get('logged_in') and session.get('user_phone'):
        logger.info(f"User {session.get('user_phone')} already logged in, redirecting to dashboard")
        return redirect(url_for('dashboard'))
    return render_template('login.html')

def get_temp_session_path(phone):
    temp_dir = tempfile.gettempdir()
    return os.path.join(temp_dir, f"telegram_session_{phone}")

@app.route('/send-otp', methods=['POST'])
def send_otp():
    phone = request.form.get('phone')
    if not phone:
        logger.error("Phone number missing in request")
        return jsonify({'error': 'Phone number is required'}), 400

    if not phone.startswith('+91'):
        logger.error(f"Invalid phone number format: {phone}")
        return jsonify({'error': 'Phone number must start with +91'}), 400

    try:
        async def send_code():
            logger.info(f"Initializing Telegram client for phone: {phone}")

            session_path = get_temp_session_path(phone)
            client = TelegramClient(session_path, API_ID, API_HASH)

            try:
                await client.connect()

                try:
                    sent = await client.send_code_request(phone)
                    session['user_phone'] = phone
                    session['phone_code_hash'] = sent.phone_code_hash
                    session['temp_session_path'] = session_path
                    session.permanent = True
                    logger.info("Code request sent successfully")

                    if client.is_connected():
                        await client.disconnect()
                    return {'message': 'OTP sent successfully'}
                except PhoneNumberInvalidError:
                    logger.error(f"Invalid phone number: {phone}")
                    if client.is_connected():
                        await client.disconnect()
                    if os.path.exists(session_path):
                        os.remove(session_path)
                    return {'error': 'Invalid phone number'}, 400

            except Exception as e:
                logger.error(f"Error in send_code: {str(e)}")
                if client.is_connected():
                    await client.disconnect()
                if os.path.exists(session_path):
                    os.remove(session_path)
                raise e

        result = asyncio.run(send_code())
        if isinstance(result, tuple):
            return jsonify(result[0]), result[1]
        return jsonify(result)
    except Exception as e:
        logger.error(f"Error in send_otp route: {str(e)}")
        return jsonify({'error': str(e)}), 500

async def get_session_string(client):
    if client.is_connected() and await client.is_user_authorized():
        return StringSession.save(client.session)
    return None

@app.route('/verify-otp', methods=['POST'])
def verify_otp():
    phone = session.get('user_phone')
    phone_code_hash = session.get('phone_code_hash')
    temp_session_path = session.get('temp_session_path')
    otp = request.form.get('otp')
    password = request.form.get('password')

    if not phone or not phone_code_hash or not temp_session_path:
        logger.error("Missing session data")
        return jsonify({'error': 'Session expired. Please try again.'}), 400

    if not otp:
        logger.error("OTP missing in request")
        return jsonify({'error': 'OTP is required'}), 400

    try:
        async def verify():
            logger.info(f"Verifying OTP for phone: {phone}")
            client = TelegramClient(temp_session_path, API_ID, API_HASH)

            try:
                await client.connect()
                logger.info("Connected to Telegram")

                try:
                    await client.sign_in(phone, otp, phone_code_hash=phone_code_hash)
                    logger.info("Sign in successful")
                except SessionPasswordNeededError:
                    logger.info("2FA password needed")
                    if not password:
                        if client.is_connected():
                            await client.disconnect()
                        return {
                            'error': 'two_factor_needed',
                            'message': 'Two-factor authentication is required'
                        }
                    try:
                        await client.sign_in(password=password)
                        logger.info("2FA verification successful")
                    except Exception as e:
                        logger.error(f"2FA verification failed: {e}")
                        if client.is_connected():
                            await client.disconnect()
                        return {'error': 'Invalid 2FA password'}, 400

                if await client.is_user_authorized():
                    session_string = await get_session_string(client)
                    if session_string:
                        user_id = get_user_id(phone)
                        db = get_db()
                        with db.cursor() as cur:
                            cur.execute("""
                                INSERT INTO user_sessions (user_id, session_string)
                                VALUES (%s, %s)
                                ON CONFLICT (user_id) 
                                DO UPDATE SET session_string = EXCLUDED.session_string
                            """, (user_id, session_string))
                            db.commit()
                            logger.info("Saved session string to database")

                    # Clear existing session and set new data
                    session.clear()
                    session['logged_in'] = True
                    session['user_phone'] = phone
                    session['user_id'] = user_id
                    session.permanent = True
                    # Make sure session is saved
                    session.modified = True
                    logger.info(f"Session variables set - user_phone: {phone}, user_id: {user_id}")

                    if client.is_connected():
                        await client.disconnect()
                    if os.path.exists(temp_session_path):
                        os.remove(temp_session_path)
                    return {'message': 'Login successful'}
                else:
                    if client.is_connected():
                        await client.disconnect()
                    return {'error': 'Invalid OTP'}, 400

            except Exception as e:
                logger.error(f"Error in verification: {str(e)}")
                if client.is_connected():
                    await client.disconnect()
                raise e

        result = asyncio.run(verify())
        if isinstance(result, tuple):
            return jsonify(result[0]), result[1]
        return jsonify(result)
    except Exception as e:
        logger.error(f"Error in verify_otp route: {str(e)}")
        if temp_session_path and os.path.exists(temp_session_path):
            os.remove(temp_session_path)
        return jsonify({'error': str(e)}), 500

@app.route('/dashboard')
@login_required
def dashboard():
    try:
        channels = asyncio.run(get_channels())
        return render_template('dashboard.html', channels=channels)
    except Exception as e:
        logger.error(f"Error in dashboard route: {str(e)}")
        session.clear()
        return redirect(url_for('login'))

@app.route('/add-replacement', methods=['POST'])
@login_required
def add_replacement():
    try:
        original = request.form.get('original')
        replacement = request.form.get('replacement')
        user_phone = session.get('user_phone')

        if not original or not replacement:
            return jsonify({'error': 'Both original and replacement text are required'}), 400

        user_id = get_user_id(user_phone)

        db = get_db()
        with db.cursor() as cur:
            cur.execute("""
                INSERT INTO text_replacements (user_id, original_text, replacement_text)
                VALUES (%s, %s, %s)
                ON CONFLICT (user_id, original_text) 
                DO UPDATE SET replacement_text = EXCLUDED.replacement_text
            """, (user_id, original, replacement))
            db.commit()

        import main
        main.load_user_replacements(user_id)

        return jsonify({'message': 'Replacement added successfully'})
    except Exception as e:
        logger.error(f"Error adding replacement: {str(e)}")
        return jsonify({'error': str(e)}), 500

@app.route('/get-replacements')
@login_required
def get_replacements():
    try:
        user_phone = session.get('user_phone')
        user_id = get_user_id(user_phone)

        db = get_db()
        with db.cursor(cursor_factory=DictCursor) as cur:
            cur.execute("""
                SELECT original_text, replacement_text 
                FROM text_replacements 
                WHERE user_id = %s
            """, (user_id,))
            replacements = {row['original_text']: row['replacement_text'] for row in cur.fetchall()}

        return jsonify(replacements)
    except Exception as e:
        logger.error(f"Error getting replacements: {str(e)}")
        return jsonify({'error': str(e)}), 500

@app.route('/remove-replacement', methods=['POST'])
@login_required
def remove_replacement():
    try:
        original = request.form.get('original')
        user_phone = session.get('user_phone')

        if not original:
            return jsonify({'error': 'Original text is required'}), 400

        user_id = get_user_id(user_phone)

        db = get_db()
        with db.cursor() as cur:
            cur.execute("""
                DELETE FROM text_replacements 
                WHERE user_id = %s AND original_text = %s
            """, (user_id, original))
            deleted = cur.rowcount > 0
            db.commit()

        if deleted:
            import main
            main.load_user_replacements(user_id)
            return jsonify({'message': 'Replacement removed successfully'})
        else:
            return jsonify({'error': 'Replacement not found'}), 404

    except Exception as e:
        logger.error(f"Error removing replacement: {str(e)}")
        return jsonify({'error': str(e)}), 500

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))


@app.route('/update-channels', methods=['POST'])
@login_required
def update_channels():
    try:
        source = request.form.get('source')
        destination = request.form.get('destination')

        if not source or not destination:
            logger.error("Missing channel IDs in request")
            return jsonify({'error': 'Both source and destination channels are required'}), 400

        if source == destination:
            logger.error("Source and destination channels cannot be the same")
            return jsonify({'error': 'Source and destination channels must be different'}), 400

        if not source.startswith('-100'):
            source = f"-100{source.lstrip('-')}"
        if not destination.startswith('-100'):
            destination = f"-100{destination.lstrip('-')}"

        user_phone = session.get('user_phone')
        user_id = get_user_id(user_phone)

        save_user_channel_config(user_id, source, destination)

        session['source_channel'] = source
        session['dest_channel'] = destination

        logger.info(f"Updated channel configuration - Source: {source}, Destination: {destination}")
        return jsonify({'message': 'Channel configuration updated successfully'})

    except Exception as e:
        logger.error(f"Error updating channels: {str(e)}")
        return jsonify({'error': str(e)}), 500

def get_user_channel_config(user_id):
    db = get_db()
    with db.cursor(cursor_factory=DictCursor) as cur:
        cur.execute("""
            SELECT source_channel, destination_channel 
            FROM channel_configs 
            WHERE user_id = %s
        """, (user_id,))
        return cur.fetchone()

def save_user_channel_config(user_id, source_channel, destination_channel):
    db = get_db()
    with db.cursor() as cur:
        cur.execute("""
            INSERT INTO channel_configs (user_id, source_channel, destination_channel)
            VALUES (%s, %s, %s)
            ON CONFLICT (user_id) 
            DO UPDATE SET 
                source_channel = EXCLUDED.source_channel,
                destination_channel = EXCLUDED.destination_channel
        """, (user_id, source_channel, destination_channel))
        db.commit()

@app.route('/clear-replacements', methods=['POST'])
@login_required
def clear_replacements():
    try:
        user_phone = session.get('user_phone')
        user_id = get_user_id(user_phone)

        db = get_db()
        with db.cursor() as cur:
            cur.execute("DELETE FROM text_replacements WHERE user_id = %s", (user_id,))
            db.commit()

        import main
        main.TEXT_REPLACEMENTS = {}  
        main.load_user_replacements(user_id)  

        logger.info("Cleared all text replacements for user")
        return jsonify({'message': 'All replacements cleared'})
    except Exception as e:
        logger.error(f"Error clearing replacements: {str(e)}")
        return jsonify({'error': str(e)}), 500

@app.route('/bot/toggle', methods=['POST'])
@login_required
def toggle_bot():
    try:
        status = request.form.get('status') == 'true'
        source = session.get('source_channel')
        destination = session.get('dest_channel')

        if not source or not destination:
            logger.error("Channel configuration missing")
            return jsonify({'error': 'Please configure source and destination channels first'}), 400

        try:
            import main
            main.SOURCE_CHANNEL = source
            main.DESTINATION_CHANNEL = destination
            logger.info(f"Bot channels configured - Source: {main.SOURCE_CHANNEL}, Destination: {main.DESTINATION_CHANNEL}")
        except Exception as e:
            logger.error(f"Error configuring bot channels: {e}")
            return jsonify({'error': 'Failed to configure bot channels'}), 500

        session['bot_running'] = status
        logger.info(f"Bot status changed to: {'running' if status else 'stopped'}")

        return jsonify({
            'status': status,
            'message': f"Bot is now {'running' if status else 'stopped'}"
        })
    except Exception as e:
        logger.error(f"Error toggling bot: {str(e)}")
        return jsonify({'error': str(e)}), 500

async def get_channels():
    phone = session.get('user_phone')
    client = TelegramClient(None, API_ID, API_HASH) 

    try:
        await client.connect()
        channels = []
        async for dialog in client.iter_dialogs():
            if dialog.is_channel:
                channels.append({
                    'id': dialog.id,
                    'name': dialog.name
                })
        await client.disconnect()

        user_id = get_user_id(phone)
        config = get_user_channel_config(user_id)
        if config:
            for channel in channels:
                if str(channel['id']) == config['source_channel']:
                    channel['is_source'] = True
                if str(channel['id']) == config['destination_channel']:
                    channel['is_destination'] = True

        return channels
    except Exception as e:
        logger.error(f"Error fetching channels: {str(e)}")
        if client and client.connected:
            await client.disconnect()
        raise e

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
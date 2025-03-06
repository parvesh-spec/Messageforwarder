import os
import logging
from flask import Flask, render_template, request, session, redirect, url_for, jsonify, g
from telethon import TelegramClient, events
from telethon.errors import SessionPasswordNeededError, PhoneNumberInvalidError, AuthKeyUnregisteredError
import asyncio
from functools import wraps
from asgiref.sync import async_to_sync
import psycopg2
from psycopg2.extras import DictCursor
import json
from flask_session import Session
from datetime import timedelta
from flask_sqlalchemy import SQLAlchemy
from threading import Thread
from contextlib import contextmanager

# Set up logging
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

app = Flask(__name__)
app.secret_key = os.urandom(24)

# Update the session configuration
app.config['SQLALCHEMY_DATABASE_URI'] = os.getenv('DATABASE_URL')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {
    'pool_size': 10,
    'pool_timeout': 30,
    'pool_recycle': 1800,
}
db = SQLAlchemy(app)

# Configure Flask-Session with SQLAlchemy
class FlaskSession(db.Model):
    __tablename__ = 'session'

    id = db.Column(db.Integer, primary_key=True)
    session_id = db.Column(db.String(255), unique=True, nullable=False)
    data = db.Column(db.LargeBinary)
    expiry = db.Column(db.DateTime)

app.config['SESSION_TYPE'] = 'sqlalchemy'
app.config['SESSION_SQLALCHEMY'] = db
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(days=7)
app.config['SESSION_PERMANENT'] = True
Session(app)

# Telegram client manager
class TelegramManager:
    def __init__(self, session_name, api_id, api_hash):
        self.session_name = session_name
        self.api_id = api_id
        self.api_hash = api_hash
        self.client = None

    async def get_client(self):
        if not self.client:
            self.client = TelegramClient(
                self.session_name,
                self.api_id,
                self.api_hash,
                device_model="Replit Web",
                system_version="Linux",
                app_version="1.0",
                connection_retries=None  # Infinite retries
            )

        if not self.client.is_connected():
            await self.client.connect()

        return self.client

    async def disconnect(self):
        if self.client and self.client.is_connected():
            await self.client.disconnect()
            self.client = None

# Create global Telegram manager
telegram_manager = TelegramManager(
    'anon',
    int(os.getenv('API_ID', '27202142')),
    os.getenv('API_HASH', 'db4dd0d95dc68d46b77518bf997ed165')
)

def get_db():
    if 'db' not in g:
        g.db = psycopg2.connect(
            os.getenv('DATABASE_URL'),
            application_name='telegram_bot_web'
        )
        g.db.autocommit = True  # Prevent transaction locks
    return g.db

@app.teardown_appcontext
def close_db(e=None):
    db = g.pop('db', None)
    if db is not None:
        db.close()

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

def get_user_id(phone):
    try:
        conn = get_db()
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM users WHERE phone = %s", (phone,))
            result = cur.fetchone()
            if result:
                return result[0]
            cur.execute("INSERT INTO users (phone) VALUES (%s) RETURNING id", (phone,))
            conn.commit() # Corrected to use conn, not db
            return cur.fetchone()[0]
    except Exception as e:
        logger.error(f"Error getting user ID: {str(e)}")
        return None

@app.route('/')
def login():
    return render_template('login.html')

@app.route('/send-otp', methods=['POST'])
@async_route
async def send_otp():
    phone = request.form.get('phone')
    if not phone:
        logger.error("Phone number missing in request")
        return jsonify({'error': 'Phone number is required'}), 400

    if not phone.startswith('+91'):
        logger.error(f"Invalid phone number format: {phone}")
        return jsonify({'error': 'Phone number must start with +91'}), 400

    try:
        client = await telegram_manager.get_client()

        try:
            # If session exists, check if it's valid
            if await client.is_user_authorized():
                logger.info(f"Found valid session for {phone}")
                session['user_phone'] = phone
                session['logged_in'] = True
                return jsonify({'message': 'Already authorized', 'already_authorized': True})

            # Send code and get the phone_code_hash
            sent = await client.send_code_request(phone)
            session['user_phone'] = phone
            session['phone_code_hash'] = sent.phone_code_hash
            logger.info("Code request sent successfully")
            return jsonify({'message': 'OTP sent successfully'})

        except PhoneNumberInvalidError:
            logger.error(f"Invalid phone number: {phone}")
            return jsonify({'error': 'Invalid phone number'}), 400

        except Exception as e:
            logger.error(f"Error in send_otp: {str(e)}")
            return jsonify({'error': str(e)}), 500

        finally:
            await telegram_manager.disconnect()

    except Exception as e:
        logger.error(f"Critical error in send_otp: {str(e)}")
        return jsonify({'error': 'Server error'}), 500

@app.route('/verify-otp', methods=['POST'])
@async_route
async def verify_otp():
    phone = session.get('user_phone')
    phone_code_hash = session.get('phone_code_hash')
    otp = request.form.get('otp')
    password = request.form.get('password')  # For 2FA

    if not phone or not phone_code_hash:
        logger.error("Missing session data")
        return jsonify({'error': 'Session expired. Please try again.'}), 400

    if not otp:
        logger.error("OTP missing in request")
        return jsonify({'error': 'OTP is required'}), 400

    try:
        client = await telegram_manager.get_client()

        try:
            # Sign in with phone code hash
            await client.sign_in(phone, otp, phone_code_hash=phone_code_hash)
            logger.info("Sign in successful")
        except SessionPasswordNeededError:
            logger.info("2FA password needed")
            if not password:
                return jsonify({
                    'error': 'two_factor_needed',
                    'message': 'Two-factor authentication is required'
                })
            try:
                await client.sign_in(password=password)
                logger.info("2FA verification successful")
            except Exception as e:
                logger.error(f"2FA verification failed: {e}")
                return jsonify({'error': 'Invalid 2FA password'}), 400

        if await client.is_user_authorized():
            session['logged_in'] = True
            return jsonify({'message': 'Login successful'})
        else:
            return jsonify({'error': 'Invalid OTP'}), 400

    except Exception as e:
        logger.error(f"Error in verify_otp: {str(e)}")
        return jsonify({'error': str(e)}), 500

    finally:
        await telegram_manager.disconnect()

@app.route('/dashboard')
@login_required
@async_route
async def dashboard():
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
            db = get_db()
            with db.cursor(cursor_factory=DictCursor) as cur:
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
            logger.error(f"Error loading dashboard data: {str(e)}")
            raise

        finally:
            await telegram_manager.disconnect()

    except Exception as e:
        logger.error(f"Critical error in dashboard: {str(e)}")
        return redirect(url_for('login'))

@app.route('/add-replacement', methods=['POST'])
@login_required
def add_replacement():
    try:
        original = request.form.get('original')
        replacement = request.form.get('replacement')
        user_phone = session.get('user_phone')

        if not original or not replacement:
            logger.error("Missing replacement text parameters")
            return jsonify({'error': 'Both original and replacement text are required'}), 400

        user_id = get_user_id(user_phone)
        if user_id is None:
            return jsonify({'error': 'Failed to get user ID'}), 500

        logger.info(f"Adding replacement for user {user_id}: '{original}' → '{replacement}'")

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
        main.CURRENT_USER_ID = user_id
        main.load_user_replacements(user_id)
        logger.info(f"Reloaded replacements for user {user_id}")

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
        if user_id is None:
            return jsonify({'error': 'Failed to get user ID'}), 500
        logger.info(f"Fetching replacements for user {user_id}")

        db = get_db()
        with db.cursor(cursor_factory=DictCursor) as cur:
            cur.execute("""
                SELECT original_text, replacement_text 
                FROM text_replacements 
                WHERE user_id = %s
                ORDER BY LENGTH(original_text) DESC
            """, (user_id,))
            replacements = {row['original_text']: row['replacement_text'] for row in cur.fetchall()}
            logger.info(f"Retrieved {len(replacements)} replacements for user {user_id}")

            # Ensure text replacements are loaded in main.py
            import main
            main.CURRENT_USER_ID = user_id
            main.load_user_replacements(user_id)

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
        if user_id is None:
            return jsonify({'error': 'Failed to get user ID'}), 500

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
    try:
        phone = session.get('user_phone')
        if phone:
            session_file = f"sessions/{phone}"
            if os.path.exists(session_file):
                os.remove(session_file)
                logger.info(f"Removed session file for {phone}")
        session.clear()
        return redirect(url_for('login'))
    except Exception as e:
        logger.error(f"Error in logout route: {str(e)}")
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
            db = get_db()
            with db.cursor() as cur:
                # Create table if not exists
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS channel_config (
                        id SERIAL PRIMARY KEY,
                        source_channel TEXT NOT NULL,
                        destination_channel TEXT NOT NULL,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                """)

                # Insert new configuration
                cur.execute("""
                    INSERT INTO channel_config (source_channel, destination_channel)
                    VALUES (%s, %s)
                """, (source, destination))

            # Update bot if running
            if session.get('bot_running'):
                import main
                main.SOURCE_CHANNEL = source
                main.DESTINATION_CHANNEL = destination
                logger.info("✅ Updated running bot configuration")

            logger.info(f"✅ Channel config updated - Source: {source}, Destination: {destination}")
            return jsonify({'message': 'Channel configuration updated successfully'})

        except psycopg2.Error as e:
            logger.error(f"❌ Database error: {e}")
            return jsonify({'error': 'Database error updating channels'}), 500
        except Exception as e:
            logger.error(f"❌ Channel update error: {e}")
            return jsonify({'error': str(e)}), 500

    except Exception as e:
        logger.error(f"❌ Route error: {str(e)}")
        return jsonify({'error': str(e)}), 500

@app.route('/clear-replacements', methods=['POST'])
@login_required
def clear_replacements():
    try:
        user_phone = session.get('user_phone')
        user_id = get_user_id(user_phone)
        if user_id is None:
            return jsonify({'error': 'Failed to get user ID'}), 500

        db = get_db()
        with db.cursor() as cur:
            cur.execute("DELETE FROM text_replacements WHERE user_id = %s", (user_id,))
            db.commit()

        logger.info("Cleared all text replacements for user")
        return jsonify({'message': 'All replacements cleared'})
    except Exception as e:
        logger.error(f"Error clearing replacements: {str(e)}")
        return jsonify({'error': str(e)}), 500

def run_async(coro):
    """Run an async function in a new event loop"""
    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        return loop.run_until_complete(coro)
    finally:
        loop.close()
        asyncio.set_event_loop(None)

@app.route('/bot/toggle', methods=['POST'])
@login_required
def toggle_bot():
    """Toggle bot status and manage Telegram client"""
    try:
        status = request.form.get('status') == 'true'
        source = session.get('source_channel')
        destination = session.get('dest_channel')

        if not source or not destination:
            logger.error("❌ Channel configuration missing")
            return jsonify({'error': 'Please configure source and destination channels first'}), 400

        try:
            import main

            # Test database connection first
            db = get_db()
            with db.cursor() as cur:
                cur.execute("SELECT 1")
            logger.info("✅ Database connection verified")

            if status:
                logger.info("🔄 Starting bot...")

                # Stop existing client if running
                if hasattr(main, 'client') and main.client:
                    try:
                        def disconnect_client():
                            loop = asyncio.new_event_loop()
                            asyncio.set_event_loop(loop)
                            try:
                                loop.run_until_complete(main.client.disconnect())
                            finally:
                                loop.close()
                                asyncio.set_event_loop(None)

                        Thread(target=disconnect_client).start()
                        logger.info("✅ Disconnected existing client")
                    except Exception as e:
                        logger.warning(f"⚠️ Error disconnecting client: {e}")

                # Reset channels
                main.SOURCE_CHANNEL = None
                main.DESTINATION_CHANNEL = None
                main.client = None

                # Update channels
                main.SOURCE_CHANNEL = source
                main.DESTINATION_CHANNEL = destination
                logger.info(f"✅ Updated channels - Source: {source}, Destination: {destination}")

                # Start new client
                def start_bot():
                    loop = asyncio.new_event_loop()
                    asyncio.set_event_loop(loop)
                    try:
                        loop.run_until_complete(main.main())
                    except Exception as e:
                        logger.error(f"❌ Bot thread error: {e}")
                    finally:
                        loop.close()

                Thread(target=start_bot, daemon=True).start()
                logger.info("✅ Started bot in new thread")

            else:
                logger.info("🔄 Stopping bot...")

                if hasattr(main, 'client') and main.client:
                    try:
                        def stop_client():
                            loop = asyncio.new_event_loop()
                            asyncio.set_event_loop(loop)
                            try:
                                loop.run_until_complete(main.client.disconnect())
                            finally:
                                loop.close()
                                asyncio.set_event_loop(None)

                        Thread(target=stop_client).start()
                        logger.info("✅ Disconnected client")
                    except Exception as e:
                        logger.warning(f"⚠️ Error disconnecting client: {e}")

                # Reset state
                main.SOURCE_CHANNEL = None
                main.DESTINATION_CHANNEL = None
                main.client = None

            # Update session
            session['bot_running'] = status
            logger.info(f"✅ Bot status changed to: {'running' if status else 'stopped'}")

            return jsonify({
                'status': status,
                'message': f"Bot is now {'running' if status else 'stopped'}"
            })

        except ImportError:
            logger.error("❌ Failed to import main module")
            return jsonify({'error': 'Bot module not found'}), 500
        except Exception as e:
            logger.error(f"❌ Bot toggle error: {str(e)}")
            import traceback
            logger.error(f"❌ Traceback:\n{traceback.format_exc()}")
            return jsonify({'error': 'Failed to toggle bot status'}), 500

    except Exception as e:
        logger.error(f"❌ Route error: {str(e)}")
        return jsonify({'error': str(e)}), 500

if __name__ == '__main__':
    os.makedirs('sessions', exist_ok=True)
    # ALWAYS serve the app on port 5000
    app.run(host='0.0.0.0', port=5000, debug=True)
from flask import Flask, render_template, request, session, redirect, url_for, jsonify
from telethon import TelegramClient, events
from telethon.errors import SessionPasswordNeededError
import asyncio
import os
from functools import wraps
from asgiref.sync import async_to_sync
from datetime import datetime, timedelta
from models import db, User, UserSession, Channel, BotConfig
import time
from sqlalchemy.exc import OperationalError

app = Flask(__name__)
app.secret_key = os.urandom(24)  # for session management

# Database configuration with SSL and connection pooling
db_url = os.environ.get('DATABASE_URL')
if db_url and db_url.startswith("postgres://"):
    db_url = db_url.replace("postgres://", "postgresql://", 1)

print(f"Initializing database with URL type: {db_url.split('://')[0] if db_url else 'None'}")

app.config['SQLALCHEMY_DATABASE_URI'] = db_url
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {
    'pool_size': 20,  # Increased pool size
    'max_overflow': 5,
    'pool_timeout': 30,
    'pool_recycle': 1800,
    'pool_pre_ping': True,
    'connect_args': {
        'sslmode': 'require',
        'connect_timeout': 10
    }
}

def retry_on_db_error(func):
    def wrapper(*args, **kwargs):
        max_retries = 3
        retry_delay = 1  # seconds

        for attempt in range(max_retries):
            try:
                return func(*args, **kwargs)
            except OperationalError as e:
                if attempt == max_retries - 1:
                    raise
                print(f"Database error, retrying ({attempt + 1}/{max_retries}): {e}")
                time.sleep(retry_delay)
                db.session.rollback()
    return wrapper

try:
    print("Initializing database connection...")
    db.init_app(app)
    print("Database initialized successfully")

    # Create all database tables
    with app.app_context():
        print("Creating database tables...")
        db.create_all()
        print("Database tables created successfully")
except Exception as e:
    print(f"Database initialization error: {e}")
    raise

# Telegram API credentials
API_ID = int(os.getenv('API_ID', '27202142'))
API_HASH = os.getenv('API_HASH', 'db4dd0d95dc68d46b77518bf997ed165')

def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'session_id' not in session:
            print("No session_id found, redirecting to login")
            return redirect(url_for('login'))

        try:
            # Verify session in database
            with app.app_context():
                user_session = UserSession.query.filter_by(
                    session_id=session.get('session_id'),
                    is_active=True
                ).first()

                if not user_session or user_session.expires_at < datetime.utcnow():
                    print("Session expired or not found")
                    session.clear()
                    return redirect(url_for('login'))

                # Ensure logged_in flag is set
                session['logged_in'] = True
                print(f"Session verified for user: {user_session.user_id}")
                return f(*args, **kwargs)
        except Exception as e:
            print(f"Session verification error: {e}")
            session.clear()
            return redirect(url_for('login'))

    return decorated_function

@app.route('/')
def login():
    try:
        with app.app_context():
            # Check if user has valid session
            if 'session_id' in session:
                user_session = UserSession.query.filter_by(
                    session_id=session.get('session_id'),
                    is_active=True
                ).first()
                if user_session and user_session.expires_at > datetime.utcnow():
                    print("Valid session found, redirecting to dashboard")
                    session['logged_in'] = True  # Ensure logged_in flag is set
                    return redirect(url_for('dashboard'))
    except Exception as e:
        print(f"Error checking session: {e}")
    return render_template('login.html')

@app.route('/send-otp', methods=['POST'])
def send_otp():
    try:
        phone = request.form.get('phone')
        if not phone:
            return jsonify({'error': 'Phone number is required'}), 400

        if not phone.startswith('+91'):
            return jsonify({'error': 'Phone number must start with +91'}), 400

        async def send_code():
            client = TelegramClient(f"sessions/{phone}", API_ID, API_HASH)
            await client.connect()

            try:
                if await client.is_user_authorized():
                    # Create session for already authorized users
                    me = await client.get_me()
                    await client.disconnect()

                    # Store user and session info
                    session_id = os.urandom(24).hex()
                    session['session_id'] = session_id
                    session['logged_in'] = True
                    session['user_phone'] = phone

                    return {'message': 'Already authorized. Redirecting to dashboard...'}, 200

                # For new users, send OTP
                print(f"Sending OTP for phone: {phone}")
                sent = await client.send_code_request(phone)
                session['user_phone'] = phone
                session['phone_code_hash'] = sent.phone_code_hash
                await client.disconnect()
                return {'message': 'OTP sent successfully'}

            except Exception as e:
                await client.disconnect()
                raise e

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        result = loop.run_until_complete(send_code())
        loop.close()

        return jsonify(result if isinstance(result, dict) else result[0]), \
               200 if isinstance(result, dict) else result[1]

    except Exception as e:
        print(f"Error in send_otp: {str(e)}")
        return jsonify({'error': str(e)}), 500

@app.route('/verify-otp', methods=['POST'])
def verify_otp():
    phone = session.get('user_phone')
    phone_code_hash = session.get('phone_code_hash')
    otp = request.form.get('otp')
    password = request.form.get('password')  # For 2FA

    if not phone or not otp or not phone_code_hash:
        return jsonify({'error': 'Phone, OTP and verification data are required'}), 400

    try:
        async def verify():
            client = TelegramClient(f"sessions/{phone}", API_ID, API_HASH)
            await client.connect()

            try:
                await client.sign_in(phone, otp, phone_code_hash=phone_code_hash)
            except SessionPasswordNeededError:
                if not password:
                    return {
                        'error': 'two_factor_needed',
                        'message': 'Two-factor authentication is required'
                    }, 403
                await client.sign_in(password=password)

            if await client.is_user_authorized():
                me = await client.get_me()
                await client.disconnect()

                try:
                    with app.app_context():
                        print(f"Updating user and creating session for phone: {phone}")
                        # Update user and create session in database
                        user = User.query.filter_by(phone=phone).first()
                        user.telegram_id = me.id
                        user.last_login = datetime.utcnow()

                        # Create new session
                        session_id = os.urandom(24).hex()
                        user_session = UserSession(
                            user_id=user.id,
                            session_id=session_id,
                            expires_at=datetime.utcnow() + timedelta(days=7)
                        )

                        # Deactivate old sessions
                        UserSession.query.filter_by(user_id=user.id).update({
                            'is_active': False
                        })

                        db.session.add(user_session)
                        db.session.commit()
                        print(f"Session created with ID: {session_id}")

                        # Store session ID in Flask session
                        session['session_id'] = session_id
                        session['logged_in'] = True
                        print("User successfully logged in")

                        return {'message': 'Login successful'}
                except Exception as db_error:
                    print(f"Database error in verify_otp: {db_error}")
                    if 'db' in locals():
                        db.session.rollback()
                    return {'error': 'Database error occurred'}, 500
            else:
                await client.disconnect()
                return {'error': 'Invalid OTP'}, 400

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        result = loop.run_until_complete(verify())
        loop.close()

        return jsonify(result if isinstance(result, dict) else result[0]), \
               200 if isinstance(result, dict) else result[1]
    except Exception as e:
        print(f"Error in verify_otp: {str(e)}")
        return jsonify({'error': str(e)}), 500

@app.route('/dashboard')
@login_required
def dashboard():
    async def get_channels():
        phone = session.get('user_phone')
        if not phone:
            return []

        client = TelegramClient(f"sessions/{phone}", API_ID, API_HASH)
        await client.connect()

        channels = []
        async for dialog in client.iter_dialogs():
            if dialog.is_channel:
                channels.append({
                    'id': dialog.id,
                    'name': dialog.name,
                    'username': dialog.entity.username if hasattr(dialog.entity, 'username') else None
                })

        await client.disconnect()
        return channels

    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        channels = loop.run_until_complete(get_channels())
        loop.close()

        return render_template('dashboard.html', channels=channels)
    except Exception as e:
        print(f"Error fetching channels: {e}")
        return render_template('dashboard.html', channels=[])

@app.route('/bot/toggle', methods=['POST'])
@login_required
def toggle_bot():
    try:
        status = request.form.get('status') == 'true'
        session['bot_active'] = status

        if status:
            # Start message forwarding
            source = session.get('source_channel')
            dest = session.get('destination_channel')
            phone = session.get('user_phone')

            if source and dest and phone:
                asyncio.run(forward_messages(source, dest, phone))

        return jsonify({'status': status})

    except Exception as e:
        print(f"Error toggling bot: {e}")
        return jsonify({'error': 'Internal server error'}), 500

@app.route('/update-channel', methods=['POST'])
@login_required
def update_channel():
    try:
        channel_id = request.form.get('channel_id')
        is_source = request.form.get('is_source') == 'true'
        is_destination = request.form.get('is_destination') == 'true'

        if not channel_id:
            return jsonify({'error': 'Channel ID is required'}), 400

        # Store selected channels in session
        if is_source:
            session['source_channel'] = channel_id
        if is_destination:
            session['destination_channel'] = channel_id

        return jsonify({
            'status': 'success',
            'message': 'Channel updated successfully'
        })

    except Exception as e:
        print(f"Error updating channel: {e}")
        return jsonify({'error': 'Internal server error'}), 500

@app.route('/channel-status')
@login_required
def channel_status():
    try:
        return jsonify({
            'source_channel': session.get('source_channel'),
            'destination_channel': session.get('destination_channel'),
            'bot_active': session.get('bot_active', False)
        })

    except Exception as e:
        print(f"Error getting channel status: {e}")
        return jsonify({'error': 'Internal server error'}), 500

@app.route('/logout')
@login_required
def logout():
    try:
        with app.app_context():
            # Get current session and deactivate it
            user_session = UserSession.query.filter_by(
                session_id=session.get('session_id'),
                is_active=True
            ).first()
            if user_session:
                user_session.is_active = False
                db.session.commit()
                print(f"Deactivated session: {user_session.session_id}")

        # Clear Flask session
        session.clear()
        print("Session cleared")
        return redirect(url_for('login'))
    except Exception as e:
        print(f"Error in logout: {e}")
        session.clear()
        return redirect(url_for('login'))

@app.route('/resend-otp', methods=['POST'])
def resend_otp():
    try:
        # Check if user is already logged in
        if 'logged_in' in session and session['logged_in']:
            return jsonify({'message': 'Already logged in. Please logout first.'}), 400

        phone = session.get('user_phone')
        if not phone:
            return jsonify({'error': 'No phone number found in session. Please enter your phone number again.'}), 400

        async def resend_code():
            client = TelegramClient(f"sessions/{phone}", API_ID, API_HASH)
            await client.connect()

            try:
                print(f"Resending OTP for phone: {phone}")
                sent = await client.send_code_request(phone)
                session['phone_code_hash'] = sent.phone_code_hash
                await client.disconnect()

                try:
                    with app.app_context():
                        print(f"Updating user last login for phone: {phone}")
                        user = User.query.filter_by(phone=phone).first()
                        if user:
                            user.last_login = datetime.utcnow()
                            db.session.commit()
                            print(f"Updated last login for user ID: {user.id}")
                except Exception as db_error:
                    print(f"Database error in resend_otp: {db_error}")
                    if 'db' in locals():
                        db.session.rollback()
                    return {'error': 'Database error occurred'}, 500

                return {'message': 'OTP resent successfully'}
            except Exception as e:
                if client and client.is_user_authorized():
                    await client.disconnect()
                    return {'message': 'You are already authorized. Please logout first.'}, 400
                await client.disconnect()
                raise e

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        result = loop.run_until_complete(resend_code())
        loop.close()

        return jsonify(result if isinstance(result, dict) else result[0]), \
               200 if isinstance(result, dict) else result[1]
    except Exception as e:
        print(f"Error in resend_otp: {str(e)}")
        return jsonify({'error': str(e)}), 500


async def forward_messages(source_channel_id, destination_channel_id, phone):
    client = TelegramClient(f"sessions/{phone}", API_ID, API_HASH)
    await client.connect()
    try:
        source_channel = await client.get_entity(source_channel_id)
        destination_channel = await client.get_entity(destination_channel_id)

        async for message in client.iter_messages(source_channel):
            await client.send_message(destination_channel, message)

    except Exception as e:
        print(f"Error forwarding messages: {e}")
    finally:
        await client.disconnect()


# Add these new routes after the existing routes


if __name__ == '__main__':
    # Make sure sessions directory exists
    os.makedirs('sessions', exist_ok=True)
    # Always serve on port 5000
    app.run(host='0.0.0.0', port=5000)
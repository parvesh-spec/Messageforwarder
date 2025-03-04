from flask import Flask, render_template, request, session, redirect, url_for, jsonify
from telethon import TelegramClient, events
from telethon.errors import SessionPasswordNeededError
import asyncio
import os
from functools import wraps
from asgiref.sync import async_to_sync
from datetime import datetime, timedelta
from models import db, User, UserSession, Channel, BotConfig

app = Flask(__name__)
app.secret_key = os.urandom(24)  # for session management

# Database configuration
app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get('DATABASE_URL')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db.init_app(app)

# Create all database tables
with app.app_context():
    db.create_all()

# Telegram API credentials
API_ID = int(os.getenv('API_ID', '27202142'))
API_HASH = os.getenv('API_HASH', 'db4dd0d95dc68d46b77518bf997ed165')

def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_phone' not in session:
            return redirect(url_for('login'))

        # Verify session in database
        user_session = UserSession.query.filter_by(
            session_id=session.get('session_id'),
            is_active=True
        ).first()

        if not user_session or user_session.expires_at < datetime.utcnow():
            session.clear()
            return redirect(url_for('login'))

        return f(*args, **kwargs)
    return decorated_function

@app.route('/')
def login():
    return render_template('login.html')

@app.route('/send-otp', methods=['POST'])
def send_otp():
    phone = request.form.get('phone')
    if not phone:
        return jsonify({'error': 'Phone number is required'}), 400

    if not phone.startswith('+91'):
        return jsonify({'error': 'Phone number must start with +91'}), 400

    try:
        os.makedirs('sessions', exist_ok=True)

        async def send_code():
            client = TelegramClient(f"sessions/{phone}", API_ID, API_HASH)
            await client.connect()

            if not await client.is_user_authorized():
                sent = await client.send_code_request(phone)
                session['user_phone'] = phone
                session['phone_code_hash'] = sent.phone_code_hash
                await client.disconnect()

                # Store or update user in database
                user = User.query.filter_by(phone=phone).first()
                if not user:
                    user = User(phone=phone)
                    db.session.add(user)
                    db.session.commit()

                return {'message': 'OTP sent successfully'}
            else:
                await client.disconnect()
                return {'message': 'Already authorized'}

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        result = loop.run_until_complete(send_code())
        loop.close()

        return jsonify(result)
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

                # Store session ID in Flask session
                session['session_id'] = session_id
                session['logged_in'] = True

                return {'message': 'Login successful'}
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
        client = TelegramClient(f"sessions/{phone}", API_ID, API_HASH)
        await client.connect()

        channels = []
        async for dialog in client.iter_dialogs():
            if dialog.is_channel:
                channels.append({
                    'id': dialog.id,
                    'name': dialog.name
                })

                # Store channel in database if not exists
                user = User.query.filter_by(phone=phone).first()
                existing_channel = Channel.query.filter_by(
                    user_id=user.id,
                    telegram_channel_id=dialog.id
                ).first()

                if not existing_channel:
                    channel = Channel(
                        user_id=user.id,
                        telegram_channel_id=dialog.id,
                        channel_name=dialog.name
                    )
                    db.session.add(channel)
                    db.session.commit()

        await client.disconnect()
        return channels

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    channels = loop.run_until_complete(get_channels())
    loop.close()

    return render_template('dashboard.html', channels=channels)

@app.route('/bot/toggle', methods=['POST'])
@login_required
def toggle_bot():
    status = request.form.get('status') == 'true'
    phone = session.get('user_phone')
    user = User.query.filter_by(phone=phone).first()

    # Update bot configuration
    bot_config = BotConfig.query.filter_by(user_id=user.id).first()
    if not bot_config:
        bot_config = BotConfig(user_id=user.id)
        db.session.add(bot_config)

    bot_config.is_active = status
    bot_config.updated_at = datetime.utcnow()
    db.session.commit()

    return jsonify({'status': status})

@app.route('/replace/toggle', methods=['POST'])
@login_required
def toggle_replace():
    status = request.form.get('status') == 'true'
    phone = session.get('user_phone')
    user = User.query.filter_by(phone=phone).first()

    # Update text replacement configuration
    bot_config = BotConfig.query.filter_by(user_id=user.id).first()
    if not bot_config:
        bot_config = BotConfig(user_id=user.id)
        db.session.add(bot_config)

    bot_config.text_replacement_enabled = status
    bot_config.updated_at = datetime.utcnow()
    db.session.commit()

    return jsonify({'status': status})

if __name__ == '__main__':
    # Make sure sessions directory exists
    os.makedirs('sessions', exist_ok=True)
    # Always serve on port 5000
    app.run(host='0.0.0.0', port=5000)
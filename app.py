from flask import Flask, render_template, request, session, redirect, url_for, jsonify
from telethon import TelegramClient, events
from telethon.errors import SessionPasswordNeededError, PhoneNumberInvalidError
import asyncio
import os
import logging
from functools import wraps
from asgiref.sync import async_to_sync

# Set up logging
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

app = Flask(__name__)
app.secret_key = os.urandom(24)  # for session management

# Store sessions in filesystem
app.config['SESSION_TYPE'] = 'filesystem'
app.config['SESSION_FILE_DIR'] = 'flask_sessions'

# Telegram API credentials
API_ID = int(os.getenv('API_ID', '27202142'))
API_HASH = os.getenv('API_HASH', 'db4dd0d95dc68d46b77518bf997ed165')

def cleanup_old_sessions():
    """Remove old session files"""
    try:
        # Clean up Telegram sessions
        sessions_dir = 'sessions'
        if os.path.exists(sessions_dir):
            for filename in os.listdir(sessions_dir):
                filepath = os.path.join(sessions_dir, filename)
                try:
                    os.remove(filepath)
                    logger.info(f"Removed old session file: {filename}")
                except Exception as e:
                    logger.error(f"Error removing session file {filename}: {e}")

        # Clean up Flask sessions
        flask_sessions_dir = 'flask_sessions'
        if os.path.exists(flask_sessions_dir):
            for filename in os.listdir(flask_sessions_dir):
                filepath = os.path.join(flask_sessions_dir, filename)
                try:
                    os.remove(filepath)
                    logger.info(f"Removed old Flask session file: {filename}")
                except Exception as e:
                    logger.error(f"Error removing Flask session file {filename}: {e}")
    except Exception as e:
        logger.error(f"Error in cleanup_old_sessions: {e}")

def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_phone' not in session:
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function

@app.route('/')
def login():
    # Clean up old sessions
    cleanup_old_sessions()
    return render_template('login.html')

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
            session_file = f"sessions/{phone}"

            # If session exists, check if it's valid
            if os.path.exists(session_file):
                try:
                    client = TelegramClient(session_file, API_ID, API_HASH)
                    await client.connect()
                    if await client.is_user_authorized():
                        logger.info(f"Found valid session for {phone}")
                        session['user_phone'] = phone
                        session['logged_in'] = True
                        await client.disconnect()
                        return {'message': 'Already authorized', 'already_authorized': True}
                    else:
                        logger.info(f"Session exists but not authorized for {phone}")
                        os.remove(session_file)
                except Exception as e:
                    logger.error(f"Error checking existing session: {e}")
                    if os.path.exists(session_file):
                        os.remove(session_file)

            # Create new session
            client = TelegramClient(session_file, API_ID, API_HASH)
            try:
                logger.info("Connecting to Telegram...")
                await client.connect()

                try:
                    # Send code and get the phone_code_hash
                    sent = await client.send_code_request(phone)
                    session['user_phone'] = phone
                    session['phone_code_hash'] = sent.phone_code_hash
                    logger.info("Code request sent successfully")
                    await client.disconnect()
                    return {'message': 'OTP sent successfully'}
                except PhoneNumberInvalidError:
                    logger.error(f"Invalid phone number: {phone}")
                    await client.disconnect()
                    if os.path.exists(session_file):
                        os.remove(session_file)
                    return {'error': 'Invalid phone number'}, 400

            except Exception as e:
                logger.error(f"Error in send_code: {str(e)}")
                if client and client.connected:
                    await client.disconnect()
                if os.path.exists(session_file):
                    os.remove(session_file)
                raise e

        result = asyncio.run(send_code())
        if isinstance(result, tuple):
            return jsonify(result[0]), result[1]
        return jsonify(result)
    except Exception as e:
        logger.error(f"Error in send_otp route: {str(e)}")
        return jsonify({'error': str(e)}), 500

@app.route('/verify-otp', methods=['POST'])
def verify_otp():
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
        async def verify():
            logger.info(f"Verifying OTP for phone: {phone}")
            session_file = f"sessions/{phone}"
            client = TelegramClient(session_file, API_ID, API_HASH)

            try:
                await client.connect()
                logger.info("Connected to Telegram")

                try:
                    # Sign in with phone code hash
                    await client.sign_in(phone, otp, phone_code_hash=phone_code_hash)
                    logger.info("Sign in successful")
                except SessionPasswordNeededError:
                    logger.info("2FA password needed")
                    if not password:
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
                        await client.disconnect()
                        return {'error': 'Invalid 2FA password'}, 400

                if await client.is_user_authorized():
                    session['logged_in'] = True
                    await client.disconnect()
                    return {'message': 'Login successful'}
                else:
                    await client.disconnect()
                    return {'error': 'Invalid OTP'}, 400

            except Exception as e:
                logger.error(f"Error during verification: {str(e)}")
                if client and client.connected:
                    await client.disconnect()
                raise e

        result = asyncio.run(verify())
        if isinstance(result, tuple):
            return jsonify(result[0]), result[1]
        return jsonify(result)
    except Exception as e:
        logger.error(f"Error in verify_otp route: {str(e)}")
        return jsonify({'error': str(e)}), 500

@app.route('/dashboard')
@login_required
def dashboard():
    async def get_channels():
        phone = session.get('user_phone')
        logger.info(f"Fetching channels for phone: {phone}")
        client = TelegramClient(f"sessions/{phone}", API_ID, API_HASH)

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
            return channels
        except Exception as e:
            logger.error(f"Error fetching channels: {str(e)}")
            if client and client.connected:
                await client.disconnect()
            raise e

    try:
        channels = asyncio.run(get_channels())
        return render_template('dashboard.html', channels=channels)
    except Exception as e:
        logger.error(f"Error in dashboard route: {str(e)}")
        return redirect(url_for('login'))

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

        # Store channel IDs in session
        session['source_channel'] = source
        session['dest_channel'] = destination
        logger.info(f"Updated channel configuration - Source: {source}, Destination: {destination}")

        return jsonify({'message': 'Channel configuration updated successfully'})
    except Exception as e:
        logger.error(f"Error updating channels: {str(e)}")
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

        # Here you would typically start/stop the bot process
        # For now, we'll just update the session status
        session['bot_running'] = status
        logger.info(f"Bot status changed to: {'running' if status else 'stopped'}")

        return jsonify({
            'status': status,
            'message': f"Bot is now {'running' if status else 'stopped'}"
        })
    except Exception as e:
        logger.error(f"Error toggling bot: {str(e)}")
        return jsonify({'error': str(e)}), 500

if __name__ == '__main__':
    # Make sure sessions directory exists
    os.makedirs('sessions', exist_ok=True)
    os.makedirs('flask_sessions', exist_ok=True)

    # Run with debug mode
    app.run(host='0.0.0.0', port=5000, debug=True)
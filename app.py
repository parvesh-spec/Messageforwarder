from flask import Flask, render_template, request, session, redirect, url_for, jsonify
from telethon import TelegramClient, events
from telethon.errors import SessionPasswordNeededError
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

# Telegram API credentials
API_ID = int(os.getenv('API_ID', '27202142'))
API_HASH = os.getenv('API_HASH', 'db4dd0d95dc68d46b77518bf997ed165')

# Store OTPs temporarily (in production, use a proper database)
otp_store = {}

def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_phone' not in session:
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
        # Create sessions directory if it doesn't exist
        os.makedirs('sessions', exist_ok=True)

        async def send_code():
            logger.info(f"Initializing Telegram client for phone: {phone}")
            client = TelegramClient(f"sessions/{phone}", API_ID, API_HASH)

            try:
                logger.info("Connecting to Telegram...")
                await client.connect()

                if not await client.is_user_authorized():
                    logger.info("User not authorized, sending code request...")
                    # Send code and get the phone_code_hash
                    sent = await client.send_code_request(phone)
                    session['user_phone'] = phone
                    session['phone_code_hash'] = sent.phone_code_hash
                    logger.info("Code request sent successfully")
                    await client.disconnect()
                    return {'message': 'OTP sent successfully'}
                else:
                    logger.info("User already authorized")
                    await client.disconnect()
                    return {'message': 'Already authorized'}
            except Exception as e:
                logger.error(f"Error in send_code: {str(e)}")
                if client and client.connected:
                    await client.disconnect()
                raise e

        return jsonify(asyncio.run(send_code()))
    except Exception as e:
        logger.error(f"Error in send_otp route: {str(e)}")
        return jsonify({'error': str(e)}), 500

@app.route('/verify-otp', methods=['POST'])
def verify_otp():
    phone = session.get('user_phone')
    phone_code_hash = session.get('phone_code_hash')
    otp = request.form.get('otp')
    password = request.form.get('password')  # For 2FA

    if not phone or not otp or not phone_code_hash:
        logger.error("Missing required verification data")
        return jsonify({'error': 'Phone, OTP and verification data are required'}), 400

    try:
        async def verify():
            logger.info(f"Verifying OTP for phone: {phone}")
            client = TelegramClient(f"sessions/{phone}", API_ID, API_HASH)

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
                        }, 403
                    await client.sign_in(password=password)
                    logger.info("2FA verification successful")

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

        return jsonify(asyncio.run(verify()) if isinstance(asyncio.run(verify()), dict) else asyncio.run(verify())[0]), \
                200 if isinstance(asyncio.run(verify()), dict) else asyncio.run(verify())[1]
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

    return render_template('dashboard.html', channels=asyncio.run(get_channels()))

@app.route('/bot/toggle', methods=['POST'])
@login_required
def toggle_bot():
    status = request.form.get('status') == 'true'
    # Add bot start/stop logic here
    return jsonify({'status': status})

@app.route('/replace/toggle', methods=['POST'])
@login_required
def toggle_replace():
    status = request.form.get('status') == 'true'
    # Add text replacement toggle logic here
    return jsonify({'status': status})

if __name__ == '__main__':
    # Make sure sessions directory exists
    os.makedirs('sessions', exist_ok=True)
    # Run with Hypercorn for better async support
    import hypercorn.asyncio
    from hypercorn.config import Config

    config = Config()
    config.bind = ["0.0.0.0:5000"]
    config.accesslog = logging.getLogger('hypercorn.access')
    config.errorlog = logging.getLogger('hypercorn.error')
    asyncio.run(hypercorn.asyncio.serve(app, config))
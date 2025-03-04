from flask import Flask, render_template, request, session, redirect, url_for, jsonify
from telethon import TelegramClient, events
from telethon.errors import SessionPasswordNeededError
import asyncio
import os
from functools import wraps
from datetime import datetime, timedelta
import json

app = Flask(__name__)
app.secret_key = os.urandom(24)  # for session management

# Telegram API credentials
API_ID = int(os.getenv('API_ID', '27202142'))
API_HASH = os.getenv('API_HASH', 'db4dd0d95dc68d46b77518bf997ed165')

def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'logged_in' not in session or not session['logged_in']:
            print("No valid session found, redirecting to login")
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function

@app.route('/')
def login():
    if 'logged_in' in session and session['logged_in']:
        return redirect(url_for('dashboard'))
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
                    me = await client.get_me()
                    await client.disconnect()

                    # Store session info
                    session['logged_in'] = True
                    session['user_phone'] = phone
                    session['user_id'] = me.id

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

        return jsonify(result[0]), result[1]

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

                session['logged_in'] = True
                session['user_id'] = me.id
                return {'message': 'Login successful'}
            else:
                await client.disconnect()
                return {'error': 'Invalid OTP'}, 400

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        result = loop.run_until_complete(verify())
        loop.close()

        return jsonify(result[0]), result[1]
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
        phone = session.get('user_phone')
        source = session.get('source_channel')
        dest = session.get('destination_channel')

        if not (phone and source and dest):
            return jsonify({'error': 'Missing configuration'}), 400

        # Update active forwards environment variable
        current_forwards = json.loads(os.environ.get('ACTIVE_FORWARDS', '[]'))

        if status:
            # Add new configuration
            config = {'phone': phone, 'source': source, 'dest': dest}
            if config not in current_forwards:
                current_forwards.append(config)
        else:
            # Remove configuration
            current_forwards = [c for c in current_forwards 
                              if not (c.get('phone') == phone and 
                                    c.get('source') == source and 
                                    c.get('dest') == dest)]

        os.environ['ACTIVE_FORWARDS'] = json.dumps(current_forwards)
        session['bot_active'] = status

        return jsonify({'status': status})

    except Exception as e:
        print(f"Error toggling bot: {e}")
        return jsonify({'error': str(e)}), 500

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
def logout():
    session.clear()
    return redirect(url_for('login'))

if __name__ == '__main__':
    # Make sure sessions directory exists
    os.makedirs('sessions', exist_ok=True)
    # Always serve on port 5000
    app.run(host='0.0.0.0', port=5000)
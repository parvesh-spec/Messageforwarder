from flask import Flask, render_template, request, session, redirect, url_for, jsonify
from telethon import TelegramClient, events
import asyncio
import os
from functools import wraps

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
async def send_otp():
    phone = request.form.get('phone')
    if not phone:
        return jsonify({'error': 'Phone number is required'}), 400

    try:
        client = TelegramClient(f"sessions/{phone}", API_ID, API_HASH)
        await client.connect()
        
        if not await client.is_user_authorized():
            await client.send_code_request(phone)
            session['user_phone'] = phone
            return jsonify({'message': 'OTP sent successfully'})
        else:
            return jsonify({'message': 'Already authorized'})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/verify-otp', methods=['POST'])
async def verify_otp():
    phone = session.get('user_phone')
    otp = request.form.get('otp')
    
    if not phone or not otp:
        return jsonify({'error': 'Phone and OTP are required'}), 400

    try:
        client = TelegramClient(f"sessions/{phone}", API_ID, API_HASH)
        await client.connect()
        
        await client.sign_in(phone, otp)
        
        if await client.is_user_authorized():
            session['logged_in'] = True
            return jsonify({'message': 'Login successful'})
        else:
            return jsonify({'error': 'Invalid OTP'}), 400
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/dashboard')
@login_required
async def dashboard():
    phone = session.get('user_phone')
    client = TelegramClient(f"sessions/{phone}", API_ID, API_HASH)
    await client.connect()
    
    # Get user's channels
    channels = []
    async for dialog in client.iter_dialogs():
        if dialog.is_channel:
            channels.append({
                'id': dialog.id,
                'name': dialog.name
            })
    
    return render_template('dashboard.html', channels=channels)

@app.route('/bot/toggle', methods=['POST'])
@login_required
def toggle_bot():
    status = request.form.get('status')
    # Add bot start/stop logic here
    return jsonify({'status': status})

@app.route('/replace/toggle', methods=['POST'])
@login_required
def toggle_replace():
    status = request.form.get('status')
    # Add text replacement toggle logic here
    return jsonify({'status': status})

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)

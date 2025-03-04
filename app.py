from flask import Flask, render_template, request, session, redirect, url_for, jsonify
from telethon import TelegramClient, events
import os
from functools import wraps

app = Flask(__name__)
app.secret_key = os.urandom(24)  # for session management

# Telegram API credentials
API_ID = int(os.getenv('API_ID', '27202142'))
API_HASH = os.getenv('API_HASH', 'db4dd0d95dc68d46b77518bf997ed165')

# Store client instances
clients = {}

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
    try:
        phone = request.form.get('phone')
        if not phone:
            return jsonify({'error': 'Phone number is required'}), 400

        # Create new client instance
        client = TelegramClient(f"sessions/{phone}", API_ID, API_HASH)
        client.connect()

        if not client.is_user_authorized():
            client.send_code_request(phone)
            session['user_phone'] = phone
            clients[phone] = client
            return jsonify({'message': 'OTP sent successfully'})
        else:
            session['user_phone'] = phone
            clients[phone] = client
            return jsonify({'message': 'Already authorized'})
    except Exception as e:
        print(f"Error in send_otp: {str(e)}")
        return jsonify({'error': 'Failed to send OTP. Please try again.'}), 500

@app.route('/verify-otp', methods=['POST'])
def verify_otp():
    try:
        phone = session.get('user_phone')
        otp = request.form.get('otp')

        if not phone or not otp:
            return jsonify({'error': 'Phone and OTP are required'}), 400

        client = clients.get(phone)
        if not client:
            return jsonify({'error': 'Session expired. Please try again.'}), 400

        try:
            client.sign_in(phone, otp)

            if client.is_user_authorized():
                session['logged_in'] = True
                return jsonify({'message': 'Login successful'})
            else:
                return jsonify({'error': 'Invalid OTP'}), 400
        except Exception as e:
            print(f"Error in sign_in: {str(e)}")
            return jsonify({'error': str(e)}), 400

    except Exception as e:
        print(f"Error in verify_otp: {str(e)}")
        return jsonify({'error': 'Failed to verify OTP. Please try again.'}), 500

@app.route('/dashboard')
@login_required
def dashboard():
    try:
        phone = session.get('user_phone')
        client = clients.get(phone)

        if not client:
            return redirect(url_for('login'))

        # Get user's channels
        channels = []
        for dialog in client.iter_dialogs():
            if dialog.is_channel:
                channels.append({
                    'id': dialog.id,
                    'name': dialog.name
                })

        return render_template('dashboard.html', channels=channels)
    except Exception as e:
        print(f"Error in dashboard: {str(e)}")
        return redirect(url_for('login'))

@app.route('/bot/toggle', methods=['POST'])
@login_required
def toggle_bot():
    try:
        status = request.form.get('status', 'false').lower() == 'true'
        # Add bot start/stop logic here
        return jsonify({'status': status})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/replace/toggle', methods=['POST'])
@login_required
def toggle_replace():
    try:
        status = request.form.get('status', 'false').lower() == 'true'
        # Add text replacement toggle logic here
        return jsonify({'status': status})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

if __name__ == '__main__':
    # Create sessions directory if it doesn't exist
    if not os.path.exists('sessions'):
        os.makedirs('sessions')

    app.run(host='0.0.0.0', port=5000)
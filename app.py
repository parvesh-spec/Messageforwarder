from flask import Flask, render_template, request, session, redirect, url_for, jsonify
from telethon import TelegramClient, events, sync, Button
from telethon.errors import SessionPasswordNeededError
from functools import wraps
import os
import re

# These example values won't work. You must get your own api_id and
# api_hash from https://my.telegram.org, under API Development.
API_ID = int(os.getenv('API_ID', '27202142'))  # Replace with your API ID
API_HASH = os.getenv('API_HASH', 'db4dd0d95dc68d46b77518bf997ed165')  # Replace with your API hash

# Store client instances and states
clients = {}
client_states = {}

app = Flask(__name__)
app.secret_key = os.urandom(24)  # for session management

def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_phone' not in session or not client_states.get(session['user_phone'], {}).get('authorized', False):
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

        print(f"\nProcessing OTP request for phone: {phone}")

        # Initialize client and state
        if phone not in clients:
            print(f"Creating new client for {phone}")
            client = TelegramClient(f"sessions/{phone}", API_ID, API_HASH)
            client.start()
            clients[phone] = client
            client_states[phone] = {'authorized': False, 'phone': phone}

        client = clients[phone]

        # Check if already authorized
        if client.is_user_authorized():
            print(f"User {phone} is already authorized")
            session['user_phone'] = phone
            client_states[phone]['authorized'] = True
            return jsonify({'message': 'Already authorized', 'redirect': '/dashboard'})

        # Send code request
        print(f"Sending code request to {phone}")
        client.send_code_request(phone)
        session['user_phone'] = phone

        return jsonify({'message': 'OTP sent successfully'})

    except Exception as e:
        print(f"Error in send_otp: {str(e)}")
        return jsonify({'error': str(e)}), 500

@app.route('/verify-otp', methods=['POST'])
def verify_otp():
    try:
        phone = session.get('user_phone')
        otp = request.form.get('otp')

        print(f"\nVerifying OTP for phone: {phone}")

        if not phone or not otp:
            return jsonify({'error': 'Phone and OTP are required'}), 400

        client = clients.get(phone)
        if not client:
            return jsonify({'error': 'Session expired. Please try again.'}), 400

        try:
            # Sign in with the code
            print(f"Attempting to sign in with OTP for {phone}")
            client.sign_in(phone, otp)

            if client.is_user_authorized():
                client_states[phone]['authorized'] = True
                print(f"User {phone} successfully signed in")
                return jsonify({'message': 'Login successful', 'redirect': '/dashboard'})
            else:
                print(f"Authorization failed for {phone}")
                return jsonify({'error': 'Authorization failed. Please try again.'}), 400

        except SessionPasswordNeededError:
            print(f"2FA required for {phone}")
            return jsonify({
                'error': 'Two-factor authentication is enabled. Please enter your password.',
                'needs_2fa': True
            }), 400
        except Exception as e:
            print(f"Error in sign_in: {str(e)}")
            return jsonify({'error': str(e)}), 400

    except Exception as e:
        print(f"Error in verify_otp: {str(e)}")
        return jsonify({'error': str(e)}), 500

@app.route('/verify-2fa', methods=['POST'])
def verify_2fa():
    try:
        phone = session.get('user_phone')
        password = request.form.get('password')

        print(f"\nVerifying 2FA for phone: {phone}")

        if not phone or not password:
            return jsonify({'error': 'Phone and password are required'}), 400

        client = clients.get(phone)
        if not client:
            return jsonify({'error': 'Session expired. Please try again.'}), 400

        try:
            print(f"Attempting 2FA verification for {phone}")
            client.sign_in(password=password)

            if client.is_user_authorized():
                client_states[phone]['authorized'] = True
                print(f"2FA verification successful for {phone}")
                return jsonify({'message': 'Login successful', 'redirect': '/dashboard'})
            else:
                print(f"2FA verification failed for {phone}")
                return jsonify({'error': 'Invalid password. Please try again.'}), 400
        except Exception as e:
            print(f"Error in 2FA: {str(e)}")
            return jsonify({'error': str(e)}), 400

    except Exception as e:
        print(f"Error in verify_2fa: {str(e)}")
        return jsonify({'error': str(e)}), 500

@app.route('/dashboard')
@login_required
def dashboard():
    try:
        phone = session.get('user_phone')
        client = clients.get(phone)

        if not client or not client_states.get(phone, {}).get('authorized', False):
            return redirect(url_for('login'))

        # Get user's channels
        print(f"Fetching channels for {phone}")
        channels = []
        try:
            dialogs = client.get_dialogs()
            for dialog in dialogs:
                if dialog.is_channel:
                    channels.append({
                        'id': dialog.id,
                        'name': dialog.name
                    })
            print(f"Found {len(channels)} channels")
        except Exception as e:
            print(f"Error fetching channels: {str(e)}")
            return redirect(url_for('login'))

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
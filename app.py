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
import json
from flask_session import Session
from datetime import timedelta
from flask_sqlalchemy import SQLAlchemy

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

# Database connection with better connection handling
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

# Telegram API credentials
API_ID = int(os.getenv('API_ID', '27202142'))
API_HASH = os.getenv('API_HASH', 'db4dd0d95dc68d46b77518bf997ed165')

def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_phone' not in session:
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

@app.route('/')
def login():
    return render_template('login.html')

@app.route('/dashboard')
@login_required
def dashboard():
    async def get_channels():
        phone = session.get('user_phone')
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

            return channels, last_config
        except Exception as e:
            logger.error(f"Error fetching channels: {str(e)}")
            if client and client.connected:
                await client.disconnect()
            raise e

    try:
        channels, last_config = asyncio.run(get_channels())
        return render_template('dashboard.html', 
                             channels=channels,
                             last_source=last_config['source_channel'] if last_config else None,
                             last_dest=last_config['destination_channel'] if last_config else None)
    except Exception as e:
        logger.error(f"Error in dashboard route: {str(e)}")
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
        main.CURRENT_USER_ID = user_id
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
                ORDER BY LENGTH(original_text) DESC
            """, (user_id,))
            replacements = {row['original_text']: row['replacement_text'] for row in cur.fetchall()}
            logger.info(f"Retrieved {len(replacements)} replacements for user {user_id}")

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
            main.CURRENT_USER_ID = user_id
            main.load_user_replacements(user_id)
            return jsonify({'message': 'Replacement removed successfully'})
        else:
            return jsonify({'error': 'Replacement not found'}), 404

    except Exception as e:
        logger.error(f"Error removing replacement: {str(e)}")
        return jsonify({'error': str(e)}), 500

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
        main.CURRENT_USER_ID = user_id
        main.load_user_replacements(user_id)

        logger.info("Cleared all text replacements for user")
        return jsonify({'message': 'All replacements cleared'})
    except Exception as e:
        logger.error(f"Error clearing replacements: {str(e)}")
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

        # Format channel IDs properly
        if not source.startswith('-100'):
            source = f"-100{source.lstrip('-')}"
        if not destination.startswith('-100'):
            destination = f"-100{destination.lstrip('-')}"

        # Store channel IDs in session for the current user
        session['source_channel'] = source
        session['dest_channel'] = destination

        # Save to database
        db = get_db()
        with db.cursor() as cur:
            # First create the table if it doesn't exist
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

            db.commit()

        logger.info(f"Updated channel configuration - Source: {source}, Destination: {destination}")
        return jsonify({'message': 'Channel configuration updated successfully'})
    except Exception as e:
        logger.error(f"Error updating channels: {str(e)}")
        return jsonify({'error': str(e)}), 500

if __name__ == '__main__':
    os.makedirs('sessions', exist_ok=True)
    # ALWAYS serve the app on port 5000
    app.run(host='0.0.0.0', port=5000, debug=True)
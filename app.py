import os
import logging
from flask import Flask, render_template, session, redirect, url_for, jsonify
from flask_sqlalchemy import SQLAlchemy
from flask_session import Session
from datetime import timedelta
import psycopg2
from psycopg2.extras import DictCursor
from psycopg2 import pool
from contextlib import contextmanager

# Set up logging
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Initialize Flask app
app = Flask(__name__)

# Configure Flask application
app.config.update(
    SECRET_KEY=os.urandom(24),
    SQLALCHEMY_DATABASE_URI=os.getenv('DATABASE_URL'),
    SQLALCHEMY_TRACK_MODIFICATIONS=False,
    SESSION_TYPE='sqlalchemy',
    SESSION_PERMANENT=True,
    PERMANENT_SESSION_LIFETIME=timedelta(days=7)
)

# Initialize SQLAlchemy
db = SQLAlchemy(app)

# Initialize Flask-Session
app.config['SESSION_SQLALCHEMY'] = db
Session(app)

# Database pool for connections
db_pool = psycopg2.pool.ThreadedConnectionPool(
    minconn=1,
    maxconn=10,
    dsn=os.getenv('DATABASE_URL')
)

# Database connection context manager
@contextmanager
def get_db():
    conn = db_pool.getconn()
    try:
        conn.autocommit = True
        yield conn
    finally:
        db_pool.putconn(conn)

# Create necessary database tables
def create_tables():
    with get_db() as conn:
        with conn.cursor() as cur:
            # Create telethon_sessions table (only if needed)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS telethon_sessions (
                    id SERIAL PRIMARY KEY,
                    phone_number VARCHAR(255) UNIQUE NOT NULL,
                    session_string TEXT NOT NULL,
                    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
                )
            """)

            # Create channel_config table (only if needed)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS channel_config (
                    id SERIAL PRIMARY KEY,
                    source_channel VARCHAR(255) NOT NULL,
                    destination_channel VARCHAR(255) NOT NULL,
                    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
                )
            """)

            # Create text_replacements table (only if needed)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS text_replacements (
                    id SERIAL PRIMARY KEY,
                    original_text TEXT NOT NULL,
                    replacement_text TEXT NOT NULL,
                    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
                )
            """)

# Initialize tables
create_tables()


@app.route('/')
def login():
    return render_template('login.html')

@app.route('/get-replacements')
def get_replacements():
    try:
        with get_db() as conn:
            with conn.cursor(cursor_factory=DictCursor) as cur:
                cur.execute("""
                    SELECT original_text, replacement_text 
                    FROM text_replacements
                    ORDER BY id DESC
                """)
                replacements = {row['original_text']: row['replacement_text'] for row in cur.fetchall()}
                return jsonify(replacements)
    except Exception as e:
        logger.error(f"❌ Get replacements error: {str(e)}")
        return jsonify({'error': str(e)}), 500

@app.route('/add-replacement', methods=['POST'])
def add_replacement():
    try:
        original = request.form.get('original')
        replacement = request.form.get('replacement')

        if not original or not replacement:
            return jsonify({'error': 'Both original and replacement text required'}), 400

        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO text_replacements (original_text, replacement_text)
                    VALUES (%s, %s)
                """, (original, replacement))

        return jsonify({'message': 'Replacement added successfully'})
    except Exception as e:
        logger.error(f"❌ Add replacement error: {str(e)}")
        return jsonify({'error': str(e)}), 500


@app.route('/remove-replacement', methods=['POST'])
def remove_replacement():
    try:
        original = request.form.get('original')
        if not original:
            return jsonify({'error': 'Original text required'}), 400

        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    DELETE FROM text_replacements 
                    WHERE original_text = %s
                """, (original,))

        return jsonify({'message': 'Replacement removed successfully'})
    except Exception as e:
        logger.error(f"❌ Remove replacement error: {str(e)}")
        return jsonify({'error': str(e)}), 500


@app.route('/clear-replacements', methods=['POST'])
def clear_replacements():
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM text_replacements")

        return jsonify({'message': 'All replacements cleared'})
    except Exception as e:
        logger.error(f"❌ Clear replacements error: {str(e)}")
        return jsonify({'error': str(e)}), 500


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
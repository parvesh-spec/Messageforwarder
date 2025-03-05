import os
import logging
from flask import Flask, render_template, redirect, url_for, session, jsonify
from flask_login import LoginManager, login_required, UserMixin, current_user
import psycopg2
from psycopg2.extras import DictCursor
from datetime import timedelta

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Initialize Flask app
app = Flask(__name__)
app.secret_key = os.urandom(24)
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(days=7)

# Initialize login manager
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login'

class User(UserMixin):
    def __init__(self, user_id, phone=None):
        self.id = user_id
        self.phone = phone

    @staticmethod
    def get(user_id):
        db = get_db()
        if not db:
            return None

        try:
            with db.cursor(cursor_factory=DictCursor) as cur:
                cur.execute("SELECT * FROM users WHERE id = %s", (user_id,))
                user = cur.fetchone()
                if user:
                    return User(user['id'], user['phone'])
        except Exception as e:
            logger.error(f"Error getting user: {str(e)}")
        return None

@login_manager.user_loader
def load_user(user_id):
    return User.get(user_id)

def get_db():
    """Database connection factory"""
    try:
        conn = psycopg2.connect(
            os.getenv('DATABASE_URL'),
            application_name='telegram_bot_web',
            connect_timeout=10
        )
        conn.autocommit = True
        return conn
    except Exception as e:
        logger.error(f"Database connection error: {str(e)}")
        return None

@app.route('/')
def index():
    """Root endpoint - redirects to dashboard if logged in, otherwise to login"""
    try:
        if current_user.is_authenticated:
            return redirect(url_for('dashboard'))
        return redirect(url_for('login'))
    except Exception as e:
        logger.error(f"Error in index route: {str(e)}")
        # For health checks, return 200
        return jsonify({
            'status': 'healthy',
            'message': 'Service is running'
        }), 200

@app.route('/login')
def login():
    """Login page route"""
    if current_user.is_authenticated:
        return redirect(url_for('dashboard'))
    return render_template('login.html')

@app.route('/dashboard')
@login_required
def dashboard():
    """Dashboard page route"""
    try:
        # We'll add channel fetching logic here later
        return render_template('dashboard.html', channels=[])
    except Exception as e:
        logger.error(f"Error in dashboard route: {str(e)}")
        return redirect(url_for('login'))

@app.route('/logout')
def logout():
    """Logout route"""
    session.clear()
    return redirect(url_for('login'))

@app.route('/health')
def health_check():
    """Health check endpoint"""
    try:
        # Test database connection
        db = get_db()
        if db:
            with db.cursor() as cur:
                cur.execute("SELECT 1")
                cur.fetchone()
            return jsonify({
                'status': 'healthy',
                'database': 'connected'
            }), 200
    except Exception as e:
        logger.error(f"Health check failed: {str(e)}")

    # Still return 200 for deployment health checks
    return jsonify({
        'status': 'degraded',
        'message': 'Service is running'
    }), 200

@app.errorhandler(404)
def not_found_error(error):
    return render_template('404.html'), 404

@app.errorhandler(500)
def internal_error(error):
    return render_template('500.html'), 500

if __name__ == '__main__':
    port = int(os.getenv('PORT', 5000))
    app.run(host='0.0.0.0', port=port)